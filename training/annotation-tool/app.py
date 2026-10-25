"""Page-label annotation tool (taxonomy v29.2).

    python app.py <label folder>               open the tool on http://127.0.0.1:5055
    python app.py <label folder> --add-to-training   Add to Training JSON without the screen
                                                      (--generate is the old name, still accepted)
    python app.py <label folder> --migrate --dry-run  old label names: counts, rows to decide
    python app.py <label folder> --migrate            rename them (never the split ones)

The label folder holds image_labels.csv and training_data.jsonl, and may hold
the page images (in sub-folders too). On the SSD layout the images are the OCR
copies in ../ocr-jpeg, found without a flag (--images names another folder),
and the encounter files are processed/encounter_review.csv and
../encounter/encounter_tags.jsonl. Runs locally only: bound to 127.0.0.1, no
outside calls. Page text never reaches the screen, the terminal or a log.
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
from collections import Counter
from pathlib import Path
from typing import Optional

from flask import Flask, abort, jsonify, render_template, request, send_file
from PIL import Image, ImageOps

import encounter
import ocr
from check_taxonomy import print_sources
from store import CONVERT_SUFFIXES, NAME_MAP_PATH, VIEWS, GenerateSummary, Store, filter_items
from taxonomy import TAXONOMY_PATH, Taxonomy

HERE = Path(__file__).resolve().parent
# Formats a browser cannot show: converted to JPEG on the way out.
CONVERT = CONVERT_SUFFIXES
DISPLAY_MAX_SIDE = 2400


def create_app(store: Store) -> Flask:
    app = Flask(__name__, template_folder=str(HERE / "templates"))
    job: dict[str, object] = {"summary": GenerateSummary(), "thread": None}

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/taxonomy")
    def taxonomy():
        # The whole label space: the page reads every name from here.
        encounters = store.encounters
        return jsonify({**store.taxonomy.data,
                        "encounter_types": encounters.classes if encounters.enabled else []})

    @app.get("/api/items")
    def items():
        view = request.args.get("view", "all")
        if view not in VIEWS:
            abort(400)
        l1 = request.args.get("l1", "")
        search = request.args.get("search", "")
        show_completed = request.args.get("completed", "0") == "1"
        with store.lock:
            indexes = list(filter_items(store, view, l1, search, show_completed))
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
        text = lambda key: str(body.get(key) or "")  # noqa: E731
        try:
            item = store.set_labels(
                index, text("L1"), text("L2"), text("L3"), text("specialty"),
                override=text("codeability_override"),
                override_reason=text("codeability_override_reason"),
                resolution=text("resolution"),
                encounter_type=text("encounter_type") or None,
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
        if store.needs_ocr():
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


def _print_checks(store: Store) -> None:
    """The work queues (file names and counts only)."""
    c = store.counts()
    print(f"Queues: {c['disagreements']} disagreement(s) to review, {c['invalid']} label(s) not valid "
          f"under the taxonomy, {c['needs_review']} needs_review, {c['specialty_other']} specialty "
          f"'{store.taxonomy.specialty_other}', {c['claude_reviewed']} claude-reviewed")
    reasons = Counter(store.invalid.values())
    for reason, n in reasons.most_common(10):
        print(f"  {n:>4}  {reason}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", nargs="?", help="label folder (holds image_labels.csv)")
    parser.add_argument("--add-to-training", "--generate", dest="generate", action="store_true",
                        help="Add to Training JSON and exit")
    parser.add_argument("--port", type=int, default=5055)
    parser.add_argument("--images", type=Path, help="folder of page images found by stem "
                        "(default: ../ocr-jpeg beside the label folder, when it exists)")
    parser.add_argument("--taxonomy", type=Path, default=TAXONOMY_PATH)
    parser.add_argument("--label-sets", type=Path, help="default: label_sets.json in the label folder, if any")
    parser.add_argument("--encounter-tags", type=Path, help="default: encounter_tags.jsonl in the label "
                        "folder, else ../encounter/encounter_tags.jsonl")
    parser.add_argument("--encounter-review", type=Path, help="default: encounter_review.csv in the label folder")
    parser.add_argument("--encounter-canon", type=Path, help="the encounter classes (default: "
                        "encounter_canon.json in the label folder, else the repo copy)")
    parser.add_argument("--migrate", action="store_true", help="rename old label names with the name map and exit")
    parser.add_argument("--dry-run", action="store_true", help="with --migrate: report only, write nothing")
    parser.add_argument("--name-map", type=Path, default=NAME_MAP_PATH)
    args = parser.parse_args(argv)

    folder = Path(args.folder or input("Label folder: ").strip().strip('"'))
    found = encounter.locate(folder.expanduser().resolve())
    encounters = encounter.Encounters(
        args.encounter_tags or found["tags"], args.encounter_review or found["review"],
        args.encounter_canon or found["canon"])
    store = Store(folder, Taxonomy(args.taxonomy), images=args.images, encounters=encounters,
                  label_sets=args.label_sets, name_map=args.name_map)
    counts = store.counts()
    print_sources(store)
    print(f"{store.root}: {counts['rows']} rows, {counts['images']} with an image, {counts['untagged']} untagged, "
          f"{counts['in_training']} in training, {counts['not_in_training']} not in training yet")
    _print_checks(store)

    if args.migrate:
        report = store.migrate(apply=not args.dry_run)
        report.print()
        return 0

    if args.generate:
        if store.needs_ocr():
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
