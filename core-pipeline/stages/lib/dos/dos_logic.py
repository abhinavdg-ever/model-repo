"""Date of service: the key/value date, else a labelled date at the top or bottom of the page.

Per page, two states:

  1. Key/value — the key/value extractor's best date for the page: the pair its rules
     accepted that the model scored highest, at any score. The confidence is that score.
  2. Edge keyword, only when key/value has no date — a DOS label (Encounter Date, Visit
     Date, Date of Service, Admission Date, Discharge Date, ...) that starts within the
     first or last ``edge_words`` (20) words of the page, with a date right after it. An
     encounter label wins; otherwise an admission date is From and a discharge date To;
     a discharge date alone is both. Read on the final OCR text first, then on the
     Tesseract text the stage passes for pages without Final2: Docling can drop a page's
     top banner (a page header).

Otherwise the page has no date of service.

Each page keeps its own date here: this is the Extracted DOS. The Final DOS
(the document's first page's, on every page) is ``continuity.engine.document_finals``.

Labels and settings live in ``keyword-canon/dos_canon.json`` (reloads on change).
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any, Optional

from stages.lib.canon_store import CANON_DIR, CanonFile


# --- editable config ---------------------------------------------------------


LABEL_CLASSES = ("encounter", "admit", "discharge")


def _any_of(patterns: Optional[list[str]]) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in patterns or [r"(?!)"]), re.IGNORECASE)


@dataclass(frozen=True)
class DosProfile:
    default_date: str  # ISO
    edge_words: int
    # How far after a label its date may start, and the only words allowed between.
    label_reach_chars: int
    label_gap_words: frozenset[str]
    confidence: dict[str, float]  # edge; kv when the extractor has no score
    label_re: re.Pattern[str]
    label_class: dict[str, str]  # letters-only phrase -> class
    # A page matching one of these is a discharge document (admit / discharge dates win).
    discharge_cues: re.Pattern[str]
    # A page matching one of these is a lab report.
    lab_cues: re.Pattern[str]
    # Page kind (discharge / lab / other) -> key groups in priority order.
    kv_priority: dict[str, list[list[str]]]


def _letters(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())


def _phrase_pattern(phrase: str) -> str:
    # "date of service" also matches OCR-glued "dateofservice".
    return r"\s*".join(re.escape(word) for word in phrase.split())


def _build_profile(data: dict[str, Any]) -> DosProfile:
    label_class: dict[str, str] = {}
    phrases: list[str] = []
    for cls in LABEL_CLASSES:
        for phrase in data["labels"].get(cls, []):
            label_class[_letters(phrase)] = cls
            phrases.append(phrase.lower())
    # Longest first, so "dos from" wins over "dos" at the same position.
    phrases.sort(key=len, reverse=True)
    label_re = re.compile(
        r"(?<![a-z])(?:" + "|".join(_phrase_pattern(p) for p in phrases) + r")(?![a-z])",
        re.IGNORECASE,
    )
    datetime.strptime(data["DOS_DEFAULT_DATE"], "%Y-%m-%d")
    return DosProfile(
        default_date=data["DOS_DEFAULT_DATE"],
        edge_words=int(data["edge_words"]),
        label_reach_chars=int(data["label_reach_chars"]),
        label_gap_words=frozenset(w.casefold() for w in data["label_gap_words"]),
        confidence={k: float(v) for k, v in data["confidence"].items()},
        label_re=label_re,
        label_class=label_class,
        discharge_cues=_any_of(data.get("discharge_document_cues")),
        lab_cues=_any_of(data.get("lab_document_cues")),
        kv_priority={kind: [list(group) for group in groups] for kind, groups in (data.get("kv_priority") or {}).items()},
    )


# keyword-canon/dos_canon.json — reloaded when the file changes.
_PROFILE: CanonFile[DosProfile] = CanonFile(CANON_DIR / "dos_canon.json", _build_profile)


def profile() -> DosProfile:
    return _PROFILE.get()


# --- page splitting ----------------------------------------------------------

UI_PAGE_MARKER_RE = re.compile(r"^=====\s*(.+?)\s*=====\s*$", re.MULTILINE)
AUTOCODER_PAGE_RE = re.compile(
    r"(^|\n)-----\s*Page\s*(\d+)[^\n]*\n", re.IGNORECASE
)


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


# --- Stage A: candidates -----------------------------------------------------

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_NAME = (
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?"
    r"|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)

# One combined pattern. The numeric shapes take / or -, used consistently.
DATE_RE = re.compile(
    r"(?<![\d/-])(?:"
    r"(?P<iy>\d{4})(?P<isep>[-/])(?P<im>\d{1,2})(?P=isep)(?P<id>\d{1,2})"
    # Also '05 / 20 / 2025' (spaced slashes, the same both times); never a spaced '-'.
    r"|(?P<nm>\d{1,2})(?P<nsep>[/-]| ?/ ?)(?P<nd>\d{1,2})(?P=nsep)(?P<ny>\d{4}|\d{2})"
    r"|(?P<tm>" + _MONTH_NAME + r")\.?\s+(?P<td>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<ty>\d{4})"
    r"|(?P<dd>\d{1,2})(?:st|nd|rd|th)?\s+(?P<dm>" + _MONTH_NAME + r")\.?,?\s+(?P<dy>\d{4})"
    r")(?!\d)",
    re.IGNORECASE,
)


def _two_digit_year(year: str) -> int:
    value = int(year)
    if len(year) == 2:
        return 2000 + value if value < 50 else 1900 + value
    return value


def parse_date_match(match: re.Match[str]) -> Optional[str]:
    """ISO date for a DATE_RE match, or None when it is not a real day (02/30)."""
    g = match.groupdict()
    if g["iy"]:
        y, m, d = int(g["iy"]), int(g["im"]), int(g["id"])
    elif g["nm"]:
        y, m, d = _two_digit_year(g["ny"]), int(g["nm"]), int(g["nd"])
    elif g["tm"]:
        y, m, d = int(g["ty"]), _MONTHS[g["tm"][:3].lower()], int(g["td"])
    else:
        y, m, d = int(g["dy"]), _MONTHS[g["dm"][:3].lower()], int(g["dd"])
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


# --- 2. edge keyword --------------------------------------------------------


@dataclass
class LabelledDate:
    """A DOS label at the page's edge and the date right after it."""

    label: str
    label_class: str  # encounter | admit | discharge
    edge: str  # top | bottom
    raw: str
    iso: str
    end: int  # offset just past the date
    chosen: bool = False


def edge_spans(page_text: str, n_words: int) -> list[tuple[str, int, int]]:
    """Character spans of the first and the last ``n_words`` words.

    A word is a whitespace-separated token with a letter or digit in it, so
    table pipes and bullets do not count. On a short page the two spans overlap
    and cover the whole page."""
    words = [m for m in re.finditer(r"\S+", page_text) if re.search(r"[A-Za-z0-9]", m.group(0))]
    if not words:
        return []
    top = words[: n_words]
    bottom = words[-n_words:]
    return [
        ("top", top[0].start(), top[-1].end()),
        ("bottom", bottom[0].start(), bottom[-1].end()),
    ]


def _overlapping(pattern: re.Pattern[str], text: str, lo: int, hi: int):
    """Every match starting in [lo, hi), including ones that overlap."""
    position = lo
    while (match := pattern.search(text, position)) is not None and match.start() < hi:
        yield match
        position = match.start() + 1


def edge_dates(page_text: str, prof: Optional[DosProfile] = None) -> list[LabelledDate]:
    """Every DOS label starting inside an edge span, with the date after it.

    The date must start within ``label_reach_chars`` of the label, with only
    ``label_gap_words`` between ("Encounter Date | at January 28, 2025",
    "Visit Date/Time: ..."), so a label never takes a date that belongs to the
    next field ("Visit Date: | DOB: 01/02/1960")."""
    prof = prof or profile()
    found: list[LabelledDate] = []
    seen: set[int] = set()
    for edge, lo, hi in edge_spans(page_text, prof.edge_words):
        # Overlapping: "Office Visit  Date of Service" holds "visit date" and
        # "date of service"; only the second has its date right after it.
        for label in _overlapping(prof.label_re, page_text, lo, hi):
            if label.start() in seen:
                continue
            seen.add(label.start())
            reach = page_text[label.end() : label.end() + prof.label_reach_chars]
            match = DATE_RE.search(reach)
            if match is None:
                continue
            gap = reach[: match.start()]
            if any(w.casefold() not in prof.label_gap_words for w in re.findall(r"[A-Za-z]+", gap)):
                continue
            if _DICTATION_BEFORE.search(page_text, 0, label.end() + match.start()):
                # 'D: 03/01/2024 T: 03/04/2024': dictated / transcribed, not a visit date.
                continue
            iso = parse_date_match(match)
            if iso is None:
                continue
            found.append(
                LabelledDate(
                    label=re.sub(r"\s+", " ", label.group(0)).strip(),
                    label_class=prof.label_class.get(_letters(label.group(0)), "encounter"),
                    edge=edge,
                    raw=match.group(0),
                    iso=iso,
                    end=label.end() + match.end(),
                )
            )
    return found


# A date right after one of these is when the note was dictated or transcribed ('D: 03/01/2024
# T: 03/04/2024', 'DT: ...', 'Dictated: ...'): never an edge DOS, never one end of a range.
_DICTATION_BEFORE = re.compile(
    r"(?:(?<![A-Za-z])(?:D|T|DD|DT|TD|TT)\s*:|(?i:\b(?:dictated|transcribed)(?:\s+(?:on|date))?\s*:?))\s*$"
)


# "03/01/2024 - 03/05/2024", "... to ...", "... thru / through ...".
_RANGE_TAIL_RE = re.compile(r"^\s*(?:-|–|—|to|thru|through)\s*", re.IGNORECASE)


def _range_end(page_text: str, hit: LabelledDate) -> str:
    """The end of a written range right after an encounter date, else ''."""
    tail = _RANGE_TAIL_RE.match(page_text[hit.end : hit.end + 12])
    if tail is None:
        return ""
    match = DATE_RE.match(page_text, hit.end + tail.end())
    iso = parse_date_match(match) if match else None
    if iso and _DICTATION_BEFORE.search(page_text, 0, hit.end + tail.end()):
        return ""
    return iso if iso and iso >= hit.iso else ""


@dataclass
class PageDate:
    dos_from: str  # ISO
    dos_to: str
    confidence: float
    keyword: str
    source: str  # "kv" | "edge" | "edge_tesseract" | "admit_discharge" | "dictation"
    is_pair: bool = False
    # A key/value date gave it (extraction_method kv).
    kv_backed: bool = False


def edge_page_date(
    page_text: str,
    prof: Optional[DosProfile] = None,
    hits: Optional[list[LabelledDate]] = None,
) -> Optional[PageDate]:
    """State 2: an encounter label first; else admission (From) + discharge (To).
    The chosen ``hits`` are marked ``chosen``."""
    prof = prof or profile()
    if hits is None:
        hits = edge_dates(page_text, prof)

    def confidence(*chosen: LabelledDate) -> float:
        return prof.confidence["edge"]

    encounter = next((h for h in hits if h.label_class == "encounter"), None)
    if encounter is not None:
        encounter.chosen = True
        end = _range_end(page_text, encounter)
        return PageDate(encounter.iso, end or encounter.iso, confidence(encounter), encounter.label,
                        "edge", is_pair=bool(end))
    admit = next((h for h in hits if h.label_class == "admit"), None)
    discharge = next(
        (h for h in hits if h.label_class == "discharge" and (admit is None or h.iso >= admit.iso)),
        None,
    )
    if admit is not None:
        admit.chosen = True
        if discharge is not None:
            discharge.chosen = True
            return PageDate(admit.iso, discharge.iso, confidence(admit, discharge),
                            f"{admit.label}+{discharge.label}", "edge", is_pair=True)
        return PageDate(admit.iso, admit.iso, confidence(admit), admit.label, "edge")
    if discharge is not None:
        discharge.chosen = True
        return PageDate(discharge.iso, discharge.iso, confidence(discharge), discharge.label, "edge")
    return None


# --- 1. key/value -------------------------------------------------------------


def page_kind(page_text: str, candidates: list[dict[str, Any]], hits: list[LabelledDate], prof: DosProfile) -> str:
    """discharge (a discharge date, or discharge wording), lab (lab wording, or a collected /
    received date), else other."""
    tiers = {item.get("tier") for item in candidates}
    if "discharge" in tiers or any(h.label_class == "discharge" for h in hits) or prof.discharge_cues.search(page_text):
        return "discharge"
    if tiers & {"collected", "received"} or prof.lab_cues.search(page_text):
        return "lab"
    return "other"


def _group(item: dict[str, Any]) -> str:
    """A candidate's priority group: its key tier, with the Report Date key on its own."""
    if str(item.get("keyword") or "").strip().casefold() == "report date":
        return "report"
    return str(item.get("tier") or "")


def kv_page_date(
    candidates: list[dict[str, Any]], kind: str = "other", prof: Optional[DosProfile] = None
) -> Optional[PageDate]:
    """State 1: among every key/value date the rules accepted, the first priority group of the
    page's kind that has one (``kv_priority``), and within it the date the model scored
    highest. A range keeps its end. Its confidence is that score (the rules' score without a
    model, else the canon's kv)."""
    prof = prof or profile()
    groups = prof.kv_priority.get(kind) or prof.kv_priority.get("other") or []
    rank = {name: i for i, group in enumerate(groups) for name in group}
    dated = [item for item in candidates or [] if len(str(item.get("iso") or "")) == 10]
    if not dated:
        return None

    def score(item: dict[str, Any]) -> float:
        value = item.get("confidence")
        return float(value) if value is not None else prof.confidence["kv"]

    item = min(dated, key=lambda it: (rank.get(_group(it), len(groups)), -score(it)))
    iso, iso_to = str(item["iso"]), str(item.get("iso_to") or "")
    is_pair = len(iso_to) == 10 and iso_to > iso
    return PageDate(iso, iso_to if is_pair else iso, score(item),
                    str(item.get("keyword") or ""), "kv", is_pair=is_pair)


def _kv_stay(candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The highest-scoring admit and discharge dates among the candidates."""
    stay: dict[str, dict[str, Any]] = {}
    for item in candidates:
        tier = item.get("tier")
        if tier in ("admit", "discharge") and len(str(item.get("iso") or "")) == 10:
            best = stay.get(tier)
            if best is None or float(item.get("confidence") or 0) > float(best.get("confidence") or 0):
                stay[tier] = item
    return stay


# Dictated (D:, DD:, Dictated) before transcribed (T:, DT:, TD:, TT:, Transcribed).
_DICTATION_MARKERS = (
    ("dictated", re.compile(r"(?:(?<![A-Za-z])(?:D|DD)\s*:|(?i:\bdictated(?:\s+(?:on|date))?\s*:?))\s*")),
    ("transcribed", re.compile(r"(?:(?<![A-Za-z])(?:T|DT|TD|TT)\s*:|(?i:\btranscribed(?:\s+(?:on|date))?\s*:?))\s*")),
)


def dictation_page_date(page_text: str, prof: Optional[DosProfile] = None) -> Optional[PageDate]:
    """Other pages, last: the dictated date, else the transcribed one ('D: 03/01/2024
    T: 03/04/2024' gives 03/01/2024)."""
    prof = prof or profile()
    for label, marker in _DICTATION_MARKERS:
        for found in marker.finditer(page_text):
            match = DATE_RE.match(page_text, found.end())
            iso = parse_date_match(match) if match else None
            if iso:
                return PageDate(iso, iso, prof.confidence["dictation"], label, "dictation")
    return None


def admit_discharge_date(
    page_text: str,
    kv_stay: dict[str, dict[str, Any]],
    hits: list[LabelledDate],
    prof: Optional[DosProfile] = None,
) -> Optional[PageDate]:
    """Discharge documents: the stay's Admit (From) and Discharge (To) dates take priority.

    A page is a discharge document when it has a discharge date (key/value or edge label)
    or matches ``discharge_document_cues``. ``kv_stay`` holds the key/value extractor's best
    ``admit`` / ``discharge`` date for the page; edge labels fill a side it lacks. A
    discharge date before the admit date is ignored; one date alone is both From and To."""
    prof = prof or profile()
    edge_side = {cls: next((h for h in hits if h.label_class == cls), None) for cls in ("admit", "discharge")}
    has_discharge = bool(kv_stay.get("discharge") or edge_side["discharge"])
    if not (has_discharge or prof.discharge_cues.search(page_text or "")):
        return None

    def side(cls: str) -> tuple[str, float | None, str] | None:
        item = kv_stay.get(cls)
        if item and len(str(item.get("iso") or "")) == 10:
            score = item.get("confidence")
            return str(item["iso"]), float(score) if score is not None else prof.confidence["kv"], str(item.get("keyword") or cls)
        hit = edge_side[cls]
        return (hit.iso, None, hit.label) if hit else None

    admit, discharge = side("admit"), side("discharge")
    if admit and discharge and discharge[0] < admit[0]:
        discharge = None
    used = [s for s in (admit, discharge) if s]
    if not used:
        return None
    start = (admit or discharge)[0]
    end = (discharge or admit)[0]
    kv_scores = [s[1] for s in used if s[1] is not None]
    confidence = max(kv_scores) if kv_scores else prof.confidence["edge"]
    return PageDate(start, end, confidence, "+".join(s[2] for s in used), "admit_discharge",
                    is_pair=end != start, kv_backed=bool(kv_scores))


# --- date formats ------------------------------------------------------------


def iso_to_mdy(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{m}-{d}-{y}"


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


# --- single page -------------------------------------------------------------


def extract_dos_from_page_text(page_text: str) -> Optional[dict]:
    """State 2 (edge keyword) alone on one page (no key/value)."""
    best = edge_page_date(page_text)
    if best is None:
        return None
    return {
        "dos_from": iso_to_mdy(best.dos_from),
        "dos_to": iso_to_mdy(best.dos_to),
        "keyword": best.keyword,
        "confidence": round(best.confidence, 4),
        "match_type": "admit_discharge_pair" if best.is_pair else "page_date",
    }


# --- the chart ----------------------------------------------------------------


_MATCH_TYPE = {
    "kv": "kv", "edge": "edge_keyword", "edge_tesseract": "edge_keyword_tesseract",
    "admit_discharge": "admit_discharge", "dictation": "dictation_date",
}


def _row(page: dict, page_date: Optional[PageDate], prof: DosProfile) -> dict:
    """One output row. MM-DD-YYYY columns plus their ISO twins.

    The document-level columns are the page's own date (DOS_DEFAULT_DATE when
    it has none); nothing is carried from another page here."""
    doc_from = page_date.dos_from if page_date else prof.default_date
    doc_to = page_date.dos_to if page_date else prof.default_date
    if page_date is None:
        match_type = "no_date_found"
    elif page_date.is_pair:
        match_type = "admit_discharge_pair"
    else:
        match_type = _MATCH_TYPE[page_date.source]
    return {
        "page_name": page.get("page_name") or str(page.get("page")),
        "page_number": page.get("page"),
        "dos_from": iso_to_mdy(page_date.dos_from) if page_date else "",
        "dos_to": iso_to_mdy(page_date.dos_to) if page_date else "",
        "dos_from_iso": page_date.dos_from if page_date else "",
        "dos_to_iso": page_date.dos_to if page_date else "",
        "doc_dos_from": iso_to_mdy(doc_from),
        "doc_dos_to": iso_to_mdy(doc_to),
        "doc_dos_from_iso": doc_from,
        "doc_dos_to_iso": doc_to,
        "match_type": match_type,
        "final_dos": page_date.dos_from if page_date else "",
        "keyword": page_date.keyword if page_date else None,
        "confidence": round(page_date.confidence, 4) if page_date else 0.0,
        "page_source": page_date.source if page_date else "",
        "kv_backed": bool(page_date and (page_date.source == "kv" or page_date.kv_backed)),
        "is_default": page_date is None,
    }


def detect_dos_per_page(
    text: str,
    *,
    candidate_log: Optional[list[dict]] = None,
    kv_dates: Optional[dict[str, list[dict[str, Any]]]] = None,
    tesseract_texts: Optional[dict[str, str]] = None,
) -> list[dict]:
    """One row per page: the key/value date chosen by the page kind's priority, else the edge
    keyword (final text, then Tesseract), else — on an "other" page — the dictated date,
    else the transcribed one, else nothing. Then, on a discharge document, its Admit /
    Discharge dates take priority (``admit_discharge_date``).

    ``kv_dates`` maps a page name to every key/value date the rules accepted (``iso``,
    optional ``iso_to``, ``tier``, ``keyword``, ``confidence`` = the model's score).
    ``tesseract_texts`` maps a
    page name to its Tesseract text; only its first and last ``edge_words`` words are read.
    ``candidate_log``, when passed, gets every edge label with its date and whether it was
    chosen.
    """
    prof = profile()
    rows: list[dict] = []
    for page in split_ocr_into_pages(text):
        page_text = text[page["start"] : page["end"]]
        cleaned = re.sub(
            r"-----\s*Page\s*\d+[^\n]*-----\s*", "", page_text, flags=re.IGNORECASE
        )
        cleaned = re.sub(r"[#\-*_=]", "", cleaned)
        if re.sub(r"\s+", " ", cleaned).strip().upper() == "UNACCEPT":
            break
        page_name = page.get("page_name") or ""

        # State 1: the key/value date, by the page kind's priority. The edge labels are read
        # first: they also tell a discharge page apart.
        candidates = (kv_dates or {}).get(page_name) or []
        hits = edge_dates(page_text, prof)
        kind = page_kind(page_text, candidates, hits, prof)
        found = kv_page_date(candidates, kind, prof)
        # State 2: no key/value date; the edge keyword.
        if found is None:
            found = edge_page_date(page_text, prof, hits)
        if found is None and (tesseract_texts or {}).get(page_name):
            tesseract_hits = edge_dates(tesseract_texts[page_name], prof)
            found = edge_page_date(tesseract_texts[page_name], prof, tesseract_hits)
            if found is not None:
                found.source = "edge_tesseract"
                for hit in tesseract_hits:
                    hit.edge = f"tesseract_{hit.edge}"
                hits = hits + tesseract_hits
        if found is None and kind == "other":
            found = dictation_page_date(page_text, prof)
        # Post-processing: a discharge document is dated by its stay.
        stay = admit_discharge_date(page_text, _kv_stay(candidates), hits, prof)
        if stay is not None:
            found = stay

        if candidate_log is not None:
            candidate_log.extend({"page_name": page_name, **asdict(hit)} for hit in hits)
        rows.append(_row(page, found, prof))
    return rows
