"""Review page-classification tags against the page image.

Reads the label CSV and shows the scan from the processed training folder
(flat ``<page_id>.jpg`` files), falling back to ``review-ui/data/folders``.
Each Next confirms the current page. Every 10 confirms, and again when the
list ends, those pages are upserted by ``page_id`` into ``training_final.csv``.
The source CSV is left unchanged.

    python training/page-classification/label-review/server.py

Then open http://127.0.0.1:8090
"""
from __future__ import annotations

import argparse
import csv
import json
import mimetypes
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO = Path(__file__).resolve().parents[3]
DEFAULT_CSV = REPO / "training" / "page-classification" / "data-prep" / "training_page_classification.csv"
DEFAULT_TYPE_LIST = REPO / "training" / "page-classification" / "data-prep" / "mappings" / "page_type_list.csv"
_PROCESSED_IMAGES = Path("/Users/abhinavdasgupta/Desktop/Training/processed")
DEFAULT_IMAGES = _PROCESSED_IMAGES if _PROCESSED_IMAGES.is_dir() else REPO / "review-ui" / "data" / "folders"
PAGE = Path(__file__).with_name("index.html")
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp", ".bmp", ".heic")
FLUSH_EVERY = 10

_lock = threading.Lock()
_csv_path: Path
_final_path: Path
_images_root: Path
_fields: list[str]
_rows: list[dict[str, str]]
_final_rows: dict[str, dict[str, str]]
_pushes_since_write: int
_catalog: list[dict[str, str]]


def _chart_names(chart_id: str) -> list[str]:
    name = chart_id.strip()
    names = [name]
    if not name.lower().endswith(".tif"):
        names.append(f"{name}.tif")
    return names


def find_image(row: dict[str, str]) -> Path | None:
    chart = row.get("chart_id", "").strip()
    index = row.get("page_index", "").strip()
    page_id = row.get("page_id", "").strip()
    stems = [index, page_id, f"Pg{index}"]
    roots = [_images_root / name for name in _chart_names(chart)]
    roots.append(_images_root)
    for root in roots:
        folders = [root / "pages", root / "corrected-pages", root]
        for folder in folders:
            for stem in stems:
                if not stem:
                    continue
                for ext in IMAGE_EXTS:
                    path = _image_file(folder, stem, ext)
                    if path is None:
                        continue
                    try:
                        path.resolve().relative_to(_images_root.resolve())
                    except ValueError:
                        continue
                    return path
    return None


def _image_file(folder: Path, stem: str, ext: str) -> Path | None:
    """A scan named ``stem`` with this extension, either case."""
    for name in (f"{stem}{ext}", f"{stem}{ext.upper()}"):
        path = folder / name
        if path.is_file() and not path.name.startswith("._"):
            return path
    return None


def image_response(path: Path) -> tuple[bytes, str]:
    """Bytes and content type for the review page. HEIC is sent as JPEG."""
    if path.suffix.lower() != ".heic":
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        return path.read_bytes(), mime
    return _heic_jpeg(path), "image/jpeg"


def _heic_jpeg(path: Path) -> bytes:
    cache = Path(tempfile.gettempdir()) / "label-review-heic" / f"{path.stem}.jpg"
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.is_file() and cache.stat().st_mtime >= path.stat().st_mtime:
        return cache.read_bytes()
    tool = shutil.which("heif-convert")
    if tool is None:
        raise RuntimeError("heif-convert is not on PATH; cannot display HEIC")
    tmp = cache.with_name(f"{path.stem}.tmp.jpg")
    subprocess.run(
        [tool, "-q", "75", str(path), str(tmp)],
        check=True,
        capture_output=True,
    )
    tmp.replace(cache)
    return cache.read_bytes()


def load_rows(path: Path) -> None:
    global _fields, _rows
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise SystemExit(f"{path} has no header row")
        _fields = list(reader.fieldnames)
        _rows = [
            {key: (value or "").strip() for key, value in row.items() if key is not None}
            for row in reader
            if (row.get("page_id") or "").strip()
        ]


def load_final(path: Path) -> None:
    global _final_rows, _pushes_since_write
    _pushes_since_write = 0
    if not path.is_file():
        _final_rows = {}
        return
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        _final_rows = {}
        for raw in reader:
            page_id = (raw.get("page_id") or "").strip()
            if not page_id:
                continue
            _final_rows[page_id] = {key: (raw.get(key) or "").strip() for key in _fields}
    for row in _rows:
        saved = _final_rows.get(row.get("page_id", ""))
        if saved is None:
            continue
        row["page_family"] = saved.get("page_family", "")
        row["page_type"] = saved.get("page_type", "")


def ordered_final_rows() -> list[dict[str, str]]:
    ordered: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in _rows:
        page_id = row.get("page_id", "")
        saved = _final_rows.get(page_id)
        if saved is None:
            continue
        ordered.append(saved)
        seen.add(page_id)
    ordered.extend(saved for page_id, saved in _final_rows.items() if page_id not in seen)
    return ordered


def write_final() -> None:
    tmp = _final_path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(ordered_final_rows())
    tmp.replace(_final_path)


def confirm_page(page_id: str, page_family: str, page_type: str, flush: bool) -> dict[str, str | bool | int]:
    """Remember one Next. Rewrite training_final.csv every 10, or when flush is set."""
    global _pushes_since_write
    row = next((item for item in _rows if item.get("page_id") == page_id), None)
    if row is None:
        raise KeyError(page_id)
    changed = False
    for key, value in (("page_family", page_family), ("page_type", page_type)):
        if (row.get(key) or "") != value:
            row[key] = value
            changed = True
    _final_rows[page_id] = {key: row.get(key, "") for key in _fields}
    _pushes_since_write += 1
    flushed = _pushes_since_write >= FLUSH_EVERY or flush
    if flushed:
        write_final()
        _pushes_since_write = 0
        print(f"Wrote {len(_final_rows)} pages -> {_final_path}")
    payload = row_payload(row)
    payload["changed"] = changed
    payload["flushed"] = flushed
    payload["final_count"] = len(_final_rows)
    return payload


def row_payload(row: dict[str, str]) -> dict[str, str | bool]:
    image = find_image(row)
    payload: dict[str, str | bool] = dict(row)
    payload["has_image"] = image is not None
    return payload


def load_type_catalog(path: Path) -> None:
    """Page types from the list, plus every family so a family can be chosen as the type."""
    global _catalog
    if not path.is_file():
        _catalog = []
        return
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        by_type: dict[str, dict[str, str]] = {}
        families: dict[str, str] = {}
        for raw in reader:
            page_type = (raw.get("page_type") or "").strip()
            family = (raw.get("page_family") or "").strip()
            if not page_type or not family:
                continue
            families[family] = family
            if page_type not in by_type:
                by_type[page_type] = {"page_type": page_type, "page_family": family}
        for family in families:
            if family not in by_type:
                by_type[family] = {"page_type": family, "page_family": family}
    _catalog = [by_type[name] for name in sorted(by_type, key=str.casefold)]


def catalog_families() -> list[str]:
    names = {item["page_family"] for item in _catalog}
    names.update(row.get("page_family", "").strip() for row in _rows)
    return sorted(name for name in names if name)


def choices() -> dict[str, list[str]]:
    def unique(name: str) -> list[str]:
        return sorted({row.get(name, "").strip() for row in _rows if row.get(name, "").strip()})

    return {
        "page_family": unique("page_family"),
        "page_type": unique("page_type"),
        "codeability": unique("codeability"),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} {fmt % args}")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: object) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(200, PAGE.read_bytes(), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/labels":
            with _lock:
                rows = [row_payload(row) for row in _rows]
                payload = {
                    "fields": _fields,
                    "rows": rows,
                    "catalog": _catalog,
                    **choices(),
                }
                payload["page_family"] = catalog_families()
            self._json(200, payload)
            return
        if parsed.path == "/api/image":
            page_id = (parse_qs(parsed.query).get("page_id") or [""])[0]
            with _lock:
                row = next((item for item in _rows if item.get("page_id") == page_id), None)
                image = find_image(row) if row else None
            if image is None:
                self._json(404, {"detail": "No image for this page"})
                return
            try:
                body, mime = image_response(image)
            except (OSError, subprocess.CalledProcessError, RuntimeError) as exc:
                self._json(500, {"detail": str(exc)})
                return
            self._send(200, body, mime)
            return
        self._json(404, {"detail": "Not found"})

    def do_POST(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/labels":
            self._json(404, {"detail": "Not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"detail": "Expected JSON"})
            return
        page_id = str(body.get("page_id") or "").strip()
        if not page_id:
            self._json(400, {"detail": "page_id is required"})
            return
        page_family = str(body.get("page_family") or "").strip()
        page_type = str(body.get("page_type") or "").strip()
        flush = bool(body.get("flush"))
        with _lock:
            try:
                payload = confirm_page(page_id, page_family, page_type, flush)
            except KeyError:
                self._json(404, {"detail": f"Unknown page_id {page_id}"})
                return
        self._json(200, payload)


def main() -> None:
    global _csv_path, _final_path, _images_root
    parser = argparse.ArgumentParser(description="Review page-classification tags on the page image.")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--types", type=Path, default=DEFAULT_TYPE_LIST)
    parser.add_argument("--final", type=Path, default=None)
    parser.add_argument("--images", type=Path, default=DEFAULT_IMAGES)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    _csv_path = args.csv.resolve()
    _final_path = (args.final or _csv_path.with_name("training_final.csv")).resolve()
    _images_root = args.images.resolve()
    if not _csv_path.is_file():
        raise SystemExit(f"CSV not found: {_csv_path}")
    if not _images_root.is_dir():
        raise SystemExit(f"Image folder not found: {_images_root}")
    load_rows(_csv_path)
    load_final(_final_path)
    load_type_catalog(args.types.resolve())
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Label review: http://{args.host}:{args.port}")
    print(f"CSV: {_csv_path}")
    print(f"Final: {_final_path} (every {FLUSH_EVERY} Next)")
    print(f"Types: {args.types.resolve()} ({len(_catalog)} choices)")
    print(f"Images: {_images_root}")
    server.serve_forever()


if __name__ == "__main__":
    main()
