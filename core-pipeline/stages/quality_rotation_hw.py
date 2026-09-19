"""Stage: rotation + page quality + handwritten/printed → ocr_quality_results.

Uses:
  * Tesseract OSD + geometric tilt (``stages.lib.imaging.rotation`` / ``osd``)
  * ConvNeXt HW classifier (``hw_printed``), with RF pickle fallback
  * Engineering quality analyzer (``quality_analyzer``) — real scores, not a placeholder

HW and quality run on the corrected page when one is written.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from config import (
    HW_MODEL_PATH,
    ROTATION_CORRECTION_ENABLED,
    STAGE_WORKERS,
    corrected_page_filename,
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
    """Load ConvNeXt (or RF fallback) once per process."""
    global _hw_model, _hw_model_loaded
    if _hw_model_loaded:
        return _hw_model
    with _model_lock:
        if _hw_model_loaded:
            return _hw_model
        try:
            from stages.lib.imaging.hw_printed import load_model

            path = HW_MODEL_PATH if HW_MODEL_PATH.is_file() else None
            _hw_model = load_model(path)
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
    """Return (printed|handwritten|mixed|uncertain, confidence, method)."""
    try:
        from stages.lib.imaging.hw_printed import classify_image_type

        model = _get_hw_model()
        label, conf, method = classify_image_type(image_path.read_bytes(), model=model)
        text = str(label).strip().lower()
        method_s = str(method or "model")
        if "uncertain" in text:
            return "uncertain", float(conf or 0.0), method_s
        if "mix" in text:
            return "mixed", float(conf or 0.0), method_s
        if "hand" in text:
            return "handwritten", float(conf or 0.0), method_s
        # RF historically: class 0 = Handwritten, 1 = Printed.
        if method_s not in {"convnext_tiny"} and label in (0, "0"):
            return "handwritten", float(conf or 0.0), method_s
        return "printed", float(conf or 0.0), method_s
    except Exception as exc:
        logger.warning("HW classify fallback for %s: %s", image_path, exc)
        return "printed", 0.5, "fallback"


def _measure_quality(image_path: Path) -> dict[str, Any]:
    """Run the engineering quality analyzer. Never raises."""
    try:
        import cv2

        from stages.lib.imaging.quality_analyzer import analyze_quality, read_embedded_dpi

        arr = cv2.imread(str(image_path))
        if arr is None:
            raise RuntimeError(f"Could not read image: {image_path}")
        dpi = read_embedded_dpi(image_path)
        result = analyze_quality(arr, dpi)
        return {
            "quality_tag": result.quality_tag,
            "quality_score": result.score_01,
            "quality_detail": result.detail_dict(),
            "input_dpi": result.input_dpi,
        }
    except Exception as exc:
        logger.warning("Quality analyzer failed for %s: %s", image_path, exc)
        return {
            "quality_tag": None,
            "quality_score": None,
            "quality_detail": {"error": str(exc)},
            "input_dpi": None,
        }


def _detect_rotation(image_path: Path) -> dict[str, Any]:
    try:
        import cv2

        image = cv2.imread(str(image_path))
        if image is None:
            raise RuntimeError(f"Could not read image: {image_path}")
        result = _get_detector().detect(image)
        tilt = float(result.get("tilt") or result.get("tilt_angle") or 0)
        mirrored = bool(result.get("mirror") or result.get("mirrored") or False)

        # Coarse rotation comes from Tesseract OSD, not from the geometric
        # detector. The detector recovered 0 of 6 sideways pages and reported
        # confidence 1.000 on the wrong answers; OSD was exact on all four
        # orientations. Tilt still comes from the detector, which measures it
        # well, and OSD says nothing about it.
        from stages.lib.imaging.osd import detect_rotation as osd_rotation

        osd = osd_rotation(image)
        if osd is not None:
            orientation = float(osd["rotation"])
            method = "osd"
            osd_confidence = float(osd["confidence"])
        else:
            # No OSD answer — a sparse page, or Tesseract without the osd
            # traineddata. Do NOT fall back to the detector's coarse rotation:
            # it is wrong more often than it is right, and a confidently wrong
            # rotation is worse than none. Leave the page unrotated.
            orientation = 0.0
            method = "osd_undecided"
            osd_confidence = 0.0

        return {
            "orientation_angle": orientation,
            "tilt_angle": tilt,
            "mirrored": mirrored,
            # Mirror is measured but never applied: the detector reported
            # mirror on 3 of 12 pages that were not mirrored, and flipping a
            # good page is strictly worse than leaving it. OSD cannot judge it.
            # Recorded so a real mirroring problem is still visible in the data.
            "needs_correction": bool(orientation != 0 or tilt != 0),
            "rotation_applied": False,
            "method": method,
            "osd_confidence": osd_confidence,
            # Kept so the correction can be applied at full resolution without
            # re-detecting. Not persisted.
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

QUALITY_COLS = [
    "chart_name",
    "page_name",
    "page_number",
    "quality_tag",
    "quality_score",
    "input_dpi",
]


def _write_corrected(
    chart_name: str, page_name: str, rot: dict[str, Any]
) -> Path | None:
    """Write a working image under corrected-pages/, or None when untouched.

    Written when:
      * rotation correction is enabled and the page needs it, or
      * the source is TIFF/TIF (always re-encoded as ``{stem}.jpg`` so later
        stages never open multi-page TIFF).
    """
    suffix = Path(page_name).suffix.lower()
    is_tiff = suffix in {".tif", ".tiff"}
    needs_rot = bool(
        ROTATION_CORRECTION_ENABLED and rot.get("needs_correction")
    )
    if not needs_rot and not is_tiff:
        return None

    image = rot.get("_image")
    if image is None:
        return None
    try:
        import cv2

        from stages.lib.imaging.rotation import correct_image

        out_img = image
        if needs_rot:
            out_img = correct_image(
                image,
                {
                    "rotation": int(rot["orientation_angle"]) % 360,
                    "tilt": float(rot["tilt_angle"]),
                    "mirror": False,
                },
            )
        dest_name = corrected_page_filename(page_name)
        dest = corrected_pages_dir(chart_name) / dest_name
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Force JPEG for TIFF sources (and keep jpeg quality sane).
        if dest.suffix.lower() in {".jpg", ".jpeg"}:
            ok = cv2.imwrite(str(dest), out_img, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        else:
            ok = cv2.imwrite(str(dest), out_img)
        if not ok:
            raise RuntimeError(f"cv2.imwrite returned False for {dest}")
        return dest
    except Exception as exc:
        logger.warning("Rotation/TIFF correction failed for %s: %s", page_name, exc)
        return None


def _measure(args: tuple[dict[str, Any], Path, str]) -> dict[str, Any]:
    page, image_path, chart_name = args
    try:
        rot = _detect_rotation(image_path)
        # Correct FIRST (and convert TIFF→JPG), then classify + score on the
        # image later stages will OCR.
        corrected = _write_corrected(chart_name, page["page_name"], rot)
        rot["rotation_applied"] = bool(
            corrected is not None
            and ROTATION_CORRECTION_ENABLED
            and rot.get("needs_correction")
        )
        rot.pop("needs_correction", None)
        scored_path = corrected or image_path
        hw_label, hw_conf, hw_method = _classify_hw(scored_path)
        quality = _measure_quality(scored_path)
        return {
            "page_id": page["id"],
            "page_name": page["page_name"],
            "page_number": page.get("page_number"),
            "rot": {k: v for k, v in rot.items() if not k.startswith("_")},
            "hw_label": hw_label,
            "hw_conf": hw_conf,
            "hw_method": hw_method,
            "quality": quality,
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


def _rewrite_csvs(conn: Any, chart_id: int, chart_name: str) -> tuple[Path, Path, Path]:
    """Rebuild CSVs from stored rows, so a resumed run stays consistent."""
    rows = conn.execute(
        """
        SELECT p.page_name, p.page_number, q.printed_or_handwritten,
               q.orientation_angle, q.tilt_angle, q.mirrored,
               q.rotation_applied, q.hw_confidence, q.hw_method,
               q.quality_tag, q.quality_score, q.input_dpi
          FROM ocr_quality_results q
          JOIN page_list p ON p.id = q.page_id
         WHERE q.chart_id = %s
         ORDER BY p.page_number NULLS LAST, p.page_name
        """,
        (chart_id,),
    ).fetchall()

    rotation_rows: list[dict[str, Any]] = []
    hw_rows: list[dict[str, Any]] = []
    quality_rows: list[dict[str, Any]] = []
    for row in rows:
        orientation = float(row["orientation_angle"] or 0)
        label = row["printed_or_handwritten"] or "printed"
        if label == "handwritten":
            hw_display = "Handwritten"
        elif label == "uncertain":
            hw_display = "Uncertain"
        elif label == "mixed":
            hw_display = "Mixed"
        else:
            hw_display = "Printed"
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
                "handwritten_label": hw_display,
                "confidence": row["hw_confidence"],
                "method": row["hw_method"] or "",
            }
        )
        quality_rows.append(
            {
                "chart_name": chart_name,
                "page_name": row["page_name"],
                "page_number": row["page_number"],
                "quality_tag": row["quality_tag"] or "",
                "quality_score": row["quality_score"],
                "input_dpi": row["input_dpi"],
            }
        )

    return (
        write_csv(imaging_csv(chart_name, "rotation"), ROTATION_COLS, rotation_rows),
        write_csv(imaging_csv(chart_name, "hw_printed"), HW_COLS, hw_rows),
        write_csv(imaging_csv(chart_name, "quality"), QUALITY_COLS, quality_rows),
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
                q = item.get("quality") or {}
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
                    quality_tag=q.get("quality_tag"),
                    quality_score=q.get("quality_score"),
                    quality_detail=q.get("quality_detail"),
                    input_dpi=q.get("input_dpi"),
                )
                mark_completed(conn, ctx, item["page_id"])

            rot_path, hw_path, quality_path = _rewrite_csvs(
                conn, chart_id, ctx.chart_name
            )

        return {
            "chart_id": chart_id,
            "rotation_csv": str(rot_path),
            "hw_csv": str(hw_path),
            "quality_csv": str(quality_path),
            "pages_done": ctx.done,
            "errors": ctx.errors,
        }
