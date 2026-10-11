#!/usr/bin/env python3
"""Merge several training_data.jsonl files (from the annotation tool) into one.

    python utilities/merge_training_json.py a/training_data.jsonl b/training_data.jsonl \\
        --out training/bert-training/data/training_data.jsonl
    python utilities/merge_training_json.py ~/Desktop/Training/ --out merged.jsonl      # every *.jsonl under it
    python utilities/merge_training_json.py --list files.txt --out merged.jsonl

Inputs are read in the order given (a folder: its *.jsonl files, sorted).

* **Same page in two files** — same ``image`` and same ``file_size`` — is kept
  once. The file listed **later wins**, so list the newest last; pages whose
  labels differ between files are counted.
* **Same image name, different file size** — two different pages that share a
  name (phone photos are often IMG_1234.HEIC in several folders) — both kept,
  and reported.
* **Identical text under different names** — very likely the same page twice,
  which would sit on both sides of the train / validation split — reported;
  dropped only with ``--drop-duplicate-text`` (the first one is kept).
* Records without ``image``, ``text`` or ``model_type`` are dropped and counted.
  Model types not in ``page_taxonomy.json`` are counted by name (not dropped).

Page text is patient data: only counts and file names are printed. The output
is written atomically; it may be one of the inputs (all inputs are read first).
Standard library only — any Python 3.9+.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parents[1]
DEFAULT_TAXONOMY = REPO / "core-pipeline" / "stages" / "lib" / "keyword-canon" / "page_taxonomy.json"
LABELS = ("model_type", "page_type", "page_subtype")
REQUIRED = ("image", "text", "model_type")


@dataclass
class Report:
    inputs: list[tuple[str, int]] = field(default_factory=list)  # (file, records read)
    bad_lines: Counter = field(default_factory=Counter)  # file -> lines that are not JSON
    missing_fields: int = 0
    same_page_replaced: int = 0
    label_changes: int = 0
    same_name_other_file: list[str] = field(default_factory=list)
    duplicate_text: list[tuple[str, str]] = field(default_factory=list)
    duplicate_text_dropped: int = 0
    unknown_model_types: Counter = field(default_factory=Counter)
    written: int = 0


def expand(paths: list[str], list_file: Optional[Path]) -> list[Path]:
    raw = list(paths)
    if list_file:
        raw += [line.strip() for line in list_file.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")]
    out: list[Path] = []
    for item in raw:
        path = Path(item).expanduser()
        if path.is_dir():
            out += sorted(p for p in path.rglob("*.jsonl") if not p.name.startswith("._"))
        elif path.is_file():
            out.append(path)
        else:
            raise FileNotFoundError(f"no such file or folder: {path}")
    seen: set[Path] = set()
    unique = []
    for path in out:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


def read_records(path: Path, report: Report) -> list[dict[str, Any]]:
    records = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                report.bad_lines[str(path)] += 1
                continue
            if not isinstance(record, dict) or any(not record.get(k) for k in REQUIRED):
                report.missing_fields += 1
                continue
            records.append(record)
    report.inputs.append((str(path), len(records)))
    return records


def merge(files: list[Path], *, drop_duplicate_text: bool = False,
          model_types: Optional[set[str]] = None) -> tuple[list[dict[str, Any]], Report]:
    report = Report()
    merged: dict[tuple[str, Any], dict[str, Any]] = {}  # (image, file_size) -> record
    order: list[tuple[str, Any]] = []
    sizes_by_name: dict[str, set[Any]] = {}

    for path in files:
        for record in read_records(path, report):
            key = (str(record["image"]), record.get("file_size"))
            if key in merged:
                report.same_page_replaced += 1
                if any(merged[key].get(k) != record.get(k) for k in LABELS):
                    report.label_changes += 1
            else:
                order.append(key)
            merged[key] = record  # later file wins
            sizes_by_name.setdefault(key[0], set()).add(key[1])

    report.same_name_other_file = sorted(n for n, sizes in sizes_by_name.items() if len(sizes) > 1)

    out: list[dict[str, Any]] = []
    by_text: dict[str, str] = {}
    for key in order:
        record = merged[key]
        digest = hashlib.sha256(" ".join(str(record["text"]).split()).encode("utf-8")).hexdigest()
        first = by_text.get(digest)
        if first is not None and first != record["image"]:
            report.duplicate_text.append((first, str(record["image"])))
            if drop_duplicate_text:
                report.duplicate_text_dropped += 1
                continue
        by_text.setdefault(digest, str(record["image"]))
        if model_types is not None and record["model_type"] not in model_types:
            report.unknown_model_types[record["model_type"]] += 1
        out.append(record)
    report.written = len(out)
    return out, report


def write_atomic(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_model_types(path: Optional[Path]) -> Optional[set[str]]:
    if path is None or not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return {m["model_type"] for m in data.get("model_types") or []}


def print_report(report: Report, out: Path, dry_run: bool) -> None:
    print("Inputs (in order; later wins):")
    for name, count in report.inputs:
        print(f"  {count:6d}  {name}")
    for name, count in report.bad_lines.items():
        print(f"  {count} line(s) in {name} are not JSON and were skipped")
    if report.missing_fields:
        print(f"Dropped {report.missing_fields} record(s) without image, text or model_type")
    print(f"Same page in more than one file: {report.same_page_replaced} "
          f"(kept the later; {report.label_changes} with different labels)")
    if report.same_name_other_file:
        print(f"Same image name, different file — kept both: {len(report.same_name_other_file)}")
        for name in report.same_name_other_file[:20]:
            print(f"  {name}")
        if len(report.same_name_other_file) > 20:
            print(f"  … and {len(report.same_name_other_file) - 20} more")
    if report.duplicate_text:
        action = "dropped the later" if report.duplicate_text_dropped else "kept both (use --drop-duplicate-text to drop)"
        print(f"Identical text under different names: {len(report.duplicate_text)} — {action}")
        for first, later in report.duplicate_text[:20]:
            print(f"  {first}  =  {later}")
    if report.unknown_model_types:
        print("Model types not in page_taxonomy.json (kept):")
        for name, count in report.unknown_model_types.most_common():
            print(f"  {count:6d}  {name}")
    verb = "Would write" if dry_run else "Wrote"
    print(f"{verb} {report.written} record(s) -> {out}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("inputs", nargs="*", help="training_data.jsonl files, or folders holding them")
    parser.add_argument("--list", type=Path, help="text file with one input path per line")
    parser.add_argument("--out", type=Path, required=True, help="merged training_data.jsonl to write")
    parser.add_argument("--drop-duplicate-text", action="store_true",
                        help="drop a record whose text is identical to an earlier one under another name")
    parser.add_argument("--taxonomy", type=Path, default=DEFAULT_TAXONOMY,
                        help="page_taxonomy.json to check model types against")
    parser.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    args = parser.parse_args(argv)

    files = expand(args.inputs, args.list)
    if not files:
        parser.error("no input files")
    out = args.out.expanduser()
    records, report = merge(files, drop_duplicate_text=args.drop_duplicate_text,
                            model_types=load_model_types(args.taxonomy))
    if not args.dry_run:
        write_atomic(out, records)
    print_report(report, out, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
