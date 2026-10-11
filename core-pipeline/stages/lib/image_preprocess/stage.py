"""Stage: rotation + page quality + handwritten/printed → ocr_quality_results.

Uses:
  * Tesseract OSD + geometric tilt (``stages.lib.image_preprocess.rotation`` / ``osd``)
  * ConvNeXt HW classifier (``hw_printed``), with RF pickle fallback.
    Empty sheets are uncertain (``blank_page``) and never reach the model.
    Faint marks are uncertain. Spread ink can upgrade printed to handwritten.
  * Engineering quality analyzer (``quality_analyzer``) — real scores, not a placeholder
  * Label post-process: Handwritten + High → Medium (score unchanged)
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from config import (
    MAX_TILT_TO_APPLY,
    ROTATION_CORRECTION_ENABLED,
    STAGE_WORKERS,
    corrected_page_filename,
    corrected_pages_dir,
    page_image_source,
    pages_dir,
)
from db import connect, set_page_image_source, upsert_quality
from db.paths import imaging_csv, write_csv
from stages._support import mark_completed, mark_failed, mark_processing, stage_run

logger = logging.getLogger(__name__)

STAGE = "ocr_quality"


def _classify_hw(image_path: Path) -> tuple[str, float | None, str, dict[str, Any]]:
    """Return (printed|handwritten|mixed|uncertain, confidence, method, page tags).

    The model is loaded inside classify_page, after the blank check, so an
    empty page does not load ConvNeXt. Page tags are the ocr_quality_results
    page-type columns; visibility and area come from the page-tag model only.
    """
    try:
        from stages.lib.image_preprocess.hw_printed import classify_page

        page = classify_page(image_path.read_bytes())
        label, conf, method = page.as_tuple()
        text = str(label).strip().lower()
        method_s = str(method or "model")
        conf_out = None if conf is None else float(conf)
        if "uncertain" in text:
            hw_label = "uncertain"
        elif "mix" in text:
            hw_label = "mixed"
        elif "hand" in text:
            hw_label = "handwritten"
        # RF historically: class 0 = Handwritten, 1 = Printed.
        elif method_s not in {
            "convnext_tiny",
            "convnext_page_tags",
            "page_convnext_plus_ink",
            "blank_page",
            "faint_marks_only",
        } and label in (0, "0"):
            hw_label = "handwritten"
            conf_out = conf_out if conf_out is not None else 0.0
        else:
            hw_label = "printed"
        tags = page.tags()
        tags["document_type"] = tags["document_type"] or hw_label
        return hw_label, conf_out, method_s, tags
    except Exception as exc:
        logger.warning("HW classify fallback for %s: %s", image_path, exc)
        return "printed", 0.5, "fallback", {"document_type": "printed"}


def _review_required(quality_tag: str | None, tags: dict[str, Any]) -> bool:
    """Low quality, uncertain page type, or not visible.

    A blank sheet's sharpness and contrast say nothing, so its quality is ignored.
    """
    document_type = tags.get("document_type")
    low_quality = quality_tag == "low" and document_type != "blank"
    return low_quality or document_type == "uncertain" or tags.get("is_visible") is False


def _measure_quality(image_path: Path) -> dict[str, Any]:
    """Run the engineering quality analyzer. Never raises."""
    try:
        import cv2

        from stages.lib.image_preprocess.quality_analyzer import analyze_quality, read_embedded_dpi

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


def _apply_quality_postprocess(
    quality: dict[str, Any],
    document_type: str | None,
) -> dict[str, Any]:
    """Handwritten type + High → Medium (teammate image_preprocessing rule)."""
    from stages.lib.image_preprocess.quality_label_postprocess import (
        apply_quality_label_postprocess,
    )

    tag = apply_quality_label_postprocess(
        quality_tag=quality.get("quality_tag"),
        quality_score=quality.get("quality_score"),
        document_type=document_type,
    )
    if tag and tag != quality.get("quality_tag"):
        detail = dict(quality.get("quality_detail") or {})
        detail["label_postprocess"] = (
            f"{quality.get('quality_tag')}→{tag} (handwritten)"
        )
        quality = {**quality, "quality_tag": tag, "quality_detail": detail}
    return quality


def _detect_rotation(image_path: Path) -> dict[str, Any]:
    try:
        import cv2

        image = cv2.imread(str(image_path))
        if image is None:
            raise RuntimeError(f"Could not read image: {image_path}")
        # Coarse rotation comes from Tesseract OSD, not from the geometric
        # detector. The detector recovered 0 of 6 sideways pages and reported
        # confidence 1.000 on the wrong answers; OSD was exact on all four
        # orientations.
        from stages.lib.image_preprocess.osd import detect_rotation as osd_rotation
        from stages.lib.image_preprocess.rotation import (
            page_for_reading,
            orientation_that_reads,
            tilt_on_upright,
            tilt_that_reads,
        )

        osd = osd_rotation(image)
        if osd is not None:
            proposed = int(osd["rotation"])
            method = "osd"
            osd_confidence = float(osd["confidence"])
        else:
            # No OSD answer — a sparse page, or Tesseract without the osd
            # traineddata. Do NOT fall back to the detector's coarse rotation:
            # it is wrong more often than it is right, and a confidently wrong
            # rotation is worse than none. Start from the page as it arrived.
            proposed = 0
            method = "osd_undecided"
            osd_confidence = 0.0

        # Every correction is kept only if Tesseract reads the page better
        # with it. Turn and mirror first: OSD's turn stands when the page
        # reads at it; otherwise the turn (and, failing that, the flip) that
        # reads best is kept, and a page nothing reads keeps OSD's turn. An
        # empty sheet has nothing to read and keeps OSD's answer.
        orientation, mirrored = float(proposed), False
        _hw_label, _hw_conf, _hw_method, tags = _classify_hw(image_path)
        small = None
        if tags.get("document_type") != "blank":
            small = page_for_reading(image)
            turn, mirrored, chosen_read = orientation_that_reads(small, proposed)
            orientation = float(turn)
            if turn != proposed or mirrored:
                method = "readability"

        # Tilt is measured on the page as it will be saved: after the turn and
        # after the flip, which reverses the direction of any lean. The
        # geometric detector's tilt is measured after its own coarse guess
        # (often 270° on an upright page) and applying that number here
        # rotates a level scan. Like the turn, it is kept only when the
        # straightened page reads at least as well as the unstraightened one.
        tilt = tilt_on_upright(image, int(orientation), mirrored=mirrored)
        if small is not None:
            tilt = tilt_that_reads(
                small,
                int(orientation),
                mirrored,
                tilt,
                chosen_read,
                max_tilt=MAX_TILT_TO_APPLY,
            )

        return {
            "orientation_angle": orientation,
            "tilt_angle": tilt,
            "mirrored": mirrored,
            # A tilt over the limit is recorded but not applied, so on its
            # own it does not make the page need a corrected copy.
            "needs_correction": bool(
                orientation != 0
                or mirrored
                or 0 < abs(tilt) <= MAX_TILT_TO_APPLY
            ),
            "rotation_applied": False,
            "method": method,
            "osd_confidence": osd_confidence,
            # Kept so the correction can be applied at full resolution without
            # re-reading the file. Not persisted.
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
    "document_type",
    "handwritten_probability",
    "is_visible",
    "handwritten_area_pct",
]

QUALITY_COLS = [
    "chart_name",
    "page_name",
    "page_number",
    "quality_tag",
    "quality_score",
    "input_dpi",
    "review_required",
]


def _clear_corrected(chart_name: str, page_name: str) -> None:
    """Remove this page's corrected image from an earlier run.

    Every later stage reads a corrected image whenever one exists
    (``config.page_image_path``), so a file left over from a run whose turn,
    flip or tilt has since been undone would keep being OCR'd.
    """
    cdir = corrected_pages_dir(chart_name)
    for name in {corrected_page_filename(page_name), page_name}:
        (cdir / name).unlink(missing_ok=True)


def _write_corrected(
    chart_name: str, page_name: str, rot: dict[str, Any]
) -> Path | None:
    """Write the corrected page under corrected-pages/, or None when untouched.

    Called only after the readability checks, so ``rot`` holds the turn, flip
    and tilt that survived them. A page is written only when correction is
    enabled and one of those changes it; otherwise corrected-pages/ holds
    nothing for it and later stages read pages/. A TIFF source that needs
    correcting is saved as ``{stem}.jpg``.
    """
    _clear_corrected(chart_name, page_name)
    if not (ROTATION_CORRECTION_ENABLED and rot.get("needs_correction")):
        return None

    image = rot.get("_image")
    if image is None:
        return None
    try:
        import cv2

        from stages.lib.image_preprocess.rotation import correct_image

        out_img = correct_image(
            image,
            {
                "rotation": int(rot["orientation_angle"]) % 360,
                "tilt": float(rot["tilt_angle"]),
                "mirror": bool(rot["mirrored"]),
            },
            max_tilt_abs=MAX_TILT_TO_APPLY,
        )
        dest = corrected_pages_dir(chart_name) / corrected_page_filename(page_name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.suffix.lower() in {".jpg", ".jpeg"}:
            ok = cv2.imwrite(str(dest), out_img, [int(cv2.IMWRITE_JPEG_QUALITY), 92])
        else:
            ok = cv2.imwrite(str(dest), out_img)
        if not ok:
            raise RuntimeError(f"cv2.imwrite returned False for {dest}")
        return dest
    except Exception as exc:
        logger.warning("Rotation correction failed for %s: %s", page_name, exc)
        return None


def _measure(args: tuple[dict[str, Any], Path, str]) -> dict[str, Any]:
    page, image_path, chart_name = args
    try:
        rot = _detect_rotation(image_path)
        # Correct FIRST, then classify + score on the image later stages
        # will OCR.
        corrected = _write_corrected(chart_name, page["page_name"], rot)
        rot["rotation_applied"] = corrected is not None
        rot.pop("needs_correction", None)
        scored_path = corrected or image_path
        use_corrected, image_relpath = page_image_source(chart_name, page["page_name"])
        hw_label, hw_conf, hw_method, tags = _classify_hw(scored_path)
        quality = _apply_quality_postprocess(
            _measure_quality(scored_path), tags.get("document_type")
        )
        page_type = {
            **tags,
            "review_required": _review_required(quality.get("quality_tag"), tags),
        }
        return {
            "page_id": page["id"],
            "page_name": page["page_name"],
            "page_number": page.get("page_number"),
            "rot": {k: v for k, v in rot.items() if not k.startswith("_")},
            "hw_label": hw_label,
            "hw_conf": hw_conf,
            "hw_method": hw_method,
            "quality": quality,
            "page_type": page_type,
            "use_corrected": use_corrected,
            "image_path": image_relpath,
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
    from db import list_quality_for_csv

    rows = list_quality_for_csv(conn, chart_id)

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
                "document_type": row.get("document_type") or "",
                "handwritten_probability": row.get("handwritten_probability"),
                "is_visible": row.get("is_visible"),
                "handwritten_area_pct": row.get("handwritten_area_pct"),
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
                "review_required": row.get("review_required"),
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
        # mark_processing leaves the last page in the log context. The pool
        # below is the whole chart, not page 29.
        from logging_setup import set_current_page

        set_current_page("")

        measured: list[dict[str, Any]] = []
        if todo:
            workers = max(1, min(STAGE_WORKERS, len(todo)))
            logger.info("Quality/rotation/HW workers=%d", workers)
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="page") as pool:
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
                    **item["page_type"],
                )
                set_page_image_source(
                    conn,
                    item["page_id"],
                    use_corrected=bool(item.get("use_corrected")),
                    image_path=item.get("image_path")
                    or f"pages/{item['page_name']}",
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
