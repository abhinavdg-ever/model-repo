"""Stage: rotation + handwritten/printed quality → ocr_quality_results + CSVs.

Uses the rotation detector and handwriting classifier in ``stages/lib/imaging``
(``rotation.PageOrientationDetector``, ``hw_printed.classify_image_type``).

Both the handwriting model and the orientation detector are built once per
process and reused. v6 rebuilt them per page, which meant unpickling the
classifier once for every page in the chart.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

from config import (
    HW_MODEL_PATH,
    ROTATION_CORRECTION_ENABLED,
    STAGE_WORKERS,
    corrected_pages_dir,
    pages_dir,
)
from db import connect, upsert_quality
from db.paths import imaging_csv, write_csv
from stages._support import mark_completed, mark_failed, mark_processing, stage_run

logger = logging.getLogger(__name__)

STAGE = "ocr_quality"

_model_lock = threading.Lock()
_hw_model: Any = None
_hw_model_loaded = False
_detector: Any = None


def _get_hw_model() -> Any:
    """Load the handwriting classifier once per process."""
    global _hw_model, _hw_model_loaded
    if _hw_model_loaded:
        return _hw_model
    with _model_lock:
        if _hw_model_loaded:
            return _hw_model
        try:
            from stages.lib.imaging.hw_printed import load_model

            _hw_model = load_model(HW_MODEL_PATH if HW_MODEL_PATH.is_file() else None)
        except Exception as exc:
            logger.warning("Handwriting model unavailable (%s); using fallback", exc)
            _hw_model = None
        _hw_model_loaded = True
        return _hw_model


def _get_detector() -> Any:
    """Build the orientation detector once per process."""
    global _detector
    if _detector is not None:
        return _detector
    with _model_lock:
        if _detector is None:
            from stages.lib.imaging.rotation import PageOrientationDetector

            _detector = PageOrientationDetector()
        return _detector


def _classify_hw(image_path: Path) -> tuple[str, float, str]:
    """Return (printed|handwritten, confidence, method)."""
    try:
        from stages.lib.imaging.hw_printed import classify_image_type

        model = _get_hw_model()
        label, conf, method = classify_image_type(image_path.read_bytes(), model=model)
        text = str(label).strip().lower()
        if "hand" in text or label in (0, "0"):
            return "handwritten", float(conf or 0.0), str(method or "model")
        return "printed", float(conf or 0.0), str(method or "model")
    except Exception as exc:
        logger.warning("HW classify fallback for %s: %s", image_path, exc)
        return "printed", 0.5, "fallback"


def _detect_rotation(image_path: Path) -> dict[str, Any]:
    try:
        import cv2

        image = cv2.imread(str(image_path))
        if image is None:
            raise RuntimeError(f"Could not read image: {image_path}")
        result = _get_detector().detect(image)
        orientation = (
            result.get("rotation")
            or result.get("orientation_angle")
            or result.get("rotation_deg")
            or 0
        )
        tilt = result.get("tilt") or result.get("tilt_angle") or 0
        mirrored = bool(result.get("mirror") or result.get("mirrored") or False)
        applied = bool(
            float(orientation) != 0
            or float(tilt) != 0
            or mirrored
            or result.get("needs_correction")
        )
        return {
            "orientation_angle": float(orientation),
            "tilt_angle": float(tilt),
            "mirrored": mirrored,
            # Whether a correction LOOKS needed, from the measurement alone.
            # Distinct from rotation_applied, which records whether one was
            # actually written — they differ whenever correction is disabled.
            "needs_correction": applied,
            "rotation_applied": False,
            "method": "detector",
            # The detector's own result object, kept so the correction can be
            # applied at full resolution without re-detecting. Not persisted.
            "_result": result,
            "_image": image,
        }
    except Exception as exc:
        logger.warning("Rotation detect fallback for %s: %s", image_path, exc)
        return {
            "orientation_angle": 0.0,
            "tilt_angle": 0.0,
            "mirrored": False,
            "needs_correction": False,
            "rotation_applied": False,
            "method": "fallback",
        }


ROTATION_COLS = [
    "chart_name",
    "page_name",
    "page_number",
    "orientation",
    "rotation_deg",
    "tilt_angle",
    "mirrored",
    "rotation_applied",
]

HW_COLS = [
    "chart_name",
    "page_name",
    "page_number",
    "handwritten_or_printed",
    "handwritten_label",
    "confidence",
    "method",
]


def _write_corrected(
    chart_name: str, page_name: str, rot: dict[str, Any]
) -> Path | None:
    """Write the corrected page, or None when the scan is already upright.

    Only pages that actually change are written, so corrected-pages/ holds
    exactly the pages that were altered rather than a second copy of the chart.
    """
    if not ROTATION_CORRECTION_ENABLED:
        return None
    if not rot.get("needs_correction"):
        return None
    result, image = rot.get("_result"), rot.get("_image")
    if result is None or image is None:
        return None
    try:
        import cv2

        corrected = _get_detector().correct(image, result)
        dest = corrected_pages_dir(chart_name) / page_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(dest), corrected):
            raise RuntimeError(f"cv2.imwrite returned False for {dest}")
        return dest
    except Exception as exc:
        # A page that cannot be corrected is still a page: fall back to the
        # original rather than failing it, and record that we did.
        logger.warning("Rotation correction failed for %s: %s", page_name, exc)
        return None


def _measure(args: tuple[dict[str, Any], Path, str]) -> dict[str, Any]:
    page, image_path, chart_name = args
    try:
        rot = _detect_rotation(image_path)
        # Correct FIRST, then classify: handwriting detection on a sideways
        # page is measurably worse, and every stage after this one reads the
        # corrected image, so the classifier should see what they see.
        corrected = _write_corrected(chart_name, page["page_name"], rot)
        # The column means "a corrected image exists for this page", not
        # "this page looked crooked" — that stays in the angle columns.
        rot["rotation_applied"] = corrected is not None
        rot.pop("needs_correction", None)
        hw_label, hw_conf, hw_method = _classify_hw(corrected or image_path)
        return {
            "page_id": page["id"],
            "page_name": page["page_name"],
            "page_number": page.get("page_number"),
            "rot": {k: v for k, v in rot.items() if not k.startswith("_")},
            "hw_label": hw_label,
            "hw_conf": hw_conf,
            "hw_method": hw_method,
            "error": "",
        }
    except Exception as exc:
        logger.exception("Quality failed for %s", page["page_name"])
        return {
            "page_id": page["id"],
            "page_name": page["page_name"],
            "page_number": page.get("page_number"),
            "error": str(exc),
        }


def _rewrite_csvs(conn: Any, chart_id: int, chart_name: str) -> tuple[Path, Path]:
    """Rebuild both CSVs from stored rows, so a resumed run stays consistent."""
    rows = conn.execute(
        """
        SELECT p.page_name, p.page_number, q.printed_or_handwritten,
               q.orientation_angle, q.tilt_angle, q.mirrored,
               q.rotation_applied, q.hw_confidence, q.hw_method
          FROM ocr_quality_results q
          JOIN page_list p ON p.id = q.page_id
         WHERE q.chart_id = %s
         ORDER BY p.page_number NULLS LAST, p.page_name
        """,
        (chart_id,),
    ).fetchall()

    rotation_rows: list[dict[str, Any]] = []
    hw_rows: list[dict[str, Any]] = []
    for row in rows:
        orientation = float(row["orientation_angle"] or 0)
        label = row["printed_or_handwritten"] or "printed"
        rotation_rows.append(
            {
                "chart_name": chart_name,
                "page_name": row["page_name"],
                "page_number": row["page_number"],
                "orientation": str(int(orientation)),
                "rotation_deg": orientation,
                "tilt_angle": float(row["tilt_angle"] or 0),
                "mirrored": bool(row["mirrored"]),
                "rotation_applied": bool(row["rotation_applied"]),
            }
        )
        hw_rows.append(
            {
                "chart_name": chart_name,
                "page_name": row["page_name"],
                "page_number": row["page_number"],
                "handwritten_or_printed": label,
                "handwritten_label": "Handwritten" if label == "handwritten" else "Printed",
                "confidence": row["hw_confidence"],
                "method": row["hw_method"] or "",
            }
        )

    return (
        write_csv(imaging_csv(chart_name, "rotation"), ROTATION_COLS, rotation_rows),
        write_csv(imaging_csv(chart_name, "hw_printed"), HW_COLS, hw_rows),
    )


def run(chart_id: int, *, force: bool = False) -> dict[str, Any]:
    with stage_run(chart_id, STAGE, force=force) as ctx:
        todo = ctx.pages_todo
        root = pages_dir(ctx.chart_name)

        with connect() as conn:
            for page in todo:
                mark_processing(conn, ctx, page["id"])

        measured: list[dict[str, Any]] = []
        if todo:
            workers = max(1, min(STAGE_WORKERS, len(todo)))
            # Warm the shared model/detector before fanning out, so workers do
            # not race to build them.
            _get_hw_model()
            with ThreadPoolExecutor(max_workers=workers) as pool:
                measured = list(
                    pool.map(
                        _measure,
                        [(p, root / p["page_name"], ctx.chart_name) for p in todo],
                    )
                )

        with connect() as conn:
            for item in measured:
                if item.get("error"):
                    mark_failed(
                        conn, ctx, item["page_id"], item["error"], item["page_name"]
                    )
                    continue
                rot = item["rot"]
                upsert_quality(
                    conn,
                    chart_id=chart_id,
                    page_id=item["page_id"],
                    printed_or_handwritten=item["hw_label"],
                    orientation_angle=rot["orientation_angle"],
                    tilt_angle=rot["tilt_angle"],
                    mirrored=rot["mirrored"],
                    rotation_applied=rot["rotation_applied"],
                    hw_method=item["hw_method"],
                    hw_confidence=item["hw_conf"],
                )
                mark_completed(conn, ctx, item["page_id"])

            rot_path, hw_path = _rewrite_csvs(conn, chart_id, ctx.chart_name)

        return {
            "chart_id": chart_id,
            "rotation_csv": str(rot_path),
            "hw_csv": str(hw_path),
            "pages_done": ctx.done,
            "errors": ctx.errors,
        }
