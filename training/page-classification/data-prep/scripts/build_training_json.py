"""OCR a folder of page images and collate page-classifier training records.

One ``.txt`` per image, then JSONL. Each line has ``id``, ``text``,
``page_family``, ``page_type``, and ``codeability``. ``text`` is cut to 500
tokens (BERT wordpieces when ``transformers`` is installed, otherwise
whitespace tokens). ``codeability`` comes from the CSV when that column is
present, otherwise from the tag on the matching row in ``page_labels.json``.

CSV columns: ``page_id``, ``family_label``, ``page_type_label``. ``page_id``
is the image file name (``chart8841_p001.png`` joins ``chart8841_p001.txt``).

Usage:
    python training/page-classification/data-prep/scripts/build_training_json.py \\
        --images /path/to/images \\
        --csv training/page-classification/data-prep/samples/labels.csv \\
        --output training/page-classification/data-prep/training.jsonl
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
MAX_TOKENS = 500

_HEADER_ALIASES = {
    "page_id": "page_id",
    "pageid": "page_id",
    "image_id": "page_id",
    "image_name": "page_id",
    "filename": "page_id",
    "file": "page_id",
    "image": "page_id",
    "family_label": "family_label",
    "page_family": "family_label",
    "family": "family_label",
    "page_type_label": "page_type_label",
    "page_type": "page_type_label",
    "pagetype": "page_type_label",
    "type": "page_type_label",
    "codeability": "codeability",
    "tag": "codeability",
}

_WORD_RE = re.compile(r"\S+")


def iter_images(root: Path) -> list[Path]:
    images = [
        path
        for path in root.rglob("*")
        if path.is_file()
        and not path.name.startswith("._")
        and path.suffix.lower() in IMAGE_EXTS
    ]
    return sorted(images, key=lambda path: path.relative_to(root).as_posix().lower())


def tesseract_cmd() -> str:
    configured = (os.environ.get("TESSERACT_CMD") or "").strip()
    if configured:
        return configured
    found = shutil.which("tesseract")
    if not found:
        raise SystemExit("tesseract is not on PATH. Set TESSERACT_CMD to the binary.")
    return found


def ocr_image(image: Path, binary: str, lang: str) -> str:
    result = subprocess.run(
        [binary, str(image), "stdout", "-l", lang],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(detail or f"tesseract failed on {image.name}")
    return result.stdout.replace("\x0c", "").rstrip("\n")


def write_ocr_texts(images_root: Path, ocr_dir: Path, binary: str, lang: str) -> list[Path]:
    written: list[Path] = []
    for image in iter_images(images_root):
        target = ocr_dir / image.relative_to(images_root).with_suffix(".txt")
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            text = ocr_image(image, binary, lang)
        except RuntimeError as exc:
            print(f"skip {image}: {exc}", file=sys.stderr)
            continue
        target.write_text(text, encoding="utf-8")
        written.append(target)
    return written


def _whitespace_cut(text: str, max_tokens: int) -> str:
    words = list(_WORD_RE.finditer(text))
    if len(words) <= max_tokens:
        return text
    return text[: words[max_tokens - 1].end()]


def truncate_text(text: str, max_tokens: int = MAX_TOKENS, tokenizer=None) -> str:
    """Keep a prefix of ``text`` that is at most ``max_tokens`` tokens."""
    if tokenizer is None:
        return _whitespace_cut(text, max_tokens)
    encoded = tokenizer(
        text,
        add_special_tokens=False,
        return_offsets_mapping=True,
        truncation=True,
        max_length=max_tokens,
    )
    offsets = encoded["offset_mapping"]
    if not offsets:
        return ""
    return text[: offsets[-1][1]]


def load_bert_tokenizer(name: str | None):
    """WordPiece tokenizer, or None when transformers / the vocab is unavailable."""
    if name == "":
        return None
    try:
        from transformers import BertTokenizerFast
    except ImportError:
        return None
    model = name or "bert-base-uncased"
    try:
        return BertTokenizerFast.from_pretrained(model)
    except Exception as exc:
        print(f"BERT tokenizer unavailable ({exc}); cutting on whitespace.", file=sys.stderr)
        return None


def _norm_header(name: str) -> str:
    key = re.sub(r"[\s\-]+", "_", (name or "").strip().lower())
    return _HEADER_ALIASES.get(key, key)


def load_labels(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise SystemExit(f"{csv_path} has no header row")
        fields = [_norm_header(name) for name in reader.fieldnames]
        missing = {"page_id", "family_label", "page_type_label"} - set(fields)
        if missing:
            raise SystemExit(
                f"{csv_path} is missing {', '.join(sorted(missing))}. "
                "Expected page_id, family_label, page_type_label."
            )
        rows: list[dict[str, str]] = []
        for raw in reader:
            row = {
                _norm_header(key): (value or "").strip()
                for key, value in raw.items()
                if key is not None
            }
            if not row.get("page_id"):
                continue
            rows.append(row)
    return rows


def collate(
    labels: list[dict[str, str]],
    ocr_files: list[tuple[Path, str]],
    *,
    images_root: Path,
    max_tokens: int = MAX_TOKENS,
    tokenizer=None,
) -> tuple[list[dict[str, str]], list[str]]:
    """Join each ``page_id`` to the OCR text of that image.

    ``page_id`` is the image file name. ``chart8841_p001.png`` matches
    ``chart8841_p001.txt``. ``images_root`` is unused; the join is by file name.
    """
    del images_root
    by_name: dict[str, str] = {}
    for path, text in ocr_files:
        by_name[path.stem.casefold()] = text
        by_name[path.name.casefold()] = text

    records: list[dict[str, str]] = []
    problems: list[str] = []
    for row in labels:
        page_id = row["page_id"]
        named = Path(page_id)
        text = by_name.get(named.stem.casefold()) or by_name.get(named.name.casefold())
        if text is None:
            problems.append(f"no OCR for page_id={page_id}")
            continue
        record = {
            "id": named.stem,
            "text": truncate_text(text, max_tokens, tokenizer),
            "page_family": row["family_label"],
            "page_type": row["page_type_label"],
        }
        if row.get("codeability"):
            record["codeability"] = row["codeability"]
        records.append(record)
    return records, problems


def codeability_index(mapping_path: Path) -> dict[tuple[str, str], str]:
    payload = json.loads(mapping_path.read_text(encoding="utf-8"))
    return {
        (item["family"], item["page_type"]): item["tag"]
        for item in payload["labels"]
    }


def fill_codeability(
    records: list[dict[str, str]],
    index: dict[tuple[str, str], str],
) -> list[str]:
    """Set codeability from the catalog tag when the CSV did not supply it."""
    problems: list[str] = []
    for record in records:
        if record.get("codeability"):
            continue
        tag = index.get((record["page_family"], record["page_type"]))
        if not tag:
            problems.append(
                f"no codeability for {record['id']} "
                f"{record['page_family']} / {record['page_type']}"
            )
            continue
        record["codeability"] = tag
    return problems


def read_ocr_tree(ocr_dir: Path) -> list[tuple[Path, str]]:
    files = [
        path
        for path in ocr_dir.rglob("*.txt")
        if path.is_file() and not path.name.startswith("._")
    ]
    return [
        (path, path.read_text(encoding="utf-8"))
        for path in sorted(files, key=lambda item: item.as_posix().lower())
    ]


def build(args: argparse.Namespace) -> int:
    images_root = Path(args.images).resolve() if args.images else None
    ocr_dir = Path(args.ocr_dir).resolve()
    if images_root is not None:
        if not images_root.is_dir():
            raise SystemExit(f"image folder not found: {images_root}")
        written = write_ocr_texts(images_root, ocr_dir, tesseract_cmd(), args.lang)
        print(f"wrote {len(written)} text file(s) to {ocr_dir}")
        root_for_join = ocr_dir
    else:
        root_for_join = ocr_dir
    if not ocr_dir.is_dir():
        raise SystemExit(f"OCR folder not found: {ocr_dir}")

    labels = load_labels(Path(args.csv))
    tokenizer = load_bert_tokenizer(args.tokenizer)
    records, problems = collate(
        labels,
        read_ocr_tree(ocr_dir),
        images_root=root_for_join,
        max_tokens=args.max_tokens,
        tokenizer=tokenizer,
    )
    mapping = Path(args.mapping)
    if mapping.is_file():
        problems.extend(fill_codeability(records, codeability_index(mapping)))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )
    print(f"wrote {len(records)} record(s) to {output}")
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--images", help="Folder of page images. Omit to collate existing .txt files.")
    parser.add_argument("--csv", required=True, help="page_id, family_label, page_type_label")
    parser.add_argument(
        "--ocr-dir",
        default="training/page-classification/data-prep/ocr",
        help="Where the per-page .txt files go",
    )
    parser.add_argument(
        "--output",
        default="training/page-classification/data-prep/training.jsonl",
        help="JSONL the trainer reads",
    )
    parser.add_argument(
        "--mapping",
        default="training/page-classification/data-prep/mappings/page_labels.json",
        help="Catalog tags used as codeability when the CSV has no codeability column",
    )
    parser.add_argument("--max-tokens", type=int, default=MAX_TOKENS)
    parser.add_argument("--lang", default="eng")
    parser.add_argument(
        "--tokenizer",
        default=None,
        help="BERT tokenizer name or path. Default bert-base-uncased when transformers is installed. Pass '' to cut on whitespace.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    sys.exit(build(parse_args(argv)))


if __name__ == "__main__":
    main()
