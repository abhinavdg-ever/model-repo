#!/usr/bin/env python3
"""Bring a taxonomy workbook (``Taxonomy_v29_3_training_needs.xlsx`` and later) into the repo.

Reads the workbook's ``Taxonomy`` tab (L1, L2, L3, Level, Definition, Default specialty,
Codeability) and writes, keeping every other key of each file as it is:

* ``training/taxonomy/taxonomy.json``, and its copies in ``training/bert-training/``,
  ``training/bert-training-leaf/`` and ``training/annotation-tool/``: L1 / L2 / L3 with their
  descriptions, the paths, the version;
* ``core-pipeline/stages/lib/keyword-canon/page_taxonomy.json``: ``label_space`` (L1, L2 per
  L1, L3 per "L1 > L2"), ``codeability`` per path (a path already tagged keeps its tag; the
  workbook's is taken for new paths only), the version, and ``name_map`` entries for renamed
  nodes (an old label still reads as the new one).

Then run ``training/taxonomy/export_review_taxonomy.py`` for review-ui's lists.

    python training/taxonomy/import_taxonomy_xlsx.py <workbook.xlsx> --version v29.3 \\
        --rename "Consent and Authorization > HIPAA and Privacy=Consent and Authorization > HIPAA Notice & Consent"

The workbook is only read. Prints counts and names, never page text.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
TAXONOMY = REPO / "training" / "taxonomy" / "taxonomy.json"
COPIES = (
    REPO / "training" / "bert-training" / "taxonomy.json",
    REPO / "training" / "bert-training-leaf" / "taxonomy.json",
    REPO / "training" / "annotation-tool" / "taxonomy.json",
)
PAGE_TAXONOMY = REPO / "core-pipeline" / "stages" / "lib" / "keyword-canon" / "page_taxonomy.json"
CODEABILITY_TAG = {"codeable": "Codeable", "non_codeable": "Non-Codeable", "discharge": "Discharge",
                   "needs_review": "Needs Review"}


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def read_workbook(path: Path) -> list[dict[str, str]]:
    import openpyxl

    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    rows = list(book["Taxonomy"].iter_rows(values_only=True))
    head = [_text(c) for c in rows[0]]
    col = {name: head.index(name) for name in ("L1", "L2", "L3", "Level", "Definition",
                                                 "Default specialty", "Codeability")}
    out = []
    for row in rows[1:]:
        item = {name: _text(row[i]) if i < len(row) else "" for name, i in col.items()}
        if item["L1"]:
            out.append(item)
    return out


def build(rows: list[dict[str, str]], old: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """taxonomy.json from the workbook rows (old notes kept where a node survives), and
    the codeability per path."""
    notes = {}
    for l1, items in (old.get("L2") or {}).items():
        for item in items:
            notes[f"{l1} > {item['name']}"] = item.get("notes", "")
    for parent, items in (old.get("L3") or {}).items():
        for item in items:
            notes[f"{parent} > {item['name']}"] = item.get("notes", "")

    l1s: list[dict[str, str]] = []
    l2s: dict[str, list[dict[str, str]]] = {}
    l3s: dict[str, list[dict[str, str]]] = {}
    codeability: dict[str, str] = {}
    paths: list[str] = []
    for row in rows:
        l1, l2, l3 = row["L1"], row["L2"], row["L3"]
        tag = row["Codeability"] if row["Codeability"] not in ("", "None") else ""
        if l3:
            path = f"{l1} > {l2} > {l3}"
            l3s.setdefault(f"{l1} > {l2}", []).append(_node(l3, row, tag, notes.get(path, "")))
        elif l2:
            path = f"{l1} > {l2}"
            l2s.setdefault(l1, []).append(_node(l2, row, tag, notes.get(path, "")))
        else:
            path = l1
            l1s.append({"name": l1, "description": row["Definition"]})
        if tag and path != l1:
            codeability[path] = tag
        if path != l1:
            paths.append(path)
    new = dict(old)
    new.update(L1=l1s, L2=l2s, L3=l3s, paths=paths)
    new["counts"] = {"L1": len(l1s), "L2": sum(map(len, l2s.values())), "L3": sum(map(len, l3s.values())),
                     "specialty": len(old.get("specialty") or []), "paths": len(paths)}
    return new, codeability


def _node(name: str, row: dict[str, str], tag: str, note: str) -> dict[str, str]:
    return {"name": name, "description": row["Definition"], "specialty": row["Default specialty"],
            "codeability": tag, "codeability_tag": CODEABILITY_TAG.get(tag, ""), "notes": note}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("workbook", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--rename", action="append", default=[], metavar="OLD=NEW",
                        help="a renamed node, as a full path ('L1 > L2'); repeat for several")
    args = parser.parse_args(argv)

    old = json.loads(TAXONOMY.read_text(encoding="utf-8"))
    new, codeability = build(read_workbook(args.workbook), old)
    page = json.loads(PAGE_TAXONOMY.read_text(encoding="utf-8"))
    # Codeability already decided stays (2026-10-26/27 user calls: CT / MR angiography
    # Codable, nothing left for review); the workbook's tag is taken for a new path only.
    before = page.get("codeability") or {}
    renamed = {}
    for pair in args.rename:
        was, _, now = (part.strip() for part in pair.partition("="))
        renamed[now] = was
    codeability = {
        path: before.get(path, before.get(renamed.get(path, ""), tag)) for path, tag in codeability.items()
    }
    for parent_key in ("L2", "L3"):
        for parent, items in new[parent_key].items():
            for item in items:
                tag = codeability.get(f"{parent} > {item['name']}", item["codeability"])
                item["codeability"], item["codeability_tag"] = tag, CODEABILITY_TAG.get(tag, "")
    new["version"] = args.version
    new["source"] = f"{args.workbook.name} (Taxonomy tab), imported by training/taxonomy/import_taxonomy_xlsx.py"
    body = json.dumps(new, indent=1, ensure_ascii=False) + "\n"
    for target in (TAXONOMY, *COPIES):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")

    page["version"] = args.version
    space = page["label_space"]
    space["L1"] = [item["name"] for item in new["L1"]]
    space["L2"] = {l1: [item["name"] for item in items] for l1, items in new["L2"].items()}
    space["L3"] = {parent: [item["name"] for item in items] for parent, items in new["L3"].items()}
    page["codeability"] = codeability
    name_map = page.setdefault("name_map", {})
    for pair in args.rename:
        before, _, after = (part.strip() for part in pair.partition("="))
        level = "L1" if " > " not in before else "L2" if before.count(" > ") == 1 else "L3"
        name_map.setdefault(level, {})[before] = after
    PAGE_TAXONOMY.write_text(json.dumps(page, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"{args.version}: {new['counts']}; codeability tags {len(codeability)}; renames {len(args.rename)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
