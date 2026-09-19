"""Load imaging pipeline CSV outputs and overlay onto page rows (no dummy values).

Sources (first hit wins):
  1) data/folders/<chart>/imaging/<chart>_*.csv
  2) data/pipeline/<filename>.csv  (Docker: /data/pipeline) — flat drop
  3) data/pipeline/<pack>/…/output/<filename>.csv — mirrored packs
  4) monorepo 01-ocr-extraction / 02-imaging-pipeline pack outputs
"""

from __future__ import annotations

import csv
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.core.schemas import ImagingPageResult, ImagingVerificationDetails


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file() or path.stat().st_size == 0:
        return []
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows: list[dict[str, str]] = []
        for raw in reader:
            row = {(k or "").strip(): (v or "").strip() for k, v in raw.items() if k is not None}
            if row:
                rows.append(row)
        return rows


def monorepo_root_from_data(data_root: Path) -> Path:
    """Resolve pack root (01-ocr-extraction / 02-imaging-pipeline) or data/pipeline drop."""
    try:
        from app.core.config import get_settings

        return get_settings().resolved_monorepo_root
    except Exception:
        pass
    env = __import__("os").environ.get("MONOREPO_ROOT", "").strip()
    if env:
        path = Path(env)
        if path.is_dir():
            return path.resolve()
    for cand in (
        Path("/data/pipeline"),
        Path("/data/monorepo"),
        data_root.parent / "pipeline",
        data_root.parent.parent.parent,
    ):
        if not cand.is_dir():
            continue
        if (cand / "02-imaging-pipeline").is_dir() or (cand / "01-ocr-extraction").is_dir():
            return cand.resolve()
        if any(cand.glob("*.csv")):
            return cand.resolve()
    return data_root.parent.parent.parent


def _csv_has_data_rows(path: Path) -> bool:
    """True when file has at least one non-empty data row (not header-only)."""
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
            # Skip blank / AppleDouble noise; first non-empty line = header
            header = None
            for line in f:
                if line.strip():
                    header = line
                    break
            if header is None:
                return False
            for line in f:
                if line.strip():
                    return True
    except OSError:
        return False
    return False


def resolve_pipeline_csv(data_root: Path, *combined_rel: str) -> Path:
    """Locate a pipeline CSV: pack path, data/pipeline mirror, or flat filename drop."""
    filename = combined_rel[-1] if combined_rel else ""
    # Real export aliases from pipeline teams
    alt_names = {
        "hw_printed.csv": ("hw_printed_classification.csv", "hw_printed.csv"),
        "rotation.csv": ("rotation_orientation.csv", "rotation.csv"),
        "junk_classification.csv": ("junk_classification.csv",),
    }
    flat_names = alt_names.get(filename, (filename,))

    roots: list[Path] = []
    try:
        from app.core.config import get_settings

        settings = get_settings()
        roots.append(settings.resolved_pipeline_root)
        roots.append(settings.resolved_monorepo_root)
    except Exception:
        pass
    roots.extend(
        [
            Path("/data/pipeline"),
            data_root.parent / "pipeline",
            monorepo_root_from_data(data_root),
        ]
    )

    seen: set[Path] = set()
    for root in roots:
        try:
            root = root.resolve()
        except OSError:
            continue
        if root in seen or not root.is_dir():
            continue
        seen.add(root)
        for name in flat_names:
            for candidate in (
                root.joinpath(*combined_rel[:-1], name) if combined_rel else root / name,
                root / "output" / name,
                root / name,
            ):
                if candidate.is_file() and _csv_has_data_rows(candidate):
                    return candidate
        # original pack path
        pack = root.joinpath(*combined_rel)
        if pack.is_file() and _csv_has_data_rows(pack):
            return pack
    return monorepo_root_from_data(data_root).joinpath(*combined_rel)


def chart_id_key(chart_name: str) -> str:
    """52743839_44976074 → 52743839; plain names unchanged."""
    name = chart_name.strip()
    if "_" in name and name.split("_", 1)[0].isdigit():
        return name.split("_", 1)[0]
    return name


def _fmt_dos_display(raw: str | date | None) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, date):
        return raw.strftime("%m/%d/%Y")
    value = str(raw).strip()
    if not value or value.lower() in {"unknown", "null", "none"}:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y", "%m-%d-%y"):
        try:
            return datetime.strptime(value, fmt).strftime("%m/%d/%Y")
        except ValueError:
            continue
    return value


def dos_row_fields(row: dict[str, str]) -> dict[str, str | None]:
    dos_from = _fmt_dos_display(row.get("dos_from_iso") or row.get("dos_from") or row.get("dos"))
    dos_to = _fmt_dos_display(row.get("dos_to_iso") or row.get("dos_to") or "")
    # Multi-date lists (comma-separated) — format each part for display
    raw_from = (row.get("dos_from") or "").strip()
    raw_to = (row.get("dos_to") or "").strip()
    if "," in raw_from:
        parts = [_fmt_dos_display(p.strip()) for p in raw_from.split(",")]
        dos_from = ", ".join(p for p in parts if p)
    if "," in raw_to:
        parts = [_fmt_dos_display(p.strip()) for p in raw_to.split(",")]
        dos_to = ", ".join(p for p in parts if p)
    if dos_from and not dos_to and (row.get("dos_from") or row.get("dos_from_iso") or row.get("dos")):
        dos_to = dos_from

    doc_from_raw = (row.get("doc_dos_from") or "").strip()
    doc_to_raw = (row.get("doc_dos_to") or "").strip()
    doc_from = _fmt_dos_display(row.get("doc_dos_from_iso") or row.get("doc_dos_from") or "")
    doc_to = _fmt_dos_display(row.get("doc_dos_to_iso") or row.get("doc_dos_to") or "")
    if "," in doc_from_raw:
        parts = [_fmt_dos_display(p.strip()) for p in doc_from_raw.split(",")]
        doc_from = ", ".join(p for p in parts if p)
    if "," in doc_to_raw:
        parts = [_fmt_dos_display(p.strip()) for p in doc_to_raw.split(",")]
        doc_to = ", ".join(p for p in parts if p)

    return {
        "dosFrom": dos_from,
        "dosTo": dos_to,
        "docDosFrom": doc_from,
        "docDosTo": doc_to,
        "dosConfidence": _parse_confidence(row.get("confidence")),
    }


def _parse_float(raw: str | None) -> float | None:
    """Parse a number as-is (angles, counts, etc.)."""
    value = (raw or "").strip()
    if not value or value.upper() in {"N/A", "NA", "NULL", "NONE"}:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_confidence(raw: str | None) -> float | None:
    """Parse confidence; values in (1, 100] treated as percent → 0–1."""
    num = _parse_float(raw)
    if num is None:
        return None
    if num > 1.0 and num <= 100.0:
        return round(num / 100.0, 4)
    return num


def _parse_bool(raw: str | None) -> bool | None:
    value = (raw or "").strip().lower()
    if not value:
        return None
    if value in {"1", "true", "t", "yes", "y"}:
        return True
    if value in {"0", "false", "f", "no", "n"}:
        return False
    return None


def empty_imaging_pages(pages: list[tuple[int, Path]]) -> list[ImagingPageResult]:
    """Skeleton rows only — all extraction fields stay null until CSV overlay."""
    return [
        ImagingPageResult(pageNumber=num, fileName=path.name) for num, path in pages
    ]


def page_has_imaging(page: ImagingPageResult) -> bool:
    return any(
        [
            page.memberName,
            page.memberDob,
            page.memberId,
            page.handwrittenOrPrinted,
            page.orientationAngle is not None,
            page.tiltAngle is not None,
            page.mirrored is not None,
            page.dosFrom,
            page.dosTo,
            page.pageType,
            page.blankOrJunk is not None,
            page.isDuplicate is not None,
        ]
    )


def _hit_for_page(
    by_key: dict[str, dict[str, Any]], page: ImagingPageResult
) -> dict[str, Any] | None:
    return (
        by_key.get(page.fileName.lower())
        or by_key.get(Path(page.fileName).name.lower())
        or by_key.get(f"#{page.pageNumber}")
        or by_key.get(Path(page.fileName).stem.lower())
    )


def overlay_fields(
    pages: list[ImagingPageResult],
    by_key: dict[str, dict[str, Any]],
) -> list[ImagingPageResult]:
    if not by_key:
        return pages
    out: list[ImagingPageResult] = []
    for page in pages:
        hit = _hit_for_page(by_key, page)
        if not hit:
            out.append(page)
            continue
        out.append(page.model_copy(update=hit))
    return out


def index_dos_rows(rows: list[dict[str, str]], chart_name: str) -> dict[str, dict[str, Any]]:
    by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        cname = (row.get("chart_name") or row.get("chart_id") or row.get("folder") or "").strip()
        if cname and not _chart_row_matches(cname, chart_name):
            continue
        fields = dos_row_fields(row)
        _put_page_keys(by_key, row, fields)
    return by_key


def index_hw_rows(rows: list[dict[str, str]], chart_name: str) -> dict[str, dict[str, Any]]:
    """hw_printed / hw_printed_classification rows → Printed/Handwritten + confidence."""
    by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        cname = (row.get("chart_name") or row.get("chart_id") or row.get("folder") or "").strip()
        if cname and not _chart_row_matches(cname, chart_name):
            continue
        label = (
            row.get("handwritten")
            or row.get("handwritten_or_printed")
            or row.get("type")
            or ""
        ).strip()
        if not label or label.upper() == "N/A":
            continue
        fields: dict[str, Any] = {
            "handwrittenOrPrinted": label,
            "handwrittenOrPrintedConfidence": _parse_confidence(row.get("confidence")),
        }
        fields = {k: v for k, v in fields.items() if v is not None and v != ""}
        if not fields:
            continue
        _put_page_keys(by_key, row, fields)
        # also page_num from hw_printed_classification
        raw_num = (row.get("page_num") or row.get("page_number") or "").strip()
        if raw_num.isdigit():
            by_key[f"#{raw_num}"] = fields
            by_key[f"{raw_num}.jpg"] = fields
            by_key[f"{raw_num}.png"] = fields
    return by_key


def index_rotation_rows(
    rows: list[dict[str, str]], chart_name: str
) -> dict[str, dict[str, Any]]:
    """rotation.csv / rotation_orientation.csv → orientation, tilt, mirrored."""
    by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        folder = (
            row.get("folder") or row.get("chart_name") or row.get("chart_id") or ""
        ).strip()
        if folder and not _chart_row_matches(folder, chart_name):
            continue
        filename = (row.get("filename") or row.get("page_name") or "").strip()
        fields: dict[str, Any] = {
            "orientationAngle": _parse_float(
                row.get("rotation_deg")
                or row.get("rotation_degree")
                or row.get("rotation_di")
                or row.get("orientation_angle")
                or row.get("rotation")
            ),
            "tiltAngle": _parse_float(
                row.get("tilt_angle_deg")
                or row.get("tilt_angle")
                or row.get("tilt_angle_c")
                or row.get("tilt")
            ),
            "mirrored": _parse_bool(row.get("mirrored")),
            "pageQualityConfidence": _parse_confidence(row.get("confidence")),
        }
        fields = {k: v for k, v in fields.items() if v is not None}
        if not fields:
            continue
        if filename:
            by_key[filename.lower()] = fields
            by_key[Path(filename).name.lower()] = fields
            stem = Path(filename).stem
            by_key[stem.lower()] = fields
            if stem.isdigit():
                by_key[f"#{stem}"] = fields
        raw_num = (row.get("page_number") or row.get("page_num") or "").strip()
        if raw_num.isdigit():
            by_key[f"#{raw_num}"] = fields
            by_key[f"{raw_num}.jpg"] = fields
            by_key[f"{raw_num}.png"] = fields
    return by_key


def _chart_row_matches(row_chart: str, chart_name: str) -> bool:
    """Match CSV chart_id to folder name (exact, prefix, or shared numeric id)."""
    if not row_chart:
        return True
    cid = chart_id_key(chart_name)
    if row_chart in {chart_name, cid}:
        return True
    if chart_name.startswith(row_chart + "_"):
        return True
    if row_chart.startswith(chart_name + "_"):
        return True
    # CSV full id vs folder full id already covered; numeric CSV vs full folder covered above
    return False


def _register_page_lookup_keys(
    by_key: dict[str, dict[str, Any]],
    fields: dict[str, Any],
    *,
    page_name: str = "",
    page_num: str = "",
) -> None:
    """Index by page file name and/or page number (#N, N.jpg, …)."""
    if page_name and page_name.upper() != "N/A":
        by_key[page_name.lower()] = fields
        by_key[Path(page_name).name.lower()] = fields
        stem = Path(page_name).stem
        by_key[stem.lower()] = fields
        if stem.isdigit():
            by_key[f"#{stem}"] = fields
            by_key[f"{stem}.jpg"] = fields
            by_key[f"{stem}.png"] = fields
            by_key[f"{stem}.jpeg"] = fields
            by_key[f"{stem}.tif"] = fields
            by_key[f"{stem}.tiff"] = fields
        elif page_name.isdigit():
            by_key[f"#{page_name}"] = fields
            by_key[f"{page_name}.jpg"] = fields
            by_key[f"{page_name}.png"] = fields

    num = page_num.strip()
    if num.isdigit():
        by_key[f"#{num}"] = fields
        by_key[num] = fields
        by_key[f"{num}.jpg"] = fields
        by_key[f"{num}.png"] = fields
        by_key[f"{num}.jpeg"] = fields
        by_key[f"{num}.tif"] = fields
        by_key[f"{num}.tiff"] = fields


def index_member_extraction_rows(
    rows: list[dict[str, str]], chart_name: str
) -> dict[str, dict[str, Any]]:
    """
    Index member_extraction_results rows for one chart.

    Match:
      chart_id / chart_name  → folder name (exact or numeric prefix)
      page_num / page_number / page_name → page file / page #

    Values:
      extracted_name, extracted_dob, confidence
      Member ID: matched_id | provided_member_id | provided_id | external_member_id
    """
    by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        row_chart = (row.get("chart_id") or row.get("chart_name") or "").strip()
        if not _chart_row_matches(row_chart, chart_name):
            continue

        name = (row.get("extracted_name") or "").strip()
        dob = (row.get("extracted_dob") or "").strip()
        member_id = (
            (row.get("matched_id") or "").strip()
            or (row.get("provided_member_id") or "").strip()
            or (row.get("provided_id") or "").strip()
            or (row.get("external_member_id") or "").strip()
        )
        if name.upper() == "N/A":
            name = ""
        if dob.upper() == "N/A":
            dob = ""
        if member_id.upper() == "N/A":
            member_id = ""

        fields: dict[str, Any] = {
            "memberName": name or None,
            "memberDob": dob or None,
            "memberId": member_id or None,
            "memberConfidence": _parse_confidence(row.get("confidence")),
        }
        fields = {k: v for k, v in fields.items() if v is not None and v != ""}
        if not fields:
            continue

        page_name = (row.get("page_name") or row.get("filename") or "").strip()
        page_num = (
            (row.get("page_num") or "").strip()
            or (row.get("page_number") or "").strip()
        )
        _register_page_lookup_keys(
            by_key, fields, page_name=page_name, page_num=page_num
        )
    return by_key


def _parse_int(raw: str | None) -> int | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def load_verifications(
    rows: list[dict[str, str]], chart_name: str
) -> list[ImagingVerificationDetails]:
    """
    member_verification_summary rows for this chart.

    Real export columns:
      chart_id, final_status, matched_member_id, matched_name,
      confidence (or matched_confidence), pages_matched, pages_checked, decision_reason
    """
    out: list[ImagingVerificationDetails] = []
    for row in rows:
        row_chart = (row.get("chart_id") or row.get("chart_name") or "").strip()
        if not _chart_row_matches(row_chart, chart_name):
            continue
        status = (row.get("final_status") or row.get("status") or "").strip() or None
        reason = (row.get("decision_reason") or "").strip() or None
        matched_name = (row.get("matched_name") or "").strip() or None
        matched_member_id = (
            (row.get("matched_member_id") or "").strip()
            or (row.get("matched_id") or "").strip()
            or None
        )
        conf = _parse_confidence(
            row.get("confidence") or row.get("matched_confidence")
        )
        pm = _parse_int(row.get("pages_matched"))
        pc = _parse_int(row.get("pages_checked"))
        # Skip completely empty rows (header-only files)
        if not any(
            [status, reason, matched_name, matched_member_id, conf is not None, pm is not None, pc is not None]
        ):
            continue
        out.append(
            ImagingVerificationDetails(
                finalStatus=status,
                matchedName=matched_name,
                matchedMemberId=matched_member_id,
                matchedConfidence=conf,
                pagesMatched=pm,
                pagesChecked=pc,
                decisionReason=reason,
            )
        )
    return out


def load_verification(
    rows: list[dict[str, str]], chart_name: str
) -> ImagingVerificationDetails | None:
    items = load_verifications(rows, chart_name)
    return items[0] if items else None


def _put_page_keys(
    by_key: dict[str, dict[str, Any]], row: dict[str, str], fields: dict[str, Any]
) -> None:
    page_name = (row.get("page_name") or row.get("filename") or "").strip()
    if page_name:
        by_key[page_name.lower()] = fields
        by_key[Path(page_name).name.lower()] = fields
        stem = Path(page_name).stem
        by_key[stem.lower()] = fields
        if stem.isdigit():
            by_key[f"#{stem}"] = fields
    raw_num = (row.get("page_number") or "").strip()
    if raw_num.isdigit():
        by_key[f"#{raw_num}"] = fields


def index_junk_rows(
    rows: list[dict[str, str]], chart_name: str
) -> dict[str, dict[str, Any]]:
    """junk_classification.csv → blankOrJunk / isDuplicate / pageType (decoupled).

    Blank or Junk and Duplicate are independent.
    Page Type shows junk subtype labels (Invoice, Cover Page, …).
    """
    by_key: dict[str, dict[str, Any]] = {}
    # Canonical UI labels (lowercase key → display)
    page_type_labels = {
        "invoice": "Invoice",
        "cover": "Cover Page",
        "cover page": "Cover Page",
        "record request/transmittal": "Record Request/Transmittal",
        "record request": "Record Request/Transmittal",
        "transmittal": "Record Request/Transmittal",
        "instructions": "Instructions",
        "others": "Others",
        "letter/fax": "Letter/Fax",
        "letter": "Letter/Fax",
        "fax": "Letter/Fax",
    }
    junk_label_keys = set(page_type_labels.keys()) | {
        "cover_page",
        "record_request",
        "letter_fax",
    }

    for row in rows:
        cname = (
            row.get("chart_name") or row.get("chart_id") or row.get("folder") or ""
        ).strip()
        if cname and not _chart_row_matches(cname, chart_name):
            continue

        label = (
            row.get("page_classification")
            or row.get("classification")
            or row.get("page_type")
            or ""
        ).strip()
        group = (row.get("page_group") or "").strip().lower()
        if not label and not group:
            continue

        label_l = label.lower()
        is_duplicate = label_l == "duplicate" or group == "duplicate"
        is_blank = label_l == "blank"
        is_typed_junk = (
            label_l in junk_label_keys
            or label_l in page_type_labels
            or (group == "junk" and not is_duplicate and not is_blank)
        )

        if is_blank:
            blank_or_junk = "Yes (Blank)"
        elif is_typed_junk and not is_duplicate:
            blank_or_junk = "Yes (Junk)"
        else:
            blank_or_junk = "No"

        if is_blank or is_duplicate or label_l in {"main", "none", ""}:
            page_type = "Not Available"
        else:
            page_type = page_type_labels.get(label_l, label if is_typed_junk else "Not Available")

        fields: dict[str, Any] = {
            "blankOrJunk": blank_or_junk,
            "isDuplicate": bool(is_duplicate),
            "pageType": page_type,
            "pageTypeConfidence": _parse_confidence(
                row.get("page_classification_confidence")
                or row.get("confidence")
                or row.get("page_type_confidence")
            ),
        }
        fields = {
            k: v
            for k, v in fields.items()
            if v is not None or k in {"blankOrJunk", "isDuplicate"}
        }
        fields["isDuplicate"] = bool(is_duplicate)

        _put_page_keys(by_key, row, fields)
        raw_num = (row.get("page_num") or row.get("page_number") or "").strip()
        if raw_num.isdigit():
            by_key[f"#{raw_num}"] = fields
            by_key[f"{raw_num}.jpg"] = fields
            by_key[f"{raw_num}.png"] = fields
    return by_key


def collect_rows(
    *,
    folder_dir: Path,
    data_root: Path,
    per_chart_name: str,
    combined_rel: tuple[str, ...],
    chart_name: str,
    chart_filter: str | None = None,
) -> list[dict[str, str]]:
    """Prefer per-chart imaging CSV, else combined pack output."""
    per_chart = folder_dir / "imaging" / per_chart_name
    rows = read_csv_rows(per_chart)
    if rows:
        return rows
    combined = resolve_pipeline_csv(data_root, *combined_rel)
    rows = read_csv_rows(combined)
    if not rows:
        return []
    key = chart_filter or chart_name
    filtered: list[dict[str, str]] = []
    for row in rows:
        # detect which id column exists
        for col in ("chart_name", "folder", "chart_id"):
            val = (row.get(col) or "").strip()
            if not val:
                continue
            if _chart_row_matches(val, chart_name) or val == key:
                filtered.append(row)
                break
    return filtered
