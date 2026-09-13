"""
DOS (Date of Service) extraction — ported from advantmed-autocoderai-new/scripts/split.py.

Per page (full-page regex; optional gated LLM):
  1) regex + visit / Admit / Discharge keywords on the **full page** → dos_from / dos_to
  2) multiple dates in the same year → list them (comma-separated); confidence 0.60
  3) single hit in top/bottom ~60 words → 0.95; only in middle (full page) → 0.70
  4) hardcoded preamble default 02-02-2022 → confidence 0.80
  5) Azure OpenAI only if page has a clinical section cue (optional --llm)
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Optional

from azure_llm import azure_deployment

VISIT_KEYWORDS = [
    # Visit / DOS
    "EVISIT",
    "E-VISIT",
    "ANNUAL WELNESS VISIT",
    "AWV",
    "TELEMEDICINE",
    "OFFICEVISIT",
    "OFFICE VISIT",
    "DATEOFVISIT",
    "DATE OF VISIT",
    "DATEOFSERVICE",
    "DATE OF SERVICE",
    "DOS",
    "DOS FROM",
    "DOS TO",
    "DOS FROM DATE",
    "DOS TO DATE",
    "SERVICEDATE",
    "SERVICE DATE",
    "ENCOUNTERDATE",
    "ENCOUNTER DATE",
    "EDVISITDATE",
    "ED VISIT DATE",
    "PRESENTATIONDATE",
    "PRESENTATION DATE",
    "ARRIVALDATE",
    "ARRIVAL DATE",
    "INJECTION VISIT",
    # Admit / from
    "ADMIT",
    "ADMITTED",
    "ADMITDATE",
    "ADMIT DATE",
    "ADMITTED DATE",
    "ADMITTED ON",
    "DATE OF ADMIT",
    "DATE OF ADMISSION",
    "ADMISSIONDATE",
    "ADMISSION DATE",
    "ADMISSION ON",
    # Discharge / to
    "DISCHARGE",
    "DISCHARGED",
    "DISCHARGEDATE",
    "DISCHARGE DATE",
    "DISCHARGED ON",
    "DATE OF DISCHARGE",
    "DISCHARGE ON",
]

# Keywords that specifically mean DOS From (admit side)
FROM_KEYWORDS = {
    "ADMIT",
    "ADMITTED",
    "ADMITDATE",
    "ADMIT DATE",
    "ADMITTED DATE",
    "ADMITTED ON",
    "DATE OF ADMIT",
    "DATE OF ADMISSION",
    "ADMISSIONDATE",
    "ADMISSION DATE",
    "ADMISSION ON",
    "DOS FROM",
    "DOS FROM DATE",
    "ARRIVALDATE",
    "ARRIVAL DATE",
}

# Keywords that specifically mean DOS To (discharge side)
TO_KEYWORDS = {
    "DISCHARGE",
    "DISCHARGED",
    "DISCHARGEDATE",
    "DISCHARGE DATE",
    "DISCHARGED ON",
    "DATE OF DISCHARGE",
    "DISCHARGE ON",
    "DOS TO",
    "DOS TO DATE",
}

DATE_REGEXES = [
    r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b",
    r"\b\d{4}[/-]\d{1,2}[/-]\d{1,2}\b",
    r"\b\d{1,2}\s+[A-Za-z]{3,9}\s*,?\s*\d{2,4}\b",
    r"\b[A-Za-z]{3,9}\s+\d{1,2},?\s*\d{2,4}\b",
]

EXCLUSION_PATTERNS = [
    r"\bdue\s+again\s+in\b",
    r"\bdue\s+again\s+on\b",
    r"\bdue\s+in\b",
    r"\bdue\s+on\b",
    r"\bdue\s+\d",
    r"\breturn\s+in\b",
    r"\breturn\s+on\b",
    r"\breturn\s+visit\s+in\b",
    r"\breturn\s+visit\s+on\b",
    r"\bnext\s+visit\s+in\b",
    r"\bnext\s+visit\s+on\b",
    r"\bscheduled\s+for\b",
    r"\bscheduled\s+on\b",
    r"\badministered\s+on\s+date\s+of\s+service\b",
    r"\bon\s+date\s+of\s+service\s*:",
]

# LLM is allowed only when one of these clinical section cues appears on the page
LLM_SECTION_CUES = re.compile(
    r"("
    r"chief\s+complaint|"
    r"history\s+of\s+present\s+illness|\bhpi\b|"
    r"discharge\s+(summary|note|diagnos|"
    r"instruction)|"
    r"reason\s+for\s+(consult|visit|admission)|"
    r"principal\s+diagnosis|"
    r"admission\s+(date|note|diagnos)|"
    r"hospital\s+course|"
    r"progress\s+note|"
    r"operative\s+(note|report)|"
    r"consult(?:ation)?\s+note|"
    r"emergency\s+(department|room)\s+note|"
    r"\bed\s+note\b"
    r")",
    re.IGNORECASE,
)

DISCHARGE_CUE = re.compile(
    r"(discharge\s+(summary|note|diagnos|date|instruction)|admission\s+date|"
    r"hospital\s+course|date\s+of\s+admission|date\s+of\s+discharge)",
    re.IGNORECASE,
)

# Non-encounter pages → document-level default DOS 02-02-2022
NON_ENCOUNTER_CUE = re.compile(
    r"("
    r"immuni[sz]ation|"
    r"vaccine|vaccination|"
    r"flu\s+shot|"
    r"covid[- ]?19\s+vacc|"
    r"medication\s+list|"
    r"problem\s+list|"
    r"allergy\s+list|"
    r"vital\s+signs|"
    r"face\s*sheet|"
    r"demographics|"
    r"patient\s+information"
    r")",
    re.IGNORECASE,
)

# Preamble / non-encounter / before-first-DOS default
DEFAULT_DOC_DOS = "02-02-2022"
DEFAULT_DOC_DOS_ISO = "2022-02-02"

# Confidence: edge-window 95%; full-page (middle) 70%; multi same-year 60%; hardcoded default 80%
CONF_EDGE = 0.95
CONF_FULL_PAGE = 0.70
CONF_MULTI = 0.60
CONF_DEFAULT = 0.80

UI_PAGE_MARKER_RE = re.compile(r"^=====\s*(.+?)\s*=====\s*$", re.MULTILINE)
AUTOCODER_PAGE_RE = re.compile(
    r"(^|\n)-----\s*Page\s*(\d+)[^\n]*\n", re.IGNORECASE
)

RANGE_RE = re.compile(
    r"("
    + "|".join(f"(?:{p})" for p in DATE_REGEXES)
    + r")\s*(?:-|–|—|to|through|/)\s*("
    + "|".join(f"(?:{p})" for p in DATE_REGEXES)
    + r")",
    re.IGNORECASE,
)

# Labeled Admit / Discharge + date (often at bottom of note)
_DATE_ALT = "(?:" + "|".join(DATE_REGEXES) + ")"
ADMIT_DATE_LABEL_RE = re.compile(
    r"\b(?:admit(?:ted)?(?:\s+date)?|admission(?:\s+date)?|date\s+of\s+admit(?:tance|ission)?)"
    r"\s*[:\-]?\s*(" + _DATE_ALT + r")",
    re.IGNORECASE,
)
DISCHARGE_DATE_LABEL_RE = re.compile(
    r"\b(?:discharge(?:d)?(?:\s+date)?|date\s+of\s+discharge)"
    r"\s*[:\-]?\s*(" + _DATE_ALT + r")",
    re.IGNORECASE,
)

REGEX_WINDOW_WORDS = 60


def normalize_date(date_str: str, reference_date: Optional[str] = None) -> str:
    """Normalize to MM-DD-YYYY."""
    if not date_str:
        return "unknown"

    date_str = date_str.strip()

    match = re.match(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", date_str)
    if match:
        month, day, year = match.groups()
        if len(year) == 2:
            year = "20" + year if int(year) < 50 else "19" + year
        return f"{month.zfill(2)}-{day.zfill(2)}-{year}"

    match = re.match(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", date_str)
    if match:
        year, month, day = match.groups()
        return f"{month.zfill(2)}-{day.zfill(2)}-{year}"

    match = re.match(r"(\d{1,2})\s+([A-Za-z]{3,9})\s*,?\s*(\d{2,4})", date_str)
    if not match:
        match = re.match(r"([A-Za-z]{3,9})\s+(\d{1,2}),?\s*(\d{2,4})", date_str)

    if match:
        month_names = {
            "january": "01",
            "jan": "01",
            "february": "02",
            "feb": "02",
            "march": "03",
            "mar": "03",
            "april": "04",
            "apr": "04",
            "may": "05",
            "june": "06",
            "jun": "06",
            "july": "07",
            "jul": "07",
            "august": "08",
            "aug": "08",
            "september": "09",
            "sep": "09",
            "sept": "09",
            "october": "10",
            "oct": "10",
            "november": "11",
            "nov": "11",
            "december": "12",
            "dec": "12",
        }
        parts = match.groups()
        if parts[0].isdigit():
            day, month_name, year = parts
        else:
            month_name, day, year = parts
        month = month_names.get(month_name.lower(), "01")
        if len(year) == 2:
            year = "20" + year if int(year) < 50 else "19" + year
        return f"{month}-{day.zfill(2)}-{year}"

    return date_str


def to_iso_date(mm_dd_yyyy: str) -> Optional[str]:
    if not mm_dd_yyyy or mm_dd_yyyy in ("unknown", "null"):
        return None
    m = re.match(r"^(\d{2})-(\d{2})-(\d{4})$", mm_dd_yyyy.strip())
    if not m:
        return None
    month, day, year = m.groups()
    try:
        datetime(int(year), int(month), int(day))
    except ValueError:
        return None
    return f"{year}-{month}-{day}"


def _slice_first_n_words(text: str, n: int = REGEX_WINDOW_WORDS) -> str:
    count = 0
    end_idx = None
    for m in re.finditer(r"\b\w+\b", text):
        count += 1
        if count >= n:
            end_idx = m.end()
            break
    return text if end_idx is None else text[:end_idx]


def _slice_last_n_words(text: str, n: int = REGEX_WINDOW_WORDS) -> str:
    """Bottom / end-of-page window (same ~50–60 word budget as the top)."""
    matches = list(re.finditer(r"\b\w+\b", text))
    if len(matches) <= n:
        return text
    start_idx = matches[-n].start()
    return text[start_idx:]


def _hit_rank(hit: Optional[dict]) -> int:
    """Higher = better. Prefer admit+discharge pairs over single-date hits."""
    if not hit or not hit.get("dos_from") or hit["dos_from"] == "unknown":
        return -1
    mt = hit.get("match_type") or ""
    if mt in ("regex_admit_discharge", "regex_range", "admit_discharge_label"):
        return 3
    if mt in ("admit_label", "discharge_label"):
        return 2
    if mt == "date_outpatient_inpatient":
        return 2
    return 1


def _merge_dos_hits(*hits: Optional[dict]) -> Optional[dict]:
    """Pick best hit; if one window has From and another To, combine them."""
    valid = [h for h in hits if h and h.get("dos_from") and h["dos_from"] != "unknown"]
    if not valid:
        return None

    best = max(valid, key=_hit_rank)
    if best.get("match_type") in (
        "regex_admit_discharge",
        "regex_range",
        "admit_discharge_label",
    ):
        return best

    from_hit = next(
        (
            h
            for h in valid
            if (h.get("keyword") or "").upper().split("+")[0] in FROM_KEYWORDS
            or any(
                part.strip().upper() in FROM_KEYWORDS
                for part in (h.get("keyword") or "").split("+")
            )
        ),
        None,
    )
    to_hit = next(
        (
            h
            for h in valid
            if any(
                part.strip().upper() in TO_KEYWORDS
                for part in (h.get("keyword") or "").split("+")
            )
        ),
        None,
    )
    # Prefer labeled admit/discharge merges across windows
    labeled_from = next(
        (h for h in valid if h.get("match_type") == "admit_label"), None
    )
    labeled_to = next(
        (h for h in valid if h.get("match_type") == "discharge_label"), None
    )
    if labeled_from and labeled_to:
        return {
            "dos_from": labeled_from["dos_from"],
            "dos_to": labeled_to["dos_from"],
            "raw_date": f"{labeled_from['dos_from']} → {labeled_to['dos_from']}",
            "keyword": "Admit+Discharge",
            "match_type": "admit_discharge_label",
        }
    if from_hit and to_hit and from_hit is not to_hit:
        return {
            "dos_from": from_hit["dos_from"],
            "dos_to": to_hit.get("dos_to") or to_hit["dos_from"],
            "raw_date": f"{from_hit['dos_from']} → {to_hit['dos_from']}",
            "keyword": f"{from_hit.get('keyword')}+{to_hit.get('keyword')}",
            "match_type": "regex_admit_discharge",
        }
    return best


def extract_admit_discharge_labels(text: str) -> Optional[dict]:
    """Find Admit … <date> and/or Discharge … <date> labeled patterns."""
    if not text or not text.strip():
        return None

    admit_m = ADMIT_DATE_LABEL_RE.search(text)
    discharge_m = DISCHARGE_DATE_LABEL_RE.search(text)

    admit_norm = normalize_date(admit_m.group(1)) if admit_m else None
    discharge_norm = normalize_date(discharge_m.group(1)) if discharge_m else None
    if admit_norm == "unknown":
        admit_norm = None
    if discharge_norm == "unknown":
        discharge_norm = None

    if admit_norm and discharge_norm:
        return {
            "dos_from": admit_norm,
            "dos_to": discharge_norm,
            "raw_date": f"{admit_m.group(0)} / {discharge_m.group(0)}",
            "keyword": "Admit+Discharge",
            "match_type": "admit_discharge_label",
        }
    if admit_norm:
        return {
            "dos_from": admit_norm,
            "dos_to": admit_norm,
            "raw_date": admit_m.group(0),
            "keyword": "Admit",
            "match_type": "admit_label",
        }
    if discharge_norm:
        return {
            "dos_from": discharge_norm,
            "dos_to": discharge_norm,
            "raw_date": discharge_m.group(0),
            "keyword": "Discharge",
            "match_type": "discharge_label",
        }
    return None


def _year_of(norm: str) -> Optional[int]:
    """MM-DD-YYYY → year int."""
    parts = (norm or "").split("-")
    if len(parts) == 3 and parts[2].isdigit() and len(parts[2]) == 4:
        return int(parts[2])
    return None


def _sort_norm_dates(dates: list[str]) -> list[str]:
    def key(d: str) -> tuple:
        y = _year_of(d) or 0
        parts = d.split("-")
        try:
            return (y, int(parts[0]), int(parts[1]))
        except (ValueError, IndexError):
            return (y, 0, 0)

    return sorted(set(dates), key=key)


def _collect_keyword_dates(text: str) -> list[tuple[str, str]]:
    """All (normalized_date, keyword) hits on full text (not just first)."""
    if not text or not text.strip():
        return []
    text_lower = text.lower()
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    for keyword in VISIT_KEYWORDS:
        hit = _date_after_keyword(text, text_lower, keyword)
        if not hit:
            continue
        norm, _raw = hit
        if "|" in norm:
            a, b = norm.split("|", 1)
            for part in (a, b):
                if part != "unknown" and part not in seen:
                    seen.add(part)
                    found.append((part, keyword))
            continue
        if norm == "unknown" or norm in seen:
            continue
        seen.add(norm)
        found.append((norm, keyword))
    return found


def extract_dos_from_page_text(page_text: str) -> Optional[dict]:
    """
    Regex DOS on the **full page** (DOS can sit in the middle).

    - Single clear hit also visible in top/bottom ~60 words → confidence 0.95
    - Hit only via full-page (middle) scan → confidence 0.70
    - Multiple same-year dates → confidence 0.60
    """
    if not page_text or not page_text.strip():
        return None

    first_60 = _slice_first_n_words(page_text, REGEX_WINDOW_WORDS)
    last_60 = _slice_last_n_words(page_text, REGEX_WINDOW_WORDS)
    edge_hit = _merge_dos_hits(
        extract_admit_discharge_labels(last_60),
        extract_admit_discharge_labels(first_60),
        extract_date_with_keyword_info(last_60),
        extract_date_with_keyword_info(first_60),
    )

    full_labels = extract_admit_discharge_labels(page_text)
    # Prefer explicit Admit+Discharge range on full page
    if full_labels and full_labels.get("match_type") == "admit_discharge_label":
        edge_same = (
            edge_hit
            and edge_hit.get("dos_from") == full_labels.get("dos_from")
            and (edge_hit.get("dos_to") or edge_hit.get("dos_from"))
            == full_labels.get("dos_to")
        )
        out = dict(full_labels)
        out["confidence"] = CONF_EDGE if edge_same else CONF_FULL_PAGE
        return out

    keyword_dates = _collect_keyword_dates(page_text)
    # Also fold in single-side admit/discharge labels
    if full_labels:
        for key in ("dos_from", "dos_to"):
            val = full_labels.get(key)
            if val and val != "unknown":
                keyword_dates.append((val, full_labels.get("keyword") or "label"))

    norms = _sort_norm_dates([d for d, _ in keyword_dates if d and d != "unknown"])
    if not norms and edge_hit:
        out = dict(edge_hit)
        out["confidence"] = CONF_EDGE
        return out
    if not norms:
        return None

    # Prefer dates sharing one year (most common year among hits)
    years = [_year_of(d) for d in norms if _year_of(d) is not None]
    if years:
        year_counts: dict[int, int] = {}
        for y in years:
            year_counts[y] = year_counts.get(y, 0) + 1
        dominant_year = max(year_counts.items(), key=lambda t: (t[1], t[0]))[0]
        same_year = [d for d in norms if _year_of(d) == dominant_year]
    else:
        same_year = norms

    edge_norms: set[str] = set()
    if edge_hit:
        for key in ("dos_from", "dos_to"):
            v = edge_hit.get(key)
            if v and v != "unknown":
                edge_norms.add(v)

    if len(same_year) == 1:
        d = same_year[0]
        in_edge = d in edge_norms
        return {
            "dos_from": d,
            "dos_to": d,
            "raw_date": d,
            "keyword": keyword_dates[0][1] if keyword_dates else None,
            "match_type": "regex" if in_edge else "regex_full_page",
            "confidence": CONF_EDGE if in_edge else CONF_FULL_PAGE,
        }

    # Multiple dates around the same year → list them all
    listed = ", ".join(same_year)
    return {
        "dos_from": listed,
        "dos_to": listed,
        "raw_date": listed,
        "keyword": "MULTIPLE_SAME_YEAR",
        "match_type": "regex_multi_same_year",
        "confidence": CONF_MULTI,
        "dos_dates": same_year,
    }


def _combined_date_regex() -> re.Pattern[str]:
    return re.compile("(?:" + "|".join(DATE_REGEXES) + ")", re.IGNORECASE)


def page_allows_llm(page_text: str) -> bool:
    """True when page looks like a clinical note section LLM may help with."""
    return bool(LLM_SECTION_CUES.search(page_text or ""))


def is_discharge_like(page_text: str) -> bool:
    return bool(DISCHARGE_CUE.search(page_text or ""))


def is_non_encounter_page(page_text: str) -> bool:
    """Immunization / med list / facesheet-style pages → use default doc DOS."""
    return bool(NON_ENCOUNTER_CUE.search(page_text or ""))


def _hit(
    page_label: str,
    page_number: Any,
    dos_from: Optional[str],
    dos_to: Optional[str] = None,
    *,
    match_type: str,
    keyword: Optional[str] = None,
    doc_dos_from: Optional[str] = None,
    doc_dos_to: Optional[str] = None,
    confidence: Optional[float] = None,
) -> dict:
    # Page-level: blank when not extracted on this page
    page_from = dos_from if dos_from and dos_from != "unknown" else None
    page_to = None
    if page_from:
        page_to = dos_to if dos_to and dos_to != "unknown" else page_from

    d_from = doc_dos_from or page_from
    d_to = doc_dos_to or page_to or d_from

    # ISO: first/last when comma-separated multi dates
    def _first_iso(raw: Optional[str]) -> str:
        if not raw:
            return ""
        first = raw.split(",")[0].strip()
        return to_iso_date(first) if first else ""

    def _last_iso(raw: Optional[str]) -> str:
        if not raw:
            return ""
        last = raw.split(",")[-1].strip()
        return to_iso_date(last) if last else ""

    return {
        "page_name": page_label,
        "page_number": page_number,
        "dos_from": page_from or "",
        "dos_to": page_to or "",
        "dos_from_iso": _first_iso(page_from),
        "dos_to_iso": _last_iso(page_to) if page_to else "",
        "doc_dos_from": d_from or "",
        "doc_dos_to": d_to or "",
        "doc_dos_from_iso": _first_iso(d_from),
        "doc_dos_to_iso": _last_iso(d_to) if d_to else "",
        "match_type": match_type,
        "keyword": keyword,
        "confidence": confidence,
    }


def detect_dos_per_page(
    text: str,
    client: Any | None,
    *,
    use_llm: bool = True,
) -> list[dict]:
    """
    One row per page.

    Page-level (`dos_from` / `dos_to`):
      - Filled only when DOS is extracted on that page; otherwise blank.

    Document-level (`doc_dos_from` / `doc_dos_to`):
      - Carry forward previous encounter DOS.
      - Before first encounter DOS, or immunization / similar pages → 02-02-2022.
    """
    pages = split_ocr_into_pages(text)
    rows: list[dict] = []
    current_doc_from: Optional[str] = None
    current_doc_to: Optional[str] = None
    latest_ref: Optional[str] = None

    for page in pages:
        page_text = text[page["start"] : page["end"]]
        page_label = page.get("page_name") or str(page.get("page"))

        cleaned = re.sub(
            r"-----\s*Page\s*\d+[^\n]*-----\s*", "", page_text, flags=re.IGNORECASE
        )
        cleaned = re.sub(r"[#\-*_=]", "", cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned.strip()).strip().upper()
        if cleaned == "UNACCEPT":
            break

        page_from: Optional[str] = None
        page_to: Optional[str] = None
        match_type = ""
        keyword: Optional[str] = None
        confidence: Optional[float] = None

        regex_hit = extract_dos_from_page_text(page_text)
        if regex_hit and regex_hit.get("dos_from") and regex_hit["dos_from"] != "unknown":
            page_from = regex_hit["dos_from"]
            page_to = regex_hit.get("dos_to") or page_from
            match_type = regex_hit.get("match_type", "regex")
            keyword = regex_hit.get("keyword")
            confidence = float(regex_hit.get("confidence") or CONF_EDGE)
        elif use_llm and client is not None and page_allows_llm(page_text):
            pair = extract_dos_range_with_llm(
                page_text,
                page_label,
                client,
                reference_date=latest_ref,
                discharge_like=is_discharge_like(page_text),
            )
            if pair:
                page_from, page_to = pair
                match_type = "llm"
                confidence = CONF_FULL_PAGE

        # Document-level DOS
        if is_non_encounter_page(page_text) and not page_from:
            doc_from = DEFAULT_DOC_DOS
            doc_to = DEFAULT_DOC_DOS
            if not match_type:
                match_type = "non_encounter_default"
            # Hardcoded 2/2/2022 → 80%
            confidence = CONF_DEFAULT
        elif page_from:
            # New encounter — update carry-forward (use first date if multi-list)
            current_doc_from = page_from.split(",")[0].strip()
            current_doc_to = (page_to or page_from).split(",")[-1].strip()
            latest_ref = current_doc_from
            doc_from = page_from
            doc_to = page_to or page_from
            if confidence is None:
                confidence = CONF_EDGE
        elif current_doc_from:
            # Inherit previous encounter
            doc_from = current_doc_from
            doc_to = current_doc_to or current_doc_from
            if not match_type:
                match_type = "carry_forward"
            if confidence is None:
                confidence = CONF_EDGE
        else:
            # Before first DOS — hardcoded default
            doc_from = DEFAULT_DOC_DOS
            doc_to = DEFAULT_DOC_DOS
            if not match_type:
                match_type = "preamble_default"
            confidence = CONF_DEFAULT

        rows.append(
            _hit(
                page_label,
                page.get("page"),
                page_from,
                page_to,
                match_type=match_type,
                keyword=keyword,
                doc_dos_from=doc_from,
                doc_dos_to=doc_to,
                confidence=confidence,
            )
        )

    return rows


def _date_after_keyword(text: str, text_lower: str, keyword: str) -> Optional[tuple[str, str]]:
    """Return (normalized, raw) date appearing shortly after keyword, or None."""
    keyword_lower = keyword.lower()
    kw_pattern = r"\b" + re.escape(keyword_lower) + r"\b"
    kw_match = re.search(kw_pattern, text_lower)
    if not kw_match:
        return None

    if keyword_lower in ("date of service", "dateofservice", "dos"):
        context_before = text_lower[max(0, kw_match.start() - 50) : kw_match.start()]
        verb_patterns = [
            r"\badministered\s+on\b",
            r"\bperformed\s+on\b",
            r"\bgiven\s+on\b",
            r"\bdelivered\s+on\b",
            r"\b(endoscopy|colonoscopy|biopsy|mammogram|procedure|test|exam)\s+\d",
            r"\breceived\s+(upper\s+)?(endoscopy|colonoscopy|biopsy|mammogram|procedure)",
            r"\breviewed\s+(the\s+)?(patient'?s\s+)?(psa|bone\s+densitometry|test|procedure)",
        ]
        if any(re.search(p, context_before) for p in verb_patterns):
            return None

    date_rx = _combined_date_regex()
    search_start = kw_match.end()
    search_end = min(search_start + 80, len(text))
    date_search_text = text[search_start:search_end]

    range_after = RANGE_RE.search(date_search_text)
    if range_after:
        d_from = normalize_date(range_after.group(1))
        d_to = normalize_date(range_after.group(2))
        if d_from != "unknown" and d_to != "unknown":
            # Caller handles ranges; signal via special raw prefix
            return (f"{d_from}|{d_to}", range_after.group(0))

    date_match = date_rx.search(date_search_text)
    if not date_match:
        return None

    date_pos = search_start + date_match.start()
    context_before_date = text_lower[max(0, date_pos - 30) : date_pos]
    if any(re.search(p, context_before_date) for p in EXCLUSION_PATTERNS):
        return None

    raw_date = date_match.group(0)
    norm = normalize_date(raw_date)
    if norm == "unknown":
        return None
    return norm, raw_date


def extract_date_with_keyword_info(text: str) -> Optional[dict]:
    """
    Regex DOS near visit / admit / discharge keywords.
    - Admit/From + Discharge/To → dos_from / dos_to
    - Single date → dos_from == dos_to
    """
    text_lower = text.lower()

    # Explicit A–B range anywhere in the window
    range_match = RANGE_RE.search(text)
    if range_match:
        d_from = normalize_date(range_match.group(1))
        d_to = normalize_date(range_match.group(2))
        if d_from != "unknown" and d_to != "unknown":
            return {
                "dos_from": d_from,
                "dos_to": d_to,
                "raw_date": range_match.group(0),
                "keyword": "DATE_RANGE",
                "match_type": "regex_range",
            }

    from_date: Optional[str] = None
    to_date: Optional[str] = None
    from_kw: Optional[str] = None
    to_kw: Optional[str] = None
    generic: Optional[tuple[str, str, str]] = None  # norm, raw, keyword

    for keyword in VISIT_KEYWORDS:
        hit = _date_after_keyword(text, text_lower, keyword)
        if not hit:
            continue
        norm, raw = hit
        # Inline range encoded as from|to
        if "|" in norm:
            a, b = norm.split("|", 1)
            return {
                "dos_from": a,
                "dos_to": b,
                "raw_date": raw,
                "keyword": keyword,
                "match_type": "regex_range",
            }

        key_upper = keyword.upper()
        if key_upper in FROM_KEYWORDS or keyword.upper() in FROM_KEYWORDS:
            if from_date is None:
                from_date, from_kw = norm, keyword
        elif key_upper in TO_KEYWORDS or keyword.upper() in TO_KEYWORDS:
            if to_date is None:
                to_date, to_kw = norm, keyword
        elif generic is None:
            generic = (norm, raw, keyword)

    if from_date and to_date:
        return {
            "dos_from": from_date,
            "dos_to": to_date,
            "raw_date": f"{from_date} → {to_date}",
            "keyword": f"{from_kw}+{to_kw}",
            "match_type": "regex_admit_discharge",
        }
    if from_date and not to_date:
        return {
            "dos_from": from_date,
            "dos_to": from_date,
            "raw_date": from_date,
            "keyword": from_kw,
            "match_type": "regex",
        }
    if to_date and not from_date:
        return {
            "dos_from": to_date,
            "dos_to": to_date,
            "raw_date": to_date,
            "keyword": to_kw,
            "match_type": "regex",
        }
    if generic:
        norm, raw, keyword = generic
        return {
            "dos_from": norm,
            "dos_to": norm,
            "raw_date": raw,
            "keyword": keyword,
            "match_type": "regex",
        }

    date_outpatient_pattern = re.compile(
        r"\b(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}[/-]\d{1,2}[/-]\d{1,2}|"
        r"\d{1,2}\s+[A-Za-z]{3,9}\s*,?\s*\d{2,4}|[A-Za-z]{3,9}\s+\d{1,2},?\s*\d{2,4})"
        r"\s*[-:]?\s*(outpatient|inpatient)\s*:?\b",
        re.IGNORECASE,
    )
    match = date_outpatient_pattern.search(text)
    if match:
        raw_date = match.group(1)
        norm = normalize_date(raw_date)
        if norm and norm != "unknown":
            return {
                "dos_from": norm,
                "dos_to": norm,
                "raw_date": raw_date,
                "keyword": match.group(2).title(),
                "match_type": "date_outpatient_inpatient",
            }
    return None


def extract_dos_range_with_llm(
    page_text: str,
    page_label: str,
    client: Any,
    reference_date: Optional[str] = None,
    *,
    discharge_like: bool = False,
) -> Optional[tuple[str, str]]:
    """
    LLM DOS extract. Returns (dos_from, dos_to) in MM-DD-YYYY, or None.
    For discharge-like notes, asks for admission/from and discharge/to.
    """
    if client is None or not page_text.strip():
        return None

    # Prefer end-of-page text (last ~60 words) plus a short top window
    top = _slice_first_n_words(page_text, REGEX_WINDOW_WORDS)
    bottom = _slice_last_n_words(page_text, REGEX_WINDOW_WORDS)
    if top.strip() == bottom.strip():
        snippet = top
    else:
        snippet = f"[TOP]\n{top}\n\n[BOTTOM]\n{bottom}"
    today_str = datetime.now().strftime("%m-%d-%Y")
    mode = (
        "This looks like a discharge / inpatient note. Extract BOTH "
        "admission (DOS From) and discharge (DOS To) when present. "
        "If only one visit date exists, set dos_to = dos_from."
        if discharge_like
        else "Extract the visit Date of Service. For a single-day visit, "
        "set dos_to = dos_from. If a date range is explicit, use both ends."
    )

    prompt = f"""You are a STRICT medical document parser. Extract Date of Service From/To.

{mode}

CRITICAL RULES:
1. Extract only explicit visit / admission / discharge / DOS labels — not labs, procedures, follow-ups, or narrative history.
2. When in doubt, return NO_VISIT_DATE_FOUND.
3. Dates must be MM-DD-YYYY (zero-padded).

Respond with ONLY JSON (no markdown):
{{"dos_from":"MM-DD-YYYY","dos_to":"MM-DD-YYYY"}}
or
{{"dos_from":null,"dos_to":null}}

Today's date (year reference): {today_str}
Reference date if helpful: {reference_date or "none"}

Text excerpt:
{snippet}
"""

    try:
        response = client.chat.completions.create(
            model=azure_deployment(),
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Extract clinical DOS from/to as JSON only. "
                        "Prefer explicit labels. For discharge notes return both ends."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=80,
            timeout=30,
        )
        raw = (response.choices[0].message.content or "").strip()
        raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        if "NO_VISIT_DATE_FOUND" in raw.upper() and "{" not in raw:
            return None
        data = json.loads(raw)
        d_from = data.get("dos_from")
        d_to = data.get("dos_to")
        if not d_from:
            return None
        n_from = normalize_date(str(d_from), reference_date=reference_date)
        if n_from == "unknown":
            return None
        if d_to:
            n_to = normalize_date(str(d_to), reference_date=reference_date)
            if n_to == "unknown":
                n_to = n_from
        else:
            n_to = n_from
        return n_from, n_to
    except Exception as exc:
        print(f"  [warn] LLM DOS failed on {page_label}: {exc}")
        return None


def split_ocr_into_pages(text: str) -> list[dict]:
    ui_matches = list(UI_PAGE_MARKER_RE.finditer(text))
    if ui_matches:
        pages: list[dict] = []
        for i, m in enumerate(ui_matches):
            start = m.end()
            end = ui_matches[i + 1].start() if i + 1 < len(ui_matches) else len(text)
            pages.append(
                {
                    "index": i,
                    "page": i + 1,
                    "page_name": m.group(1).strip(),
                    "start": start,
                    "end": end,
                }
            )
        return pages

    ac_matches = list(AUTOCODER_PAGE_RE.finditer(text))
    if ac_matches:
        pages = []
        for i, m in enumerate(ac_matches):
            start = m.end()
            end = ac_matches[i + 1].start() if i + 1 < len(ac_matches) else len(text)
            pages.append(
                {
                    "index": i,
                    "page": int(m.group(2)),
                    "page_name": f"{int(m.group(2))}.jpg",
                    "start": start,
                    "end": end,
                }
            )
        return pages

    return [{"index": 0, "page": 1, "page_name": "1.jpg", "start": 0, "end": len(text)}]
