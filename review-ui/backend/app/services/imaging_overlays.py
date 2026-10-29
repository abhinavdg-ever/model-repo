"""Load imaging pipeline CSV outputs and overlay onto page rows (no dummy values).

Sources (first hit wins):
  1) data/folders/<chart>/imaging/<chart>_*.csv
  2) data/pipeline/<filename>.csv  (Docker: /data/pipeline) — flat drop
  3) data/pipeline/<pack>/…/output/<filename>.csv — mirrored packs
  4) monorepo 01-ocr-extraction / 02-imaging-pipeline pack outputs
"""

from __future__ import annotations

import csv
import json
import re
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from app.core.schemas import ImagingPageResult, ImagingVerificationDetails
from app.services.dates import show_date
from app.services.ground_truth import spell_codeable

_WS_RE = re.compile(r"\s+")


# Display-only names: the pipeline and taxonomy keep the canonical name.
_DISPLAY_NAMES = {"social history": "History"}


def display_page_type(label: str) -> str:
    """Page Type as the pipeline wrote it: ``Family (Page Type)``.

    The pipeline already drops the catalog's own parenthetical notes, so the
    one parenthesis left is the type within the family and is kept. A few
    names are shown shorter (``_DISPLAY_NAMES``: Social History -> History).
    """
    text = _WS_RE.sub(" ", (label or "").strip()).strip(" /")
    return _DISPLAY_NAMES.get(text.casefold(), text)


def compose_page_type(page_type: str | None, page_subtype: str | None) -> str:
    """``Page Type (Sub-type)`` from the taxonomy columns; just the name when
    the sub-type is the generic one. A row from before the taxonomy holds the
    whole ``Family (Type)`` string in one of them and is returned as is."""
    page_type = display_page_type(page_type or "")
    page_subtype = display_page_type(page_subtype or "")
    if page_type and page_subtype and page_type != page_subtype:
        return f"{page_type} ({page_subtype})"
    return page_type or page_subtype


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
    return show_date(raw)


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

    final = (row.get("final_dos") or "").strip() or None
    match = (row.get("match_type") or "").strip() or None
    return {
        "dosFrom": dos_from,
        "dosTo": dos_to,
        "docDosFrom": doc_from,
        "docDosTo": doc_to,
        "dosConfidence": _parse_confidence(row.get("confidence")),
        "dosMatch": match,
        "finalDos": final,
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


def page_type_fields(
    document_type: Any,
    is_visible: Any,
    handwritten_area_pct: Any,
) -> dict[str, Any]:
    """ocr_quality_results page-type columns (or their CSV cells) → API fields.

    Empty values are left out so they do not overwrite another source.
    """
    doc = _text(document_type).strip()
    fields: dict[str, Any] = {
        "documentType": doc.capitalize() if doc else None,
        "isVisible": _parse_bool(_text(is_visible)),
        "handwrittenAreaPct": _parse_float(_text(handwritten_area_pct)),
    }
    return {k: v for k, v in fields.items() if v is not None}


def _text(value: Any) -> str:
    return "" if value is None else str(value)


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
            page.isCodeable is not None,
            page.encounterType is not None,
            page.actualSequence is not None,
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
        if "measuredConfidences" in hit and page.measuredConfidences:
            # Two files carry these (hw_printed, rotation): merge, never replace.
            hit = {**hit, "measuredConfidences": {**page.measuredConfidences, **hit["measuredConfidences"]}}
        out.append(page.model_copy(update=hit))
    return out


def inherit_duplicate_blank_junk(pages: list[ImagingPageResult]) -> list[ImagingPageResult]:
    """A duplicate page shows its original's Blank/Junk tag when the original is
    blank (or junk); otherwise it keeps its own "No" (a duplicate is not junk).

    Duplicates are found among Main pages, but the original can be junked later
    (pass 2), so the duplicate then reads as the page it copies.
    ``duplicateOf`` is the original's file name; a page whose original is not found
    keeps its own tag.
    """
    by_name: dict[str, ImagingPageResult] = {}
    for page in pages:
        by_name[page.fileName.lower()] = page
        by_name.setdefault(Path(page.fileName).stem.lower(), page)
    out: list[ImagingPageResult] = []
    for page in pages:
        original_name = (page.duplicateOf or "").strip().lower()
        original = (
            (by_name.get(original_name) or by_name.get(Path(original_name).stem))
            if original_name
            else None
        )
        if (
            page.isDuplicate
            and original is not None
            and original is not page
            and (original.blankOrJunk or "").strip().lower().startswith("yes")
        ):
            page = page.model_copy(update={"blankOrJunk": original.blankOrJunk})
        out.append(page)
    return out


def overlay_dos_decision(
    pages: list[ImagingPageResult],
    by_key: dict[str, dict[str, Any]],
) -> list[ImagingPageResult]:
    """Copy the span decision onto pages whose dates came from the database.

    ``match_type`` and ``final_dos`` live on the DOS CSV. The database row
    keeps the page date and the document date, and has no column for the
    continuation mark.
    """
    if not by_key:
        return pages
    out: list[ImagingPageResult] = []
    for page in pages:
        hit = _hit_for_page(by_key, page)
        if not hit:
            out.append(page)
            continue
        update: dict[str, Any] = {}
        if hit.get("dosMatch"):
            update["dosMatch"] = hit["dosMatch"]
        if hit.get("finalDos"):
            update["finalDos"] = hit["finalDos"]
        out.append(page.model_copy(update=update) if update else page)
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


def signature_people(text: str) -> tuple[str, str]:
    """Name and credentials of the one provider in a ``provider_name`` cell.

    The pipeline writes only the highest-scoring provider. A CSV from an
    earlier run lists several, separated by `` | ``; only the first is shown.
    A credential is a suffix on that person (``PA``, ``MD``, ``DO``).
    """
    from app.services.extraction_review import _is_credential

    names: list[str] = []
    credentials: list[str] = []
    for person in (text or "").split("|")[:1]:
        person = person.strip()
        if not person:
            continue
        name, creds = _split_person(person, _is_credential)
        if name:
            names.append(name)
        credentials.extend(creds)
    return " | ".join(names), " | ".join(credentials)


def _split_person(person: str, is_credential) -> tuple[str, list[str]]:
    pieces = [part.strip() for part in person.split(",") if part.strip()]
    creds: list[str] = []
    name_bits: list[str] = []
    for piece in pieces:
        tokens = piece.split()
        if tokens and all(is_credential(token) for token in tokens):
            creds.extend(tokens)
            continue
        trail: list[str] = []
        while tokens and is_credential(tokens[-1]):
            trail.append(tokens.pop())
        trail.reverse()
        if tokens:
            name_bits.append(" ".join(tokens))
        creds.extend(trail)
    return " ".join(name_bits), creds


def undotted(credentials: str) -> str:
    """Credentials shown without dots: 'M.D.' -> 'MD', 'Ph.D.' -> 'PhD', 'R.N.' -> 'RN'. A dot
    between two longer credentials separates them: 'APRN.CNP' -> 'APRN CNP'."""
    text = re.sub(r"(?<=[A-Za-z]{2})\.(?=[A-Za-z]{2})", " ", credentials)
    return re.sub(r"[ \t]{2,}", " ", text.replace(".", "")).strip()


def provider_fields(name: str | None, credentials: str | None) -> tuple[str, str]:
    """A provider's name and credentials as the pipeline stored them. Rows written since the
    provider-signature rule (2026-10-18) carry ``provider_credentials`` beside a bare name;
    an older row (``credentials`` None: no such column) keeps them inside the name."""
    bare_name, credentials_in_name = signature_people(name or "")
    if credentials is None:
        return bare_name, undotted(credentials_in_name)
    # The name is shown without credentials even if some slipped into it: the known ones,
    # and the row's own (DNP, APRN.CNP) wherever they sit.
    own = {re.sub(r"[\s.]+", "", c).casefold() for c in re.split(r"[,|/]", credentials) if c.strip()}
    if own:
        kept = [
            token for token in re.split(r"(\s+|,)", bare_name)
            if re.sub(r"[\s.,]+", "", token).casefold() not in own
        ]
        bare_name = re.sub(r"\s*,\s*$", "", re.sub(r"\s+", " ", "".join(kept))).strip(" ,")
    return bare_name, undotted(credentials)


def provider_version(value: Any) -> int:
    """The provider-signature rule a row was written by: 2 (2026-10-18) or 1 (older; also
    a row with no version at all)."""
    try:
        return int(str(value).strip()) if value not in (None, "") else 1
    except ValueError:
        return 1


def _name_words(name: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", name.casefold())


def same_person(a: str, b: str) -> bool:
    """Two spellings of one signer: the same words ignoring case and punctuation
    ("Lynda J Storm" / "Lynda J. Storm"), the same first and last name with or
    without middle initials ("Lynda Storm"), or an OCR slip (≥ 90% alike)."""
    wa, wb = _name_words(a), _name_words(b)
    if not wa or not wb:
        return False
    if wa == wb or sorted(wa) == sorted(wb):
        return True
    if len(wa) > 1 and len(wb) > 1 and (wa[0], wa[-1]) == (wb[0], wb[-1]):
        return True
    return SequenceMatcher(None, " ".join(wa), " ".join(wb)).ratio() >= 0.9


def _distinct_signers(people: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each signer once: a later spelling of someone already listed is dropped,
    lending its credentials and date when the first had none."""
    kept: list[dict[str, Any]] = []
    for person in people:
        twin = next((k for k in kept if same_person(k["name"], person["name"])), None)
        if twin is None:
            kept.append(dict(person))
            continue
        for key in ("credentials", "signatureDate"):
            if not twin.get(key) and person.get(key):
                twin[key] = person[key]
    return kept


def signer_fields(signers: Any, name: str | None, credentials: str | None, version: Any = None) -> dict[str, Any]:
    """providerName / providerCredentials / providerSigners from a signature row, read by
    the rule that wrote it (``provider_version``).

    Version 2: every signer in ``signers`` (a JSON list, or its text) is listed,
    providerName is the first and providerCredentials all of their credentials; the
    ``provider_credentials`` column beside a bare name otherwise. Version 1 (or none): the
    one provider, its credentials split off the name.
    """
    if provider_version(version) < 2:
        who, creds = provider_fields(name, None)
        return {"providerName": who or None, "providerCredentials": creds or None}
    if isinstance(signers, str):
        try:
            signers = json.loads(signers) if signers.strip() else None
        except ValueError:
            signers = None
    people: list[dict[str, str | None]] = []
    for item in signers if isinstance(signers, list) else []:
        if not isinstance(item, dict):
            continue
        who, creds = provider_fields(item.get("name"), item.get("credentials") or "")
        if who:
            people.append({"name": who, "credentials": creds or None,
                           "signatureDate": item.get("signature_date") or None})
    people = _distinct_signers(people)
    if people:
        joined = " | ".join(p["credentials"] for p in people if p["credentials"])
        return {"providerName": people[0]["name"], "providerCredentials": joined or None,
                "providerSigners": people}
    who, creds = provider_fields(name, credentials or "")
    return {"providerName": who or None, "providerCredentials": creds or None}


def index_signature_rows(
    rows: list[dict[str, str]], chart_name: str
) -> dict[str, dict[str, Any]]:
    """provider_signature.csv → name, credentials, and Yes/No signature."""
    by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        cname = (row.get("chart_name") or row.get("chart_id") or row.get("folder") or "").strip()
        if cname and not _chart_row_matches(cname, chart_name):
            continue
        present = (row.get("signature_present") or "").strip().lower() in {"y", "yes", "true", "1"}
        fields: dict[str, Any] = {"providerSignature": "Yes" if present else "No"}
        people = signer_fields(
            row.get("signers"), row.get("provider_name"), row.get("provider_credentials"), row.get("provider_version")
        )
        fields.update({key: value for key, value in people.items() if value})
        confidence = _parse_confidence(row.get("confidence"))
        if confidence is not None:
            fields["providerSignatureConfidence"] = confidence
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
            **page_type_fields(
                row.get("document_type"),
                row.get("is_visible"),
                row.get("handwritten_area_pct"),
            ),
        }
        fields = {k: v for k, v in fields.items() if v is not None and v != ""}
        measured = _measured(row, visibility="visibility_confidence", handwritten_area="handwritten_area_confidence")
        if measured:
            fields["measuredConfidences"] = measured
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


def subtype_confidence(l2: Any, l2_conf: Any, l3: Any, l3_conf: Any) -> float | None:
    """The subtype's own confidence: the deepest named level's (L3, else L2; Others is not
    named), as core-pipeline's axes.subtype_confidence."""
    for name, value in ((l3, l3_conf), (l2, l2_conf)):
        if name and str(name).strip() and str(name).strip() != "Others":
            parsed = _parse_confidence(value) if isinstance(value, str) else (float(value) if value is not None else None)
            if parsed is not None:
                return parsed
    return None


def _measured(row: dict[str, str], **columns: str) -> dict[str, float]:
    """Stage 1's measured values' confidences from a CSV row: {key: column}."""
    out: dict[str, float] = {}
    for key, column in columns.items():
        value = _parse_confidence(row.get(column))
        if value is not None:
            out[key] = value
    return out


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
        }
        fields = {k: v for k, v in fields.items() if v is not None}
        measured = _measured(row, orientation="orientation_confidence", mirrored="orientation_confidence",
                             tilt="tilt_confidence")
        if measured:
            fields["measuredConfidences"] = measured
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


def index_quality_rows(
    rows: list[dict[str, str]], chart_name: str
) -> dict[str, dict[str, Any]]:
    """<chart>_quality.csv → pageQualityTag + pageQualityConfidence."""
    by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        cname = (row.get("chart_name") or row.get("chart_id") or row.get("folder") or "").strip()
        if cname and not _chart_row_matches(cname, chart_name):
            continue
        tag = (row.get("quality_tag") or row.get("quality") or "").strip()
        score = _parse_confidence(
            row.get("quality_score") or row.get("confidence") or row.get("score")
        )
        fields: dict[str, Any] = {}
        if tag:
            fields["pageQualityTag"] = tag
        if score is not None:
            fields["pageQualityConfidence"] = score
        if not fields:
            continue
        _put_page_keys(by_key, row, fields)
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
        dob = show_date(row.get("extracted_dob")) or ""
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


def index_duplicate_scores(staged_path: Path) -> dict[str, dict[str, Any]]:
    """``<chart>/staging/blank_junk.json`` -> each page's ``duplicateConfidence``: the
    similarity of its closest page in its last judged pass (pass 2, else pass 1), listed
    from 70% up. Local Mode's source for what Production Mode reads from
    ``blank_junk_classification.duplicate_confidence``; below 70% (not listed) the
    display is 100% "not a duplicate" either way. An absent or unreadable file gives
    nothing (never raises)."""
    try:
        payload = json.loads(staged_path.read_text(encoding="utf-8")) if staged_path.is_file() else {}
    except (OSError, ValueError):
        return {}
    by_key: dict[str, dict[str, Any]] = {}
    for page_name, passes in (payload.get("pages") or {}).items():
        if not isinstance(passes, dict):
            continue
        last = next(
            (passes[k] for k in ("pass_2", "pass_1") if isinstance(passes.get(k), dict)), {}
        )
        score = (last.get("duplicate") or {}).get("similarity")
        if score is None:
            continue
        try:
            _put_page_keys(by_key, {"page_name": str(page_name)}, {"duplicateConfidence": float(score)})
        except (TypeError, ValueError):
            continue
    return by_key

_DUP_PAGE_RE = re.compile(r"duplicate_of_page:(\S+)")


def _duplicate_of_name(row: dict[str, str]) -> str:
    """Original page file a duplicate was matched to, when the junk CSV recorded it."""
    named = (row.get("duplicate_of_page") or row.get("duplicate_of") or "").strip()
    if named:
        return named
    match = _DUP_PAGE_RE.search(row.get("reason") or "")
    return match.group(1) if match else ""


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
        elif is_duplicate:
            # A duplicate is not junk (2026-10-29, user).
            blank_or_junk = "No"
        elif is_typed_junk:
            blank_or_junk = "Yes (Junk)"
        else:
            blank_or_junk = "No"
        pointing = not is_duplicate and group in ("main", "junk") and bool(_duplicate_of_name(row))
        # A copied page whose encounter is not a copy (continuity, 2026-10-29); any other
        # Main or junk page that names the page it nearly copies: a partial (95-98%) duplicate.
        is_content = pointing and "duplicate_content_of_page_id:" in (row.get("reason") or "")
        is_partial = pointing and not is_content

        if is_blank or is_duplicate or label_l in {"main", "none", ""}:
            page_type = "Not Available"
        else:
            page_type = page_type_labels.get(label_l, label if is_typed_junk else "Not Available")

        duplicate_of = _duplicate_of_name(row) if is_duplicate or is_partial or is_content else ""
        fields: dict[str, Any] = {
            "blankOrJunk": blank_or_junk,
            "isDuplicate": bool(is_duplicate),
            "isPartialDuplicate": is_partial,
            "isDuplicateContent": is_content,
            "pageType": page_type,
            # The blank/junk verdict's own confidence (the stage writes it to this column);
            # the page type's comes from the codeable CSV.
            "blankJunkConfidence": _parse_confidence(
                row.get("page_classification_confidence") or row.get("confidence")
            ),
        }
        fields = {
            k: v
            for k, v in fields.items()
            if v is not None or k in {"blankOrJunk", "isDuplicate", "isPartialDuplicate", "isDuplicateContent"}
        }
        fields["isDuplicate"] = bool(is_duplicate)
        if duplicate_of:
            fields["duplicateOf"] = duplicate_of

        _put_page_keys(by_key, row, fields)
        raw_num = (row.get("page_num") or row.get("page_number") or "").strip()
        if raw_num.isdigit():
            by_key[f"#{raw_num}"] = fields
            by_key[f"{raw_num}.jpg"] = fields
            by_key[f"{raw_num}.png"] = fields
    return by_key


def index_codeable_rows(
    rows: list[dict[str, str]], chart_name: str
) -> dict[str, dict[str, Any]]:
    """``*_codeable.csv`` → pageType + isCodeable.

    Junk leaves Main pages as pageType=\"Not Available\". This overlay writes
    the TF page_type when present. Unmatched main pages keep Not Available and
    get isCodeable=\"Not Sure\".
    """
    by_key: dict[str, dict[str, Any]] = {}
    display = {
        "codeable": "Codable",
        "non_codeable": "Non-Codable",
        "discharge_frequency": "Discharge",
        "discharge_summary": "Discharge",
        "not_sure": "Not Sure",
    }
    for row in rows:
        cname = (
            row.get("chart_name") or row.get("chart_id") or row.get("folder") or ""
        ).strip()
        if cname and not _chart_row_matches(cname, chart_name):
            continue
        page_type = compose_page_type(
            row.get("page_type") or row.get("pageType"), row.get("page_subtype")
        )
        tag = (row.get("tag") or "").strip().casefold()
        label = (
            row.get("is_codeable")
            or row.get("isCodeable")
            or display.get(tag)
            or ""
        ).strip()
        if label.casefold() == "discharge frequency":
            label = "Discharge"
        # A CSV from an earlier run says Codeable / Non Codeable.
        label = spell_codeable(label) or ""
        # Blank/Duplicate rows always have a page_type (Blank / Duplicate / …).
        # Empty or Not Available → Not Sure for codability.
        if not label:
            if not page_type or page_type.casefold() == "not available":
                label = "Not Sure"
            else:
                continue
        elif page_type.casefold() == "not available" and tag in {"", "not_sure"}:
            label = "Not Sure"
        fields: dict[str, Any] = {"isCodeable": label}
        # The page type's confidence from the ladder step (page_arbitration.json),
        # as the Postgres path reads page_classification.confidence.
        confidence = _parse_confidence(row.get("confidence"))
        if confidence is not None:
            fields["pageTypeConfidence"] = confidence
        subtype = subtype_confidence(
            row.get("axes_l2"), row.get("axes_l2_confidence"), row.get("axes_l3"), row.get("axes_l3_confidence")
        )
        if subtype is not None:
            fields["subtypeConfidence"] = subtype
        fields["specialty"] = (row.get("specialty") or "").strip() or "Unknown"
        if (row.get("specialty") or "").strip():
            fields["specialtyConfidence"] = _parse_confidence(row.get("specialty_confidence"))
        if page_type:
            fields["pageType"] = display_page_type(page_type)
        elif label == "Not Sure":
            fields["pageType"] = "Not Available"
        _put_page_keys(by_key, row, fields)
    return by_key


def index_encounter_rows(
    rows: list[dict[str, str]], chart_name: str
) -> dict[str, dict[str, Any]]:
    """``*_encounter.csv`` → encounterType display label and its confidence."""
    by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        cname = (
            row.get("chart_name") or row.get("chart_id") or row.get("folder") or ""
        ).strip()
        if cname and not _chart_row_matches(cname, chart_name):
            continue
        label = (
            row.get("encounter_label")
            or row.get("encounterType")
            or row.get("encounter_type")
            or ""
        ).strip()
        if not label:
            continue
        fields: dict[str, Any] = {"encounterType": label}
        confidence = _parse_confidence(row.get("confidence"))
        if confidence is not None:
            fields["encounterConfidence"] = confidence
        _put_page_keys(by_key, row, fields)
    return by_key


def index_sequencing_rows(
    rows: list[dict[str, str]], chart_name: str
) -> dict[str, dict[str, Any]]:
    """``*_sequencing.csv`` → currentSequence / actualSequence and its confidence."""
    by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        cname = (
            row.get("chart_name") or row.get("chart_id") or row.get("folder") or ""
        ).strip()
        if cname and not _chart_row_matches(cname, chart_name):
            continue
        current = _parse_int(
            row.get("current_sequence") or row.get("page_number")
        )
        actual = _parse_int(
            row.get("actual_sequence") or row.get("seq") or row.get("sequence_position")
        )
        if current is None and actual is None:
            continue
        fields: dict[str, Any] = {}
        if current is not None:
            fields["currentSequence"] = current
        if actual is not None:
            fields["actualSequence"] = actual
        confidence = _parse_confidence(row.get("confidence"))
        if confidence is not None:
            fields["sequenceConfidence"] = confidence
        _put_page_keys(by_key, row, fields)
    return by_key


def index_continuity_rows(
    rows: list[dict[str, str]], chart_name: str
) -> dict[str, dict[str, Any]]:
    """``*_continuity.csv`` → the page's document: number, position, evidence.

    Blank, junk and duplicate pages have no document and get no fields.
    """
    by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        cname = (row.get("chart_name") or "").strip()
        if cname and not _chart_row_matches(cname, chart_name):
            continue
        document = _parse_int(row.get("document"))
        # A junk page ('Last (Junk J1)') has no document number, only its label; Doc Summary
        # groups on the number, so it must stay empty.
        label = (row.get("continuation") or "").strip()
        if document is None and not label:
            continue
        _put_page_keys(
            by_key,
            row,
            {
                "documentSeq": document,
                "documentPosition": (row.get("position") or "").strip() or None,
                "documentLabel": (row.get("continuation") or "").strip()
                or document_label(row.get("position"), document),
                "continuityRelation": (row.get("relation") or "").strip() or None,
                "continuityDecidedBy": (row.get("decided_by") or "").strip() or None,
                "continuityEvidence": (row.get("evidence") or "").strip() or None,
            },
        )
    return by_key


_TAG = {"first": "New", "single": "Standalone", "continue": "Continue", "last": "Last"}


def continuity_label(position: str | None, document: int | None, confidence_level: str | None) -> str | None:
    """'New (Doc 3)', 'Continue? (Doc 3)' from page_continuity_results' position: the gap
    scorer's tags (first = New, single = Standalone); '?' when the gap before was low."""
    if not position or document is None:
        return None
    unsure = "?" if (confidence_level or "") == "low" else ""
    return f"{_TAG.get(position, position.capitalize())}{unsure} (Doc {document})"


def document_label(position: str | None, document: int | None) -> str | None:
    """'First (Doc 3)' — the continuity stage's own label, rebuilt for rows without it."""
    position = (position or "").strip()
    if not position or document is None:
        return None
    return f"{position.capitalize()} (Doc {document})"


_CATEGORY_DISPLAY = {
    "codeable": "Codable",
    "non_codeable": "Non-Codable",
    "discharge_summary": "Discharge",
}


def index_final_rows(
    rows: list[dict[str, str]], chart_name: str
) -> dict[str, dict[str, Any]]:
    """``*_final.csv`` → the Final page type, codeable and DOS of each page."""
    by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        cname = (row.get("chart_name") or "").strip()
        if cname and not _chart_row_matches(cname, chart_name):
            continue
        category = (row.get("classification_category") or "").strip()
        _put_page_keys(
            by_key,
            row,
            {
                "finalPageType": compose_page_type(row.get("page_type"), row.get("page_subtype")) or None,
                "finalCodeable": _CATEGORY_DISPLAY.get(category),
                "finalDosFrom": show_date(row.get("dos_from")),
                "finalDosTo": show_date(row.get("dos_to")),
                "finalSpecialty": (row.get("specialty") or "").strip() or None,
                **_final_confidences(row),
            },
        )
    return by_key


def _final_confidences(row: dict[str, str]) -> dict[str, Any]:
    """The final CSV's ``confidences`` / ``review_fields`` JSON cells (2026-10-25), when
    the run wrote them."""
    out: dict[str, Any] = {}
    for column, key in (("confidences", "finalConfidences"), ("review_fields", "reviewFields")):
        try:
            value = json.loads(row.get(column) or "null")
        except ValueError:
            continue
        if isinstance(value, (dict, list)):
            out[key] = value
    return out


def collect_rows(
    *,
    folder_dir: Path,
    data_root: Path,
    per_chart_name: str,
    combined_rel: tuple[str, ...] | None,
    chart_name: str,
    chart_filter: str | None = None,
) -> list[dict[str, str]]:
    """Prefer per-chart imaging CSV, else combined pack output."""
    per_chart = folder_dir / "imaging" / per_chart_name
    rows = read_csv_rows(per_chart)
    if rows:
        return rows
    if not combined_rel:
        return []
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
