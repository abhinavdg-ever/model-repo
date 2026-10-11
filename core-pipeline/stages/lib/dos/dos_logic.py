"""Date of service: find every date, score each one, resolve the chart.

  A. Candidates — one sweep over each page for four date shapes. Every real
     calendar day is kept, birth dates and 1998 included.
  B. Features   — the nearest label to the left, where the date sits on the
     page, whether a clock time sits beside it (a print stamp), the page type,
     its age against the chart's received date, and how many other dates in
     the chart agree with it.
  C. Score      — a weighted sum from ``dos_canon.json``, clamped to
     [0, 1]. A key/value date is the page's date. Otherwise the best
     candidate must reach 0.80.
  D. Resolve    — each page keeps its own date. Demographics, injection
     pages, and pages with no date keep DOS_DEFAULT_DATE at document level.
     Carrying a note's date across its pages is the continuity stage's job
     (Final DOS), not this one's.

Nothing is vetoed. A DOB label or an old year is a large negative weight, so
a losing date keeps a score that says why it lost. Azure OpenAI is a fallback
for clinical pages where no candidate clears DOS_MIN_SCORE.
"""
from __future__ import annotations

import bisect
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Optional

from azure_llm import azure_deployment
from stages.lib.canon_store import CANON_DIR, CanonFile
from stages.lib.page_classify import keywords as page_keywords


# --- editable config ---------------------------------------------------------


def _any_of(patterns: list[str]) -> re.Pattern[str]:
    return re.compile(r"(" + "|".join(patterns) + r")", re.IGNORECASE)


LABEL_CLASSES = (
    "encounter", "admit", "discharge", "birth", "doc_meta", "future", "procedure",
)


@dataclass(frozen=True)
class DosProfile:
    max_age_years: int
    min_score: float
    default_date: str  # ISO
    base: float
    label_weights: dict[str, float]
    label_distance_decay: float
    edge_bonus: float
    clinical_cue_bonus: float
    cluster_bonus: float
    cluster_cap: int
    age_penalty: float
    timestamp_penalty: float
    llm_confidence: float
    window_left_chars: int
    window_right_chars: int
    edge_words: int
    cluster_days: int
    range_pair_chars: int
    label_re: re.Pattern[str]
    label_class: dict[str, str]  # letters-only phrase → class
    non_encounter_page_types: frozenset[str]
    default_page_types: frozenset[str]
    default_page_type_contains: tuple[str, ...]
    # has_clinical_cue, and the gate for the Azure OpenAI fallback.
    clinical_cues: re.Pattern[str]
    discharge_cues: re.Pattern[str]


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
    weights = {cls: float(w) for cls, w in data["label_weights"].items()}
    missing = {"none", *LABEL_CLASSES} - weights.keys()
    if missing:
        raise ValueError(f"label_weights is missing {sorted(missing)}")
    datetime.strptime(data["DOS_DEFAULT_DATE"], "%Y-%m-%d")
    return DosProfile(
        max_age_years=int(data["DOS_MAX_AGE_YEARS"]),
        min_score=float(data["DOS_MIN_SCORE"]),
        default_date=data["DOS_DEFAULT_DATE"],
        base=float(data["base"]),
        label_weights=weights,
        label_distance_decay=float(data["label_distance_decay"]),
        edge_bonus=float(data["edge_bonus"]),
        clinical_cue_bonus=float(data["clinical_cue_bonus"]),
        cluster_bonus=float(data["cluster_bonus"]),
        cluster_cap=int(data["cluster_cap"]),
        age_penalty=float(data["age_penalty"]),
        timestamp_penalty=float(data["timestamp_penalty"]),
        llm_confidence=float(data["llm_confidence"]),
        window_left_chars=int(data["window_left_chars"]),
        window_right_chars=int(data["window_right_chars"]),
        edge_words=int(data["edge_words"]),
        cluster_days=int(data["cluster_days"]),
        range_pair_chars=int(data["range_pair_chars"]),
        label_re=label_re,
        label_class=label_class,
        non_encounter_page_types=frozenset(
            t.casefold() for t in data["non_encounter_page_types"]
        ),
        default_page_types=frozenset(
            t.casefold() for t in data.get("default_page_types") or []
        ),
        default_page_type_contains=tuple(
            t.casefold() for t in data.get("default_page_type_contains") or []
        ),
        clinical_cues=_any_of(data["clinical_cues"]),
        discharge_cues=_any_of(data["discharge_cues"]),
    )


# keyword-canon/dos_canon.json — reloaded when the file changes.
_PROFILE: CanonFile[DosProfile] = CanonFile(CANON_DIR / "dos_canon.json", _build_profile)


def profile() -> DosProfile:
    return _PROFILE.get()


def _uses_default_date(page_type: str, prof: DosProfile) -> bool:
    """Demographics and injection pages keep the default date."""
    if page_type in prof.default_page_types:
        return True
    return any(part in page_type for part in prof.default_page_type_contains)


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
    r"|(?P<nm>\d{1,2})(?P<nsep>[/-])(?P<nd>\d{1,2})(?P=nsep)(?P<ny>\d{4}|\d{2})"
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


@dataclass
class Candidate:
    """One date on one page, its features, and its score."""

    page_index: int
    page_number: Any
    page_name: str
    raw: str
    iso: str
    start: int  # offset within the page text
    end: int
    label_text: str = ""
    label_class: str = "none"
    label_distance: Optional[int] = None
    position: float = 0.0
    edge_position: bool = False
    has_time: bool = False
    page_type: str = ""
    has_clinical_cue: bool = False
    year_delta: Optional[int] = None
    cluster_size: int = 0
    in_range_pair: bool = False
    pair_iso: Optional[str] = None
    context: str = ""
    score: float = 0.0
    chosen: bool = False
    # "text" from the page sweep, "kv" when the key/value extractor supplied it.
    origin: str = "text"
    pair: Optional["Candidate"] = field(default=None, repr=False, compare=False)

    def log_row(self) -> dict[str, Any]:
        row = asdict(self)
        row.pop("pair")
        row["score"] = round(self.score, 4)
        row["position"] = round(self.position, 4)
        return row


def find_candidates(
    page_text: str, *, page_index: int = 0, page_number: Any = 1, page_name: str = ""
) -> list[Candidate]:
    out: list[Candidate] = []
    for match in DATE_RE.finditer(page_text or ""):
        iso = parse_date_match(match)
        if iso is None:
            continue
        out.append(
            Candidate(
                page_index=page_index,
                page_number=page_number,
                page_name=page_name,
                raw=match.group(0),
                iso=iso,
                start=match.start(),
                end=match.end(),
            )
        )
    return out


# --- Stage B: features -------------------------------------------------------

# A clock time right after the date ("03/20/2024 10:15 AM", "2025-12-31T10:00")
# or right before it ("10:15 03/20/2024") — the shape of a print or fax stamp.
_TIME = r"\d{1,2}:\d{2}(?::\d{2})?(?:\s*[ap]\.?m\.?)?(?![\d:])"
_TIME_AFTER_RE = re.compile(r"^(?:T|,?\s*(?:at\s+|@\s*)?)" + _TIME, re.IGNORECASE)
_TIME_BEFORE_RE = re.compile(r"(?<![\d:])" + _TIME + r",?\s*$", re.IGNORECASE)


def _has_time(page_text: str, start: int, end: int) -> bool:
    return bool(
        _TIME_AFTER_RE.match(page_text[end : end + 20])
        or _TIME_BEFORE_RE.search(page_text[max(0, start - 20) : start])
    )



def _edge_bounds(page_text: str, n_words: int) -> tuple[int, int]:
    """(end of the first n words, start of the last n words)."""
    words = list(re.finditer(r"\b\w+\b", page_text))
    if len(words) <= 2 * n_words:
        return len(page_text), 0
    return words[n_words - 1].end(), words[-n_words].start()


def _nearest_label(
    page_text: str, lo: int, hi: int, prof: DosProfile
) -> Optional[re.Match[str]]:
    best: Optional[re.Match[str]] = None
    for match in prof.label_re.finditer(page_text, lo, hi):
        if best is None or match.end() >= best.end():
            best = match
    return best


def page_features(
    page_text: str,
    candidates: list[Candidate],
    *,
    page_type: str,
    received_year: int,
    prof: DosProfile,
) -> None:
    """Fill every feature that depends on this page alone."""
    length = max(1, len(page_text))
    first_end, last_start = _edge_bounds(page_text, prof.edge_words)
    clinical = bool(profile().clinical_cues.search(page_text))
    previous_end = 0
    for cand in candidates:
        # A label belongs to the date after it, so it never reaches past
        # an earlier date: "DOB 03/06/1972 ... 12/31/2025" leaves the second
        # date unlabelled.
        lo = max(previous_end, cand.start - prof.window_left_chars)
        label = None if cand.origin == "kv" else _nearest_label(page_text, lo, cand.start, prof)
        if label is not None:
            cand.label_text = re.sub(r"\s+", " ", label.group(0)).strip()
            cand.label_class = prof.label_class.get(_letters(label.group(0)), "none")
            cand.label_distance = cand.start - label.end()
        cand.position = cand.start / length
        cand.edge_position = cand.start < first_end or cand.start >= last_start
        cand.has_time = _has_time(page_text, cand.start, cand.end)
        cand.page_type = page_type
        cand.has_clinical_cue = clinical
        cand.year_delta = int(cand.iso[:4]) - received_year
        cand.context = re.sub(
            r"\s+",
            " ",
            page_text[
                max(0, cand.start - prof.window_left_chars) : cand.end + prof.window_right_chars
            ],
        ).strip()
        previous_end = cand.end
    _pair_ranges(candidates, prof)


def _pair_ranges(candidates: list[Candidate], prof: DosProfile) -> None:
    """Admit + discharge dates within range_pair_chars of each other."""
    admits = [c for c in candidates if c.label_class == "admit"]
    discharges = [c for c in candidates if c.label_class == "discharge"]
    for admit in admits:
        best: Optional[Candidate] = None
        best_gap = prof.range_pair_chars + 1
        for discharge in discharges:
            if discharge.iso < admit.iso:
                continue
            gap = max(discharge.start - admit.end, admit.start - discharge.end, 0)
            if gap < best_gap:
                best, best_gap = discharge, gap
        if best is None:
            continue
        for a, b in ((admit, best), (best, admit)):
            if a.pair is None:
                a.in_range_pair = True
                a.pair = b
                a.pair_iso = b.iso


def chart_features(candidates: list[Candidate], prof: DosProfile) -> None:
    """cluster_size: other candidates in the chart within cluster_days."""
    ordinals = sorted(date.fromisoformat(c.iso).toordinal() for c in candidates)
    for cand in candidates:
        day = date.fromisoformat(cand.iso).toordinal()
        lo = bisect.bisect_left(ordinals, day - prof.cluster_days)
        hi = bisect.bisect_right(ordinals, day + prof.cluster_days)
        cand.cluster_size = hi - lo - 1


# --- Stage C: score ----------------------------------------------------------


def score_candidate(cand: Candidate, prof: DosProfile) -> float:
    score = prof.base + prof.label_weights[cand.label_class]
    if cand.label_distance is not None:
        score -= prof.label_distance_decay * cand.label_distance
    if cand.has_time:
        # Print stamps live at the edges; the edge is no evidence for them.
        score -= prof.timestamp_penalty
    elif cand.edge_position:
        score += prof.edge_bonus
    if cand.has_clinical_cue:
        score += prof.clinical_cue_bonus
    score += prof.cluster_bonus * min(cand.cluster_size, prof.cluster_cap)
    if cand.year_delta is not None and cand.year_delta < -prof.max_age_years:
        score -= prof.age_penalty
    cand.score = min(1.0, max(0.0, score))
    return cand.score


@dataclass
class PageDate:
    dos_from: str  # ISO
    dos_to: str
    confidence: float
    keyword: str
    source: str  # "rules" | "llm" | "kv"
    is_pair: bool = False


def _date_source(cand: Candidate) -> str:
    if cand.origin == "kv" or (cand.pair is not None and cand.pair.origin == "kv"):
        return "kv"
    return "rules"


def best_page_date(candidates: list[Candidate], prof: DosProfile) -> Optional[PageDate]:
    """Key/value date first. Otherwise, as the backup, the highest text
    candidate at or above ``DOS_MIN_SCORE`` (dos_canon.json, 0.75).

    A range pair emits both ends.
    """
    kv = [c for c in candidates if c.origin == "kv"]
    passing = kv or [c for c in candidates if c.score >= prof.min_score]
    if not passing:
        return None
    best = max(passing, key=lambda c: c.score)  # ties: first on the page
    best.chosen = True
    if best.in_range_pair and best.pair is not None:
        best.pair.chosen = True
        admit, discharge = (
            (best, best.pair) if best.label_class == "admit" else (best.pair, best)
        )
        return PageDate(
            dos_from=admit.iso,
            dos_to=discharge.iso,
            confidence=best.score,
            keyword=f"{admit.label_text}+{discharge.label_text}",
            source=_date_source(best),
            is_pair=True,
        )
    return PageDate(
        dos_from=best.iso,
        dos_to=best.iso,
        confidence=best.score,
        keyword=best.label_text,
        source=_date_source(best),
    )


# --- date formats ------------------------------------------------------------


def iso_to_mdy(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{m}-{d}-{y}"


def normalize_date(date_str: str, reference_date: Optional[str] = None) -> str:
    """Any supported shape → MM-DD-YYYY, or "unknown". Used for LLM replies."""
    match = DATE_RE.search((date_str or "").strip())
    if not match:
        return "unknown"
    iso = parse_date_match(match)
    return iso_to_mdy(iso) if iso else "unknown"


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


def _page_type_name(page_text: str, page_number: Any) -> str:
    """The page's sub-type by the keyword model (page_keyword_canon.json).

    The fallback for a page the page-type stage has not classified (it runs
    first in the chain). A generic sub-type carries its page type's name, so
    the profile's lists can name either."""
    match = page_keywords.classify(page_text)
    return match.page_subtype if match is not None else ""


def extract_dos_from_page_text(
    page_text: str,
    *,
    received_date: Optional[date] = None,
    page_type: Optional[str] = None,
) -> Optional[dict]:
    """Stages A–C on one page, without chart context (cluster_size is 0)."""
    prof = profile()
    candidates = find_candidates(page_text)
    if not candidates:
        return None
    received = received_date or date.today()
    page_features(
        page_text,
        candidates,
        page_type=page_type if page_type is not None else _page_type_name(page_text, 1),
        received_year=received.year,
        prof=prof,
    )
    for cand in candidates:
        score_candidate(cand, prof)
    best = best_page_date(candidates, prof)
    if best is None:
        return None
    return {
        "dos_from": iso_to_mdy(best.dos_from),
        "dos_to": iso_to_mdy(best.dos_to),
        "keyword": best.keyword,
        "confidence": round(best.confidence, 4),
        "match_type": "admit_discharge_pair" if best.is_pair else "page_date",
    }


# --- Stage D: resolve the chart ---------------------------------------------


def page_allows_llm(page_text: str) -> bool:
    """True when page looks like a clinical note section LLM may help with."""
    return bool(profile().clinical_cues.search(page_text or ""))


def is_discharge_like(page_text: str) -> bool:
    return bool(profile().discharge_cues.search(page_text or ""))


def _row(
    page: dict,
    page_date: Optional[PageDate],
    doc: Optional[PageDate],
    *,
    match_type: str,
    prof: DosProfile,
) -> dict:
    """One output row. MM-DD-YYYY columns plus their ISO twins."""
    is_default = doc is None
    doc_from = doc.dos_from if doc else prof.default_date
    doc_to = doc.dos_to if doc else prof.default_date
    if page_date is not None:
        confidence = page_date.confidence
    elif doc is not None:
        confidence = doc.confidence
    else:
        confidence = 0.0
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
        "final_dos": _final_date(page_date, doc, match_type),
        "keyword": page_date.keyword if page_date else None,
        "confidence": round(confidence, 4),
        "page_source": page_date.source if page_date else "",
        "is_default": is_default,
    }


def _final_date(
    page_date: Optional[PageDate],
    doc: Optional[PageDate],
    match_type: str,
) -> str:
    """The page's own date. The document's date, carried across its pages, is
    the continuity stage's Final DOS."""
    return page_date.dos_from if page_date else ""


_KV_LABEL = {"admit": "admit", "discharge": "discharge"}


def add_kv_dates(
    page_text: str,
    candidates: list[Candidate],
    dates: list[dict[str, Any]],
    *,
    page_index: int,
    page_number: Any,
    page_name: str,
) -> None:
    """Mark a key/value date as the page's date, or add it when the text missed it.

    The same day already found in the text keeps its span and takes the
    key/value label. A new one is inserted with the extractor's label
    (admit, discharge, or encounter) so it is scored by the same weights.
    """
    seen = {cand.iso: cand for cand in candidates}
    for item in dates:
        iso = str(item.get("iso") or "")
        if len(iso) != 10:
            continue
        existing = seen.get(iso)
        if existing is not None:
            # The same day already came from the text. Keep that span, but the
            # key/value hit is the one the page uses.
            existing.origin = "kv"
            existing.label_text = str(item.get("keyword") or existing.label_text)
            existing.label_class = _KV_LABEL.get(str(item.get("tier") or ""), "encounter")
            continue
        raw = str(item.get("raw") or "")
        start = page_text.find(raw) if raw else -1
        if start < 0:
            start, end = 0, 0
        else:
            end = start + len(raw)
        tier = str(item.get("tier") or "")
        candidates.append(
            Candidate(
                page_index=page_index,
                page_number=page_number,
                page_name=page_name,
                raw=raw or iso,
                iso=iso,
                start=start,
                end=end,
                label_text=str(item.get("keyword") or ""),
                label_class=_KV_LABEL.get(tier, "encounter"),
                origin="kv",
            )
        )
        seen[iso] = candidates[-1]


def detect_dos_per_page(
    text: str,
    client: Any | None,
    *,
    use_llm: bool = True,
    received_date: Optional[date] = None,
    candidate_log: Optional[list[dict]] = None,
    kv_dates: Optional[dict[str, list[dict[str, Any]]]] = None,
    page_types: Optional[dict[str, str]] = None,
) -> list[dict]:
    """One row per page.

    Page level (``dos_from`` / ``dos_to``): the date found on that page, or
    blank. Document level (``doc_dos_*``): the encounter the page belongs to.
    ``final_dos`` is the page's own date; the continuity stage carries a
    document's date across its pages.

    ``received_date`` is the chart's received date; DOS_MAX_AGE_YEARS counts
    back from it (today when not given). ``candidate_log``, when passed, gets
    every candidate with its features, score and whether it was chosen.

    ``page_types`` maps a page name to its sub-type from the page-type stage
    (the Extracted answer), which runs first. A page missing from it — or every
    page, when the stage has not run — falls back to the keyword model.

    ``kv_dates`` maps a page name to dates the key/value extractor chose
    (``iso``, ``raw``, ``tier``, ``keyword``). They join the text sweep and
    are scored with the same weights.
    """
    prof = profile()
    received = received_date or date.today()

    pages: list[dict] = []
    for page in split_ocr_into_pages(text):
        page_text = text[page["start"] : page["end"]]
        cleaned = re.sub(
            r"-----\s*Page\s*\d+[^\n]*-----\s*", "", page_text, flags=re.IGNORECASE
        )
        cleaned = re.sub(r"[#\-*_=]", "", cleaned)
        if re.sub(r"\s+", " ", cleaned).strip().upper() == "UNACCEPT":
            break
        page_name = page.get("page_name") or ""
        page_type = (page_types or {}).get(page_name) or (
            _page_type_name(page_text, page.get("page")) if page_text.strip() else ""
        )
        candidates = find_candidates(
            page_text,
            page_index=page["index"],
            page_number=page.get("page"),
            page_name=page_name,
        )
        add_kv_dates(
            page_text,
            candidates,
            (kv_dates or {}).get(page_name) or [],
            page_index=page["index"],
            page_number=page.get("page"),
            page_name=page_name,
        )
        page_features(
            page_text,
            candidates,
            page_type=page_type,
            received_year=received.year,
            prof=prof,
        )
        pages.append(
            {**page, "text": page_text, "page_type": page_type, "candidates": candidates}
        )

    everything = [c for p in pages for c in p["candidates"]]
    chart_features(everything, prof)
    for cand in everything:
        score_candidate(cand, prof)

    rows: list[dict] = []
    current: Optional[PageDate] = None

    for page in pages:
        page_text = page["text"]
        page_type = page["page_type"].casefold()
        found = best_page_date(page["candidates"], prof)

        if (
            found is None
            and not _uses_default_date(page_type, prof)
            and use_llm
            and client is not None
            and page_allows_llm(page_text)
        ):
            pair = extract_dos_range_with_llm(
                page_text,
                str(page.get("page_name") or page.get("page")),
                client,
                reference_date=iso_to_mdy(current.dos_from) if current else None,
                discharge_like=is_discharge_like(page_text),
            )
            if pair:
                d_from, d_to = (to_iso_date(v) for v in pair)
                if d_from:
                    found = PageDate(
                        dos_from=d_from,
                        dos_to=d_to or d_from,
                        confidence=prof.llm_confidence,
                        keyword="",
                        source="llm",
                    )

        assigned = found
        if _uses_default_date(page_type, prof):
            # Demographics and injection pages do not take a carried date.
            assigned, doc, match_type = None, None, "default_page"
        elif found is None:
            assigned, doc, match_type = None, None, "no_date_found"
        elif page_type in prof.non_encounter_page_types:
            # A facesheet or med list never replaces an encounter already found.
            assigned = found
            doc = current or found
            match_type = "non_encounter_page"
        else:
            current = found
            assigned, doc = found, found
            match_type = "admit_discharge_pair" if found.is_pair else "page_date"

        if assigned is not None and assigned.source == "llm" and doc is assigned:
            match_type = "llm"

        rows.append(_row(page, assigned, doc, match_type=match_type, prof=prof))

    if candidate_log is not None:
        candidate_log.extend(c.log_row() for c in everything)
    return rows


# --- LLM fallback ------------------------------------------------------------


def _slice_first_n_words(text: str, n: int) -> str:
    count = 0
    for m in re.finditer(r"\b\w+\b", text):
        count += 1
        if count >= n:
            return text[: m.end()]
    return text


def _slice_last_n_words(text: str, n: int) -> str:
    matches = list(re.finditer(r"\b\w+\b", text))
    if len(matches) <= n:
        return text
    return text[matches[-n].start() :]


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

    words = profile().edge_words
    top = _slice_first_n_words(page_text, words)
    bottom = _slice_last_n_words(page_text, words)
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
        from azure_retry import call_with_retry
        from config import (
            AZURE_RETRY_ATTEMPTS,
            AZURE_RETRY_BASE_DELAY,
            AZURE_RETRY_MAX_DELAY,
        )

        response = call_with_retry(
            lambda: client.chat.completions.create(
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
            ),
            attempts=AZURE_RETRY_ATTEMPTS,
            base_delay=AZURE_RETRY_BASE_DELAY,
            max_delay=AZURE_RETRY_MAX_DELAY,
            label="azure.openai.dos",
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
