"""Date of service: the key/value date, else a labelled date at the top or bottom of the page.

Per page, two states:

  1. Key/value — among the DOS dates the extractor accepted (never a print key's), the
     first tier group in the order for the page's kind, which is its page classification
     L1. Discharge (Discharge Summary): the Admit (From) / Discharge (To) pair, then Date of
     Service. Lab (Lab Report): Collected, then Received, then Date of Service.
     Other: Date of Service. Then weak keys, dt / dd last among them, then keyless
     header / footer dates. Lab pages rank Report Date after Received; lab and other
     pages end with the Admit / Discharge pair (a misclassified discharge page). A tier not listed
     for the kind is not a candidate.
     Within a group the model's highest score wins. Its confidence
     (modules/confidence.py): the model's score lifted by the rules' score when both
     read the date, the rules' alone times no_model, times conflict when another date of
     the same group scores within 80% of it (``kv_confidence``).
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

import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Optional

from modules import confidence as conf
from modules.canon_store import CANON_DIR, CanonFile
from modules.extraction.util.dates import is_iso_date

logger = logging.getLogger(__name__)


# --- editable config ---------------------------------------------------------


LABEL_CLASSES = ("encounter", "admit", "discharge")


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
    # Page classification L1 (casefolded) -> page kind (discharge / lab); else other.
    page_kinds: dict[str, str]
    # Page kind (discharge / lab / other) -> key groups in priority order.
    kv_priority: dict[str, list[list[str]]]
    # Weak keys ranked after every other weak key (group weak_last).
    weak_last_keys: frozenset[str]
    # A weak key's date is a candidate only when scored above this; no other tier has a floor.
    weak_min_score: float
    # 2026-11-01 (user): exam-date keys (group "exam"), the amended words and their reach,
    # Report Date keys a discharge page takes as its discharge date, the page kinds that get
    # the keyword pass, the signature fallback and the neighbour consistency.
    exam_keys: frozenset[str] = frozenset()
    amended_re: Optional[re.Pattern[str]] = None
    amended_reach_chars: int = 40
    report_as_discharge_keys: frozenset[str] = frozenset()
    keyword_pass_kinds: frozenset[str] = frozenset()
    signature_fallback_below: float = 0.0
    neighbours: dict[str, float] = field(default_factory=dict)


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
        page_kinds={l1.casefold(): kind for kind, l1s in (data.get("page_kinds") or {}).items() for l1 in l1s},
        kv_priority={kind: [list(group) for group in groups] for kind, groups in (data.get("kv_priority") or {}).items()},
        weak_last_keys=frozenset(k.casefold() for k in data.get("weak_last_keys") or []),
        weak_min_score=float(data.get("weak_min_score", 0.0)),
        exam_keys=frozenset(k.casefold() for k in data.get("exam_keys") or []),
        amended_re=re.compile(r"(?<![a-z])(?:" + "|".join(re.escape(w) for w in data["amended_words"]) + r")(?![a-z])",
                              re.IGNORECASE) if data.get("amended_words") else None,
        amended_reach_chars=int(data.get("amended_reach_chars", 40)),
        report_as_discharge_keys=frozenset(k.casefold() for k in data.get("report_as_discharge_keys") or []),
        keyword_pass_kinds=frozenset(data.get("keyword_pass_kinds") or []),
        signature_fallback_below=float(data.get("signature_fallback_below", 0.0)),
        neighbours={k: float(v) for k, v in (data.get("neighbours") or {}).items()},
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


def edge_dates(page_text: str, prof: Optional[DosProfile] = None, *, whole_page: bool = False) -> list[LabelledDate]:
    """Every DOS label starting inside an edge span, with the date after it.

    The date must start within ``label_reach_chars`` of the label, with only
    ``label_gap_words`` between ("Encounter Date | at January 28, 2025",
    "Visit Date/Time: ..."), so a label never takes a date that belongs to the
    next field ("Visit Date: | DOB: 01/02/1960")."""
    prof = prof or profile()
    found: list[LabelledDate] = []
    seen: set[int] = set()
    edges = edge_spans(page_text, prof.edge_words)
    # 2026-11-01 (user): beyond the edges only a strong label counts: two words or more,
    # with ':' or '-' between it and its date ("Date of Service: 04/02/2024").
    spans = edges + ([("page", 0, len(page_text))] if whole_page else [])
    for edge, lo, hi in spans:
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
            if edge == "page" and not _strong_mid_label(label.group(0), gap):
                continue
            if _DICTATION_BEFORE.search(page_text, 0, label.end() + match.start()):
                # 'D: 03/01/2024 T: 03/04/2024': dictated / transcribed, not a visit date.
                continue
            iso = parse_date_match(match)
            if iso is None:
                continue
            if amended_before(page_text, label.end() + match.start(), prof):
                continue  # 'Amended 07/23/2025': when the note was amended
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


def _strong_mid_label(label: str, gap: str) -> bool:
    """A label read beyond the page's edges: at least two words, then ':' or '-' and the date."""
    return len(label.split()) >= 2 and bool(re.match(r"^\s*[:\-–]\s*$", gap))


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
    source: str  # kv | edge | edge_tesseract | admit_discharge | dictation | keyword_pass | signature | neighbours
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
        # A labelled date the key/value extractor did not read: the label's strength
        # (canon confidence.edge), with no model behind it.
        return conf.score(prof.confidence["edge"], "no_model", module="dos")

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


def page_kind(family: str, prof: DosProfile) -> str:
    """The page's kind from its classification L1: discharge, lab, else other."""
    return prof.page_kinds.get((family or "").strip().casefold(), "other")


def _group(item: dict[str, Any], prof: DosProfile, rank: dict[str, int]) -> str:
    """A candidate's priority group: the tier the extractor gave it. A weak key is weak_last
    when it is in ``weak_last_keys`` (dt, dd), and report when it is Report Date and the
    page kind ranks report on its own (lab); otherwise weak."""
    tier = str(item.get("tier") or "")
    key = str(item.get("keyword") or "").strip().casefold()
    if tier == "service" and key in prof.exam_keys and "exam" in rank:
        return "exam"
    if tier != "weak":
        return tier
    key = str(item.get("keyword") or "").strip().casefold()
    if key in prof.weak_last_keys:
        return "weak_last"
    if key == "report date" and "report" in rank:
        return "report"
    return tier


# A print stamp: 'Printed: 10/30/2025', 'Printed On 10/30/20258:10 AM' (date glued to the
# time), 'Print Date - Oct 30, 2025', 'Printed by J Doe on 10/30/2025'. The date is read
# within PRINT_REACH characters after the word, on its line.
_PRINT_WORD = re.compile(r"(?i)(?<![a-z])print(?:ed|s)?(?![a-z])")
_GLUED_DATE = re.compile(r"(?<!\d)(\d{1,2})[/-](\d{1,2})[/-](\d{4}|\d{2}(?!\d))")
PRINT_REACH = 50


def print_dates(*texts: Optional[str]) -> set[str]:
    """Every date written in a print stamp in these texts (ISO). Never a DOS anywhere."""
    found: set[str] = set()
    for text in texts:
        for word in _PRINT_WORD.finditer(text or ""):
            window = text[word.end() : word.end() + PRINT_REACH].split("\n", 1)[0]
            match = DATE_RE.search(window)
            iso = parse_date_match(match) if match else None
            if iso is None and (glued := _GLUED_DATE.search(window)):
                month, day, year = glued.groups()
                try:
                    iso = date(_two_digit_year(year), int(month), int(day)).isoformat()
                except ValueError:
                    iso = None
            if iso:
                found.add(iso)
    return found


def amended_before(text: str, at: int, prof: Optional[DosProfile] = None) -> bool:
    """An amended word (amended, addendum ...) within ``amended_reach_chars`` before
    offset ``at``, on the same line: the date there is when the note was amended."""
    prof = prof or profile()
    if prof.amended_re is None or at < 0:
        return False
    window = text[max(0, at - prof.amended_reach_chars):at].rsplit("\n", 1)[-1]
    return bool(prof.amended_re.search(window))


def amended_item(item: dict[str, Any], prof: Optional[DosProfile] = None) -> bool:
    """A key/value date whose sentence puts an amended word right before it."""
    sentence, raw = str(item.get("sentence") or ""), str(item.get("raw") or "")
    if not sentence or not raw:
        return False
    at = sentence.find(raw)
    return at >= 0 and amended_before(sentence, at, prof)


def kv_dated(candidates: list[dict[str, Any]], printed: frozenset[str] | set[str] = frozenset()) -> list[dict[str, Any]]:
    """The candidates with a date that may be a DOS.

    Never a print date: not a print key's row (tier printed; staging written before the
    extractor rejected print keys still has them accepted), and no row of any tier whose
    date is one of the page's ``printed`` dates or a print key's date."""
    printed = set(printed) | {str(item.get("iso")) for item in candidates or [] if item.get("tier") == "printed"}
    return [
        item for item in candidates or []
        if is_iso_date(item.get("iso"))
        and item.get("tier") != "printed"
        and not amended_item(item)
        and str(item.get("iso")) not in printed
        and str(item.get("iso_to") or "") not in printed
    ]


def kv_page_date(
    candidates: list[dict[str, Any]], kind: str = "other", prof: Optional[DosProfile] = None,
    printed: frozenset[str] | set[str] = frozenset(), *, relaxed: bool = False,
) -> Optional[PageDate]:
    """State 1: among the key/value dates (``kv_dated``), the first priority group of the
    page's kind that has one (``kv_priority``), and within it the date scored highest (then
    page order). No score floor, except that a weak key's date (weak, weak_last) must score
    above ``weak_min_score``. The score is the model's, else the rules'. A range keeps its
    end. Its confidence is that score (the canon's kv when there is none)."""
    prof = prof or profile()
    groups = prof.kv_priority.get(kind) or prof.kv_priority.get("other") or []
    rank = {name: i for i, group in enumerate(groups) for name in group}

    def score(item: dict[str, Any]) -> float:
        value = item.get("confidence")
        return float(value) if value is not None else prof.confidence["kv"]

    # Only the tiers listed for the page's kind; a weak key's date needs a score above the floor.
    dated = [
        it for it in kv_dated(candidates, printed)
        if _group(it, prof, rank) in rank
        and (relaxed or it.get("tier") != "weak" or score(it) > prof.weak_min_score)
    ]
    if not dated:
        return None
    item = min(dated, key=lambda it: (rank[_group(it, prof, rank)], -score(it)))
    if item.get("tier") in ("admit", "discharge"):
        # The admit / discharge backup (a misclassified discharge page) keeps the pair.
        return admit_discharge_date(_kv_stay(dated), [], prof)
    iso, iso_to = str(item["iso"]), str(item.get("iso_to") or "")
    is_pair = is_iso_date(iso_to) and iso_to > iso
    group = _group(item, prof, rank)
    rivals = [it for it in dated if _group(it, prof, rank) == group and str(it["iso"]) != iso
              and score(it) >= 0.8 * score(item)]
    return PageDate(iso, iso_to if is_pair else iso, kv_confidence(item, prof, conflict=bool(rivals)),
                    str(item.get("keyword") or ""), "kv", is_pair=is_pair)


def kv_confidence(item: dict[str, Any], prof: Optional[DosProfile] = None, *, conflict: bool = False) -> float:
    """A key/value date's confidence (modules/confidence.py): the model's score,
    lifted by the rules' score when the rules read the date too; the rules' score alone
    (the canon's kv without one) times ``no_model``; times ``conflict`` when another
    date of its priority group scores close to it."""
    prof = prof or profile()
    value, rule = item.get("confidence"), item.get("rule_score")
    reasons = ["conflict" if conflict else ""]
    if value is None:
        return conf.score(prof.confidence["kv"], "no_model", *reasons, module="dos")
    if item.get("model_scored", True):  # a score with no flag is the model's (older staging)
        base = conf.backed(float(value), float(rule)) if rule is not None else float(value)
        return conf.score(base, *reasons, module="dos")
    return conf.score(float(value), "no_model", *reasons, module="dos")


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
            if iso and not amended_before(page_text, found.end(), prof):
                # A dictation date is often not the visit's: a fallback, flagged.
                return PageDate(iso, iso, conf.score(prof.confidence["dictation"], "no_model", "fallback", module="dos"),
                                label, "dictation")
    return None


def admit_discharge_date(
    kv_stay: dict[str, dict[str, Any]],
    hits: list[LabelledDate],
    prof: Optional[DosProfile] = None,
    reports: Optional[list[dict[str, Any]]] = None,
) -> Optional[PageDate]:
    """Discharge pages: the stay's Admit (From) and Discharge (To) dates, ahead of every other.

    ``kv_stay`` holds the key/value extractor's best ``admit`` / ``discharge`` date for the
    page; edge labels fill a side it lacks. A discharge date before the admit date is
    ignored; one date alone is both From and To."""
    prof = prof or profile()
    edge_side = {cls: next((h for h in hits if h.label_class == cls), None) for cls in ("admit", "discharge")}

    def side(cls: str) -> tuple[str, float | None, str] | None:
        item = kv_stay.get(cls)
        if item and len(str(item.get("iso") or "")) == 10:
            return str(item["iso"]), kv_confidence(item, prof), str(item.get("keyword") or cls)
        hit = edge_side[cls]
        return (hit.iso, None, hit.label) if hit else None

    admit, discharge = side("admit"), side("discharge")
    if admit and discharge and discharge[0] < admit[0]:
        discharge = None
    if admit and discharge is None and reports:
        # 2026-11-01 (user): no discharge date: the Report Date on or after the admit date.
        later = [it for it in reports if str(it.get("iso") or "") >= admit[0]]
        if later:
            best = max(later, key=lambda it: float(it.get("confidence") or 0))
            discharge = (str(best["iso"]), conf.score(kv_confidence(best, prof), "fallback", module="dos"),
                         str(best.get("keyword") or "report date"))
    used = [s for s in (admit, discharge) if s]
    if not used:
        return None
    start = (admit or discharge)[0]
    end = (discharge or admit)[0]
    # Both ends must be right: the weaker side's; an edge-label side is the label's strength.
    kv_scores = [s[1] for s in used if s[1] is not None]
    edge = conf.score(prof.confidence["edge"], "no_model", module="dos")
    confidence = min(kv_scores + ([edge] if len(kv_scores) < len(used) else [])) if kv_scores else edge
    return PageDate(start, end, confidence, "+".join(s[2] for s in used), "admit_discharge",
                    is_pair=end != start, kv_backed=bool(kv_scores))


def keyword_pass_date(
    page_text: str, candidates: list[dict[str, Any]], kind: str, prof: Optional[DosProfile] = None,
    printed: frozenset[str] | set[str] = frozenset(),
) -> Optional[PageDate]:
    """2026-11-01 (user): a discharge or Progress Note page with no date yet: its weak
    key/value dates under ``weak_min_score`` (the floor relaxed). (A DOS label anywhere on
    such a page is already read by the label step, which reads the whole page for these
    kinds.) Confidence: the canon's keyword_pass, no model."""
    prof = prof or profile()
    if kind not in prof.keyword_pass_kinds:
        return None
    # 2026-11-01 (user): a bare 'Date:' counts only in the page's first / last edge_words
    # words: the weak dates kept are those whose date is written there.
    edge_text = " ".join(page_text[lo:hi] for _edge, lo, hi in edge_spans(page_text, prof.edge_words))
    near_edge = [
        it for it in candidates
        if it.get("tier") != "weak" or (str(it.get("raw") or "") and str(it.get("raw")) in edge_text)
    ]
    found = kv_page_date(near_edge, kind, prof, printed, relaxed=True)
    if found is None:
        return None
    found.confidence = conf.score(prof.confidence.get("keyword_pass", 0.7), "no_model", module="dos")
    found.source = "keyword_pass"
    return found


def signature_page_date(signature_dates: list[str], prof: Optional[DosProfile] = None,
                        printed: frozenset[str] | set[str] = frozenset()) -> Optional[PageDate]:
    """2026-11-01 (user): the last electronic signature's date (the main doctor's) on the page."""
    prof = prof or profile()
    dated = [iso for iso in signature_dates if is_iso_date(iso) and iso not in printed]
    if not dated:
        return None
    return PageDate(dated[-1], dated[-1], conf.score(prof.confidence.get("signature", 0.6), "no_model", module="dos"),
                    "electronic signature", "signature")


# DOS sources that are fallbacks: checked against the neighbouring pages' dates.
FALLBACK_SOURCES = frozenset({"keyword_pass", "signature", "dictation"})


def consistent_with_neighbours(found: list[Optional[PageDate]], kinds: list[str],
                               prof: Optional[DosProfile] = None) -> list[Optional[PageDate]]:
    """2026-11-01 (user): dates do not vary within a run of pages. For a page (not lab) with
    no DOS, a DOS under ``neighbours.below``, or a fallback DOS (keyword pass, signature,
    dictation), against the sure dated pages (each at least ``below``) within ``max_gap``
    pages before and after it:

    * both sides agree on one date -> the page takes it (confidence = the weaker
      neighbour's x ``factor``);
    * otherwise a date the page has that is outside the neighbours' dates (from the
      earliest to the latest) is dropped: the page has no DOS rather than a stray one.
    A page with no sure neighbour keeps what it has."""
    prof = prof or profile()
    cfg = prof.neighbours
    if not cfg:
        return found
    gap, below, factor = int(cfg.get("max_gap", 2)), float(cfg.get("below", 0.5)), float(cfg.get("factor", 0.9))

    def sure(i: int) -> bool:
        return found[i] is not None and found[i].confidence >= below

    out = list(found)
    for i, page in enumerate(found):
        weak = page is None or page.confidence < below or page.source in FALLBACK_SOURCES
        if kinds[i] == "lab" or not weak:
            continue
        before = next((j for j in range(i - 1, max(-1, i - gap - 1), -1) if sure(j)), None)
        after = next((j for j in range(i + 1, min(len(found), i + gap + 1)) if sure(j)), None)
        sides = [found[j] for j in (before, after) if j is not None]
        if not sides:
            continue
        if len(sides) == 2 and (sides[0].dos_from, sides[0].dos_to) == (sides[1].dos_from, sides[1].dos_to):
            a = sides[0]
            out[i] = PageDate(a.dos_from, a.dos_to, round(min(s.confidence for s in sides) * factor, 4),
                              f"pages {before + 1} and {after + 1}", "neighbours", is_pair=a.is_pair)
            continue
        if page is not None:
            lo = min(s.dos_from for s in sides)
            hi = max(s.dos_to or s.dos_from for s in sides)
            if page.dos_from < lo or (page.dos_to or page.dos_from) > hi:
                out[i] = None  # a stray date outside the neighbours': no DOS
    return out


# --- date formats ------------------------------------------------------------


def iso_to_mdy(iso: str) -> str:
    """YYYY-MM-DD as MM-DD-YYYY; '' for anything that is not a real ISO date."""
    if not is_iso_date(iso):
        return ""
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
    "keyword_pass": "keyword_pass", "signature": "signature_date", "neighbours": "neighbours",
}


def _row(page: dict, page_date: Optional[PageDate], prof: DosProfile) -> dict:
    """One output row. MM-DD-YYYY columns plus their ISO twins.

    The document-level columns are the page's own date (DOS_DEFAULT_DATE when
    it has none); nothing is carried from another page here."""
    if page_date is not None and not (is_iso_date(page_date.dos_from) and is_iso_date(page_date.dos_to)):
        # Never fail a chart on one unreadable date: the page is treated as undated.
        logger.warning("DOS: page %s dropped an unreadable date %r..%r (%s)", page.get("page_name"),
                       page_date.dos_from, page_date.dos_to, page_date.source)
        page_date = None
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
        "confidence": round(page_date.confidence, 4) if page_date else None,
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
    page_families: Optional[dict[str, str]] = None,
    print_texts: Optional[dict[str, str]] = None,
    signature_dates: Optional[dict[str, list[str]]] = None,
) -> list[dict]:
    """One row per page. On a discharge page, its Admit / Discharge pair first
    (``admit_discharge_date``). Then the key/value date chosen by the page kind's priority,
    else the edge keyword (final text, then Tesseract), else — on an "other" page — the
    dictated date, else the transcribed one, else nothing.

    ``kv_dates`` maps a page name to every key/value date the rules accepted (``iso``,
    optional ``iso_to``, ``tier``, ``keyword``, ``confidence`` = the model's score).
    ``tesseract_texts`` maps a
    page name to its Tesseract text; only its first and last ``edge_words`` words are read.
    ``candidate_log``, when passed, gets every edge label with its date and whether it was
    chosen. ``page_families`` maps a page name to its classification L1 (the page kind);
    a page without one is "other". ``print_texts`` maps a page name to more text of the
    page (its Tesseract text, the print keys' sentences) read only for print stamps.

    A print date (``print_dates``: the final text, the Tesseract text, ``print_texts`` and
    the print keys' dates) is never a DOS: it is dropped from the key/value dates, the edge
    labels, the dictated date and a range's end.
    """
    prof = profile()
    pages: list[dict] = []
    founds: list[Optional[PageDate]] = []
    kinds: list[str] = []
    for page in split_ocr_into_pages(text):
        page_text = text[page["start"] : page["end"]]
        cleaned = re.sub(
            r"-----\s*Page\s*\d+[^\n]*-----\s*", "", page_text, flags=re.IGNORECASE
        )
        cleaned = re.sub(r"[#\-*_=]", "", cleaned)
        if re.sub(r"\s+", " ", cleaned).strip().upper() == "UNACCEPT":
            break
        page_name = page.get("page_name") or ""

        # State 1: the key/value date, by the page kind's priority; a discharge page's stay
        # first. Edge labels fill a side of the stay the key/value dates lack.
        candidates = (kv_dates or {}).get(page_name) or []
        tesseract_text = (tesseract_texts or {}).get(page_name) or ""
        printed = print_dates(page_text, tesseract_text, (print_texts or {}).get(page_name))
        printed |= {str(item.get("iso")) for item in candidates if item.get("tier") == "printed"}
        kind = page_kind((page_families or {}).get(page_name, ""), prof)
        # 2026-11-01 (user): on a discharge or Progress Note page a DOS label anywhere on the
        # page counts; elsewhere only in its first / last edge_words words.
        whole = kind in prof.keyword_pass_kinds
        hits = [hit for hit in edge_dates(page_text, prof, whole_page=whole) if hit.iso not in printed]
        found = None
        if kind == "discharge":
            dated = kv_dated(candidates, printed)
            reports = [it for it in dated if str(it.get("keyword") or "").strip().casefold()
                       in prof.report_as_discharge_keys]
            found = admit_discharge_date(_kv_stay(dated), hits, prof, reports)
        if found is None:
            found = kv_page_date(candidates, kind, prof, printed)
        # State 2: no key/value date; the edge keyword.
        if found is None:
            found = edge_page_date(page_text, prof, hits)
        if found is None and tesseract_text:
            tesseract_hits = [hit for hit in edge_dates(tesseract_text, prof) if hit.iso not in printed]
            found = edge_page_date(tesseract_text, prof, tesseract_hits)
            if found is not None:
                found.source = "edge_tesseract"
                for hit in tesseract_hits:
                    hit.edge = f"tesseract_{hit.edge}"
                hits = hits + tesseract_hits
        # 2026-11-01 (user): discharge / Progress Note pages: a keyword pass over the page.
        if found is None:
            found = keyword_pass_date(page_text, candidates, kind, prof, printed)
        if found is None and kind in ("other", "note"):
            found = dictation_page_date(page_text, prof)
            if found is not None and found.dos_from in printed:
                found = None
        # 2026-11-01 (user): no DOS, or a weak one: the last electronic signature's date.
        if found is None or found.confidence < prof.signature_fallback_below:
            signed = signature_page_date((signature_dates or {}).get(page_name) or [], prof, printed)
            if signed is not None:
                found = signed
        if found is not None and found.dos_to in printed:
            # A written range never ends on a print date.
            found.dos_to, found.is_pair = found.dos_from, False

        if candidate_log is not None:
            candidate_log.extend({"page_name": page_name, **asdict(hit)} for hit in hits)
        pages.append(page)
        founds.append(found)
        kinds.append(kind)
    founds = consistent_with_neighbours(founds, kinds, prof)
    return [_row(page, found, prof) for page, found in zip(pages, founds)]
