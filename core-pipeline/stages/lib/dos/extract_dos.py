#!/usr/bin/env python3
"""
DOS extraction for imaging folders → CSV.

For each chart folder under DATA_ROOT:
  1. Prefer ocr/<folder>_final1.txt
  2. Else fall back to ocr/<folder>_prelim.txt
  3. Extract DOS per page (regex; optional Azure OpenAI if --llm)

LLM is OFF by default. Enable with --llm or DOS_USE_LLM=true.

Usage:
  cd 02-imaging-pipeline/dos-extraction
  python extract_dos.py
  python extract_dos.py --llm
  python extract_dos.py --out ./output/dos_extraction.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
MONOREPO_ROOT = SCRIPT_DIR.parents[2]
IMAGING_UI_CANDIDATES = (
    MONOREPO_ROOT / "05-imaging-ui",
    SCRIPT_DIR.parent.parent / "05-imaging-ui",
)

CSV_COLUMNS = [
    "chart_name",
    "page_name",
    "page_number",
    "dos_from",
    "dos_to",
    "dos_from_iso",
    "dos_to_iso",
    "doc_dos_from",
    "doc_dos_to",
    "doc_dos_from_iso",
    "doc_dos_to_iso",
    "confidence",
]


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


def _env_flag(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def bootstrap_env() -> Path | None:
    ui: Path | None = None
    for cand in IMAGING_UI_CANDIDATES:
        if (cand / ".env").is_file() or (cand / "data" / "folders").is_dir():
            ui = cand.resolve()
            break
    sibling = SCRIPT_DIR.parent.parent / "05-imaging-ui"
    if ui is None and (
        (sibling / ".env").is_file() or (sibling / "data" / "folders").is_dir()
    ):
        ui = sibling.resolve()
    if ui:
        _load_env_file(ui / ".env")
    _load_env_file(SCRIPT_DIR / ".env")
    return ui


def find_ocr_file(folder: Path) -> tuple[Path | None, str | None]:
    ocr_dir = folder / "ocr"
    name = folder.name
    final1 = ocr_dir / f"{name}_final1.txt"
    prelim = ocr_dir / f"{name}_prelim.txt"
    if final1.is_file() and final1.stat().st_size > 0:
        return final1, "final1"
    if ocr_dir.is_dir():
        for p in sorted(ocr_dir.glob("*_final1.txt")):
            if p.is_file() and p.stat().st_size > 0:
                return p, "final1"
        for p in sorted(ocr_dir.glob("*_prelim.txt")):
            if p.is_file() and p.stat().st_size > 0:
                return p, "prelim"
    if prelim.is_file() and prelim.stat().st_size > 0:
        return prelim, "prelim"
    return None, None


def default_data_root(ui: Path | None) -> Path:
    env = (os.environ.get("DATA_ROOT") or "").strip()
    if env:
        path = Path(env)
        if not path.is_absolute() and ui is not None:
            return (ui / path).resolve()
        return path.resolve()
    if ui is not None:
        return (ui / "data" / "folders").resolve()
    return (MONOREPO_ROOT / "05-imaging-ui" / "data" / "folders").resolve()


def hit_to_row(
    chart_name: str,
    hit: dict,
) -> dict[str, str]:
    conf = hit.get("confidence")
    conf_s = ""
    if conf is not None and conf != "":
        try:
            conf_s = f"{float(conf):.2f}"
        except (TypeError, ValueError):
            conf_s = str(conf)
    return {
        "chart_name": chart_name,
        "page_name": str(hit.get("page_name") or ""),
        "page_number": str(hit.get("page_number") or ""),
        "dos_from": str(hit.get("dos_from") or ""),
        "dos_to": str(hit.get("dos_to") or ""),
        "dos_from_iso": str(hit.get("dos_from_iso") or ""),
        "dos_to_iso": str(hit.get("dos_to_iso") or ""),
        "doc_dos_from": str(hit.get("doc_dos_from") or ""),
        "doc_dos_to": str(hit.get("doc_dos_to") or ""),
        "doc_dos_from_iso": str(hit.get("doc_dos_from_iso") or ""),
        "doc_dos_to_iso": str(hit.get("doc_dos_to_iso") or ""),
        "confidence": conf_s,
    }


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    ui = bootstrap_env()

    parser = argparse.ArgumentParser(
        description="Extract DOS from final1 (else prelim) OCR → CSV"
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=default_data_root(ui),
        help="Path to data/folders",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=SCRIPT_DIR / "output" / "dos_extraction.csv",
        help="Combined CSV path (default: ./output/dos_extraction.csv)",
    )
    parser.add_argument(
        "--per-chart",
        action="store_true",
        help="Also write imaging/<chart>_dos.csv under each folder",
    )
    parser.add_argument(
        "--llm",
        action="store_true",
        help="Enable Azure OpenAI fallback (OFF by default)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Process at most N folders (0 = all)",
    )
    args = parser.parse_args()

    from azure_llm import get_azure_openai_client
    from dos_logic import detect_dos_per_page

    data_root = args.data_root.resolve()
    if not data_root.is_dir():
        print(f"DATA_ROOT not found: {data_root}", file=sys.stderr)
        sys.exit(1)

    use_llm = bool(args.llm) or _env_flag("DOS_USE_LLM", False)
    client = None
    if use_llm:
        client = get_azure_openai_client()
        if client is None:
            print(
                "LLM requested but Azure OpenAI is not configured "
                "(AZURE_OPENAI_API_KEY + AZURE_OPENAI_ENDPOINT). Using regex only.",
                file=sys.stderr,
            )
            use_llm = False
        else:
            print(
                f"LLM: ENABLED (Azure OpenAI deployment="
                f"{os.getenv('AZURE_OPENAI_DEPLOYMENT', 'gpt-4o-mini')})"
            )
    else:
        print("LLM: DISABLED (regex only). Pass --llm to enable Azure OpenAI.")

    folders = sorted(
        p for p in data_root.iterdir() if p.is_dir() and not p.name.startswith(".")
    )
    if args.limit and args.limit > 0:
        folders = folders[: args.limit]

    print(f"data_root: {data_root}")
    print(f"folders: {len(folders)}")
    print(f"out: {args.out.resolve()}")

    all_rows: list[dict[str, str]] = []

    for folder in folders:
        ocr_path, source = find_ocr_file(folder)
        if not ocr_path or source is None:
            print(f"  skip {folder.name}: no final1/prelim OCR")
            continue

        text = ocr_path.read_text(encoding="utf-8", errors="replace")
        hits = detect_dos_per_page(text, client, use_llm=use_llm and client is not None)
        rows = [hit_to_row(folder.name, hit) for hit in hits]
        all_rows.extend(rows)

        if args.per_chart:
            chart_csv = folder / "imaging" / f"{folder.name}_dos.csv"
            write_csv(chart_csv, rows)
            print(
                f"  {folder.name}: source={source} dos_hits={len(hits)} → {chart_csv.name}"
            )
        else:
            print(f"  {folder.name}: source={source} dos_hits={len(hits)}")

        for h in hits[:5]:
            print(
                f"      page={h.get('page_name')} "
                f"page=[{h.get('dos_from') or '—'}→{h.get('dos_to') or '—'}] "
                f"doc=[{h.get('doc_dos_from')}→{h.get('doc_dos_to')}]"
            )
        if len(hits) > 5:
            print(f"      … +{len(hits) - 5} more")

    write_csv(args.out.resolve(), all_rows)
    print(f"Wrote {len(all_rows)} row(s) → {args.out.resolve()}")
    print(f"Done — {len(folders)} folder(s) scanned.")


if __name__ == "__main__":
    main()
