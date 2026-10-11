"""Page-label annotation tool.

    python app.py <image folder>               open the tool on http://127.0.0.1:5055
    python app.py <image folder> --add-to-training   Add to Training JSON without the screen
                                                      (--generate is the old name, still accepted)

The folder (and its sub-folders) holds the page images; image_labels.csv sits
in its root. Runs locally only: bound to 127.0.0.1, no outside calls. Page
text never reaches the screen, the terminal or a log.
"""
from __future__ import annotations

import sys

# No __pycache__ beside the tool: on an external drive macOS adds a ._ file
# next to every file written there.
sys.dont_write_bytecode = True

import argparse  # noqa: E402
import io  # noqa: E402
import logging  # noqa: E402
import threading
from pathlib import Path
from typing import Optional

from flask import Flask, abort, jsonify, render_template, request, send_file
from PIL import Image, ImageOps

import ocr
from store import GenerateSummary, Store, filter_items

HERE = Path(__file__).resolve().parent
# Formats a browser cannot show: converted to JPEG on the way out.
CONVERT = {".heic", ".heif", ".tif", ".tiff", ".bmp"}
DISPLAY_MAX_SIDE = 2400


def create_app(store: Store) -> Flask:
    app = Flask(__name__, template_folder=str(HERE / "templates"))
    job: dict[str, object] = {"summary": GenerateSummary(), "thread": None}

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/taxonomy")
    def taxonomy():
        tax = store.taxonomy
        return jsonify(
            {
                "page_types": [
                    {"page_type": p, "codability": tax.codability_of(p),
                     "sub_types": tax.sub_types.get(p, [])}
                    for p in tax.page_types
                ],
                "model_types": tax.model_types,
                "embedded_types": tax.embedded_types,
                "progress_note": "Progress Note",
            }
        )

    @app.get("/api/items")
    def items():
        view = request.args.get("view", "all")
        model_type = request.args.get("model_type", "")
        search = request.args.get("search", "")
        show_completed = request.args.get("completed", "0") == "1"
        with store.lock:
            indexes = list(filter_items(store, view, model_type, search, show_completed))
            return jsonify(
                {"items": [store.item(i) for i in indexes], "counts": store.counts()}
            )

    @app.get("/api/image/<int:index>")
    def image(index: int):
        if index < 0 or index >= len(store.rows):
            abort(404)
        path = store.image_path(index)
        if path is None or not path.is_file():
            abort(404)
        if path.suffix.lower() not in CONVERT:
            return send_file(path)
        with Image.open(path) as opened:
            opened.seek(0)
            shown = ImageOps.exif_transpose(opened).convert("RGB")
        shown.thumbnail((DISPLAY_MAX_SIDE, DISPLAY_MAX_SIDE))
        buffer = io.BytesIO()
        shown.save(buffer, "JPEG", quality=88)
        buffer.seek(0)
        return send_file(buffer, mimetype="image/jpeg")

    @app.post("/api/label")
    def label():
        body = request.get_json(force=True) or {}
        index = int(body.get("index", -1))
        if index < 0 or index >= len(store.rows):
            abort(404)
        try:
            if body.get("clear"):
                item = store.clear(index)
            else:
                item = store.set_labels(
                    index,
                    str(body.get("page_type") or ""),
                    str(body.get("page_subtype") or ""),
                    embedded=bool(body.get("embedded")),
                )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"item": item, "counts": store.counts()})

    @app.post("/api/undo")
    def undo():
        item = store.undo()
        return jsonify({"item": item, "counts": store.counts()})

    @app.post("/api/generate")
    def generate():
        thread = job.get("thread")
        if isinstance(thread, threading.Thread) and thread.is_alive():
            return jsonify({"error": "already running"}), 409
        if store_needs_ocr(store):
            try:
                ocr.tesseract_version()
            except ocr.TesseractMissing as exc:
                return jsonify({"error": str(exc)}), 400
        summary = GenerateSummary(running=True)
        job["summary"] = summary

        def run() -> None:
            try:
                store.generate(summary=summary)
            except Exception as exc:  # reported through /api/generate
                summary.error = str(exc)
                summary.running = False

        thread = threading.Thread(target=run, daemon=True)
        job["thread"] = thread
        thread.start()
        return jsonify(summary.as_dict())

    @app.get("/api/generate")
    def generate_status():
        summary = job["summary"]
        assert isinstance(summary, GenerateSummary)
        return jsonify({**summary.as_dict(), "counts": store.counts()})

    return app


def store_needs_ocr(store: Store) -> bool:
    """True when some row waiting for training has no reusable text."""
    for index, row in enumerate(store.rows):
        if not store.tagged(row):
            continue
        if row.get("is_training_added") == "Yes" and not store._image_changed(index):
            continue
        path = store.image_path(index)
        record = store.records.get(row.get("image"))
        if path and (record is None or record.get("file_size") != path.stat().st_size):
            return True
    return False


def _print_summary(summary: GenerateSummary) -> None:
    print(f"rows waiting: {summary.candidates}")
    print(f"pages read (OCR): {summary.pages_read}")
    print(f"label-only changes: {summary.label_changes}")
    print(f"records in training_data.jsonl: {summary.records}")
    print(f"untagged rows skipped: {summary.skipped_untagged}")
    print(f"pages with almost no text: {len(summary.low_text)}")
    for name in summary.low_text:
        print(f"  {name}")
    print(f"pages that could not be read: {len(summary.unreadable)}")
    for name in summary.unreadable:
        print(f"  {name}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", nargs="?", help="image folder (holds image_labels.csv)")
    parser.add_argument("--add-to-training", "--generate", dest="generate", action="store_true",
                        help="Add to Training JSON and exit")
    parser.add_argument("--port", type=int, default=5055)
    args = parser.parse_args(argv)

    folder = args.folder or input("Image folder: ").strip().strip('"')
    store = Store(Path(folder))
    counts = store.counts()
    print(f"{store.root}: {counts['images']} images, {counts['untagged']} untagged, "
          f"{counts['in_training']} in training, {counts['not_in_training']} not in training yet")

    if args.generate:
        if store_needs_ocr(store):
            try:
                ocr.tesseract_version()
            except ocr.TesseractMissing as exc:
                print(exc, file=sys.stderr)
                return 2
        last = [0]

        def progress(summary: GenerateSummary) -> None:
            if summary.done - last[0] >= 10 or not summary.running:
                last[0] = summary.done
                print(f"  {summary.done}/{summary.candidates}")

        _print_summary(store.generate(progress=progress))
        return 0

    # Request lines carry only an index, never text; keep them quiet anyway.
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    print(f"Open http://127.0.0.1:{args.port}  (Ctrl+C to stop)")
    create_app(store).run(host="127.0.0.1", port=args.port, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
