"""Blank / junk decision: rules R1-R4, the short page R6, then DistilBERT.

Inputs per page (``PageInputs``): the OCR text, its word boxes when the pass has them,
DistilBERT's probabilities (model_bridge.model_scores), the page type and handwritten
share from the page-tag model, and Provider Signature from KV extraction.

No keyword scoring or voting (removed 2026-10-20): keywords survive only as the fixed
phrases R1 (cover term), R2 and R3 look for. R4 reads only kv_extract's Electronic
Signature (no signature phrases since 2026-10-21). The ladder, first rule that fires decides:

  R1 blank / tiny   no ink -> Blank. Not Visual: 0 characters, or the word "blank" in
                    < 30 characters -> Blank; else < 15 characters -> Junk / Cover Page
                    when a Cover Page term is there, Junk / Others otherwise
  R1 cover mark     Accept / Unaccept / Cover on a page of < 10 words -> Junk / Cover Page
  R1E end of report "End of Report" on a page of < 10 words -> Junk / Cover Page (2026-11-08)
  R2 placeholder    "intentionally left blank" on a page of <= 25 words -> Blank
  R3 card           pass 2, page-tag printed, visual or uncertain: an ID or insurance card by
                    card_rules.detect (card or no card; no form / letter words, no section
                    header but the card's own, <= 150 words) -> Junk / Cards, after the
                    handwritten and signed keeps (2026-11-05, user)
  R3V visual        a visual page under 20 words that is not a card (a picture: an
                    ECG strip, an X-ray print) -> Main, either pass (2026-11-01, user)
  pass 1 (Tesseract text) stops here:
    R5 keep         DistilBERT p(keep) > 0.95 -> Main, final: pass 2 does not judge it again
    R5 junk         DistilBERT p(junk) > 0.95 -> Junk (DistilBERT's subtype), final: pass 2
                    does not judge it again (2026-10-22). Not on a page more than 30%
                    handwritten. Like every page pass 1 settles, it gets Final OCR 1 (stored
                    only: no quality re-grade) and no Final OCR 2 or later stage
    otherwise       Main for now; Final OCR, then pass 2 decides
  pass 2 (Final OCR text):
    R4 signature    Electronic Signature found by kv_extract -> Main, unless DistilBERT's
                    p(junk) > 0.95 (a signed records-request letter is still junk), and not when
                    the same signature (region + sentence / signer) is on the previous / next non-blank page:
                    a repeated stamp (2026-11-15, user; replaced "a neighbour is signed too")
    INDEX contents  "Index" / "Contents" as a title and 3+ lines with page numbers ->
                    Junk / Others (2026-11-08, user)
    short page      content in the top 20%: stage._classify (a signature carried on from
                    the page before -> Main; only its own section header new -> Junk / Others;
                    < 5 new words when the previous page's last section header is Visit
                    Diagnosis, Encounter, Summary or a Progress Note header -> Main; else < 5 words not on the
                    previous / next non-blank page -> Junk / Others; 2026-11-08 / 2026-11-15, user)
    R5 DistilBERT   p(junk) >= 0.70 -> Junk (DistilBERT's subtype); below -> Main (2026-11-03,
                    user: no review band; canon junk_at = review_junk_at = 0.7)
    handwritten     a page more than 30% handwritten is never junked by R5: Main for review

Pass 2 also has, right after the signature rule (2026-11-08, user: part of the ladder, not
after it): LAB (Lab, Reference Range and Value on the page) -> Main and CLINICAL_CONTENT
(2+ ICD-10 codes or a Social History header) -> Main; the classifier's Invoice is Main (Invoice
page), except a terms page (T&C, Terms and Conditions, Payment Terms) -> Junk / Others. The full logic
for people: docs/specials/BLANK_JUNK_LOGIC.md.

So junk is decided in pass 1 by R1-R3 and DistilBERT's sure calls (p(junk) > 0.95); the rest
of its junk calls wait for the Final OCR.
Confidence (2026-11-14, user; docs/specials/CONFIDENCE_LOGIC.md section 12, ``verdict_confidence``):
the DistilBERT service's raw probability for the verdict's class (Main p(keep), Blank p(blank),
Junk p(junk); an Invoice kept Main p(junk)) when the model's top class agrees; ``confidence.flipped``
when a rule or the service's veto went against it; ``confidence.backup`` when no model ran. No ink /
stage-1 Blank / no text: 1.0 (measured); the other pass-1 text-pattern rules: ``confidence.pass1_rule``
(0.95). The older rule strengths (RULE_SUPPORT, rule_strength) are kept as ``Verdict.strength``, the
weight continuity reads for junk runs, so no decision changes. Thresholds live in ``resources/canon/blank_junk_context_canon.json``
(``distilbert``), which reloads on change.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from card_rules import detect as detect_card
from classify import (
    CODE_BLANK,
    CODE_CARDS,
    CODE_COVER_PAGE,
    CODE_INSTRUCTIONS,
    CODE_INVOICE,
    CODE_LETTER_FAX,
    CODE_MAIN,
    CODE_OTHERS,
    CODE_RECORD_REQUEST,
)
from apps.worker.stages._support import FINAL_JUNK, INVOICE_MARK, INVOICE_PAGE_SUBTYPE, INVOICE_PAGE_TYPE, is_invoice_page  # noqa: F401
from modules import confidence as conf
from modules.canon_store import CANON_DIR, CanonFile


# Subtype names in the canon and in the schema -> classify.py codes.
SUBTYPE_CODE = {
    "Invoice": CODE_INVOICE,
    "Cover Page": CODE_COVER_PAGE,
    "Record Request/Transmittal": CODE_RECORD_REQUEST,
    "Instructions": CODE_INSTRUCTIONS,
    "Letter/Fax": CODE_LETTER_FAX,
    "Others": CODE_OTHERS,
    "Cards": CODE_CARDS,
}

# Page-tag types the card rule R3 reads (2026-10-31, user; uncertain added 2026-11-15, user).
CARD_DOCUMENT_TYPES = frozenset({"printed", "visual", "uncertain"})
# DistilBERT labels -> subtype.
MODEL_SUBTYPE = {
    "JUNK_INVOICE": "Invoice",
    "JUNK_INSURANCE_ID": "Cards",       # 2026-11-07 (user): a card is never an invoice
    "JUNK_COVER_PAGE": "Cover Page",
    "JUNK_RECORD_REQUEST": "Record Request/Transmittal",
    "JUNK_COVER_REQUEST": "Record Request/Transmittal",
    "JUNK_INSTRUCTIONS": "Instructions",
    "JUNK_LETTER_FAX": "Letter/Fax",
    "JUNK_FAX_TRANSMISSION": "Letter/Fax",
}
# Junk evidence that is retrieval or billing paperwork, not something a face sheet carries.
RETRIEVAL_GROUPS = ("record_request", "roi_vendors")
# id_card (visual pages only): an insurance / ID card or driver's licence prints a name
# and DOB, which must not rescue it as a face sheet. These groups are also the R3 card check.
HARD_JUNK_GROUPS = ("record_request", "roi_vendors", "invoice", "id_card")

# How strongly each rule backs its verdict: the pre-2026-11-14 confidence, now only the
# ``Verdict.strength`` weight (rule_strength), never the stored confidence. R1-R2 judge a page
# with (almost) no text -- empty, a placeholder -- where DistilBERT has
# nothing to read: their confidence is this strength. R4 and R6 back DistilBERT's
# probability for their verdict; R5 is DistilBERT alone.
RULE_SUPPORT = {"R1": 1.0, "R2": 0.99, "R3": 0.95, "R4": 0.95, "R6": 0.95, "CONTENT": 0.95, "FAX": 0.85, "LAB": 0.95, "INVOICE": 0.95, "INDEX": 0.95, "R1E": 1.0}
NO_TEXT_RULES = ("R1", "R2")
# Starts the reason of a pass-1 keep that is final: pass 2 does not judge that page again.
FINAL_KEEP = "R5 keep (final)"
MIN_TEXT_CHARS = 15
BLANK_WORD_MAX_CHARS = 30
BLANK_WORD = re.compile(r"\bblank\b", re.IGNORECASE)
PLACEHOLDER_MAX_WORDS = 25


# --- canon -----------------------------------------------------------------------


@dataclass(frozen=True)
class Group:
    context: str
    name: str
    weight: float
    cap: float
    subtype: str
    zone: str
    # Counted only on pages of this page-tag type ("visual"); "" = every page.
    document_type: str = ""


@dataclass(frozen=True)
class Canon:
    raw: dict[str, Any]
    groups: dict[tuple[str, str], Group]
    # Per context: one alternation of every plain term (longest first), one of the
    # line-start terms, and the regex patterns. Matched text -> (group, term).
    term_re: dict[str, Optional[re.Pattern[str]]]
    line_re: dict[str, Optional[re.Pattern[str]]]
    # The line-start terms found anywhere in a heading line (a line of at most
    # heading_words[context] words): 'Allergies and Adverse Reactions' counts 'allergies'.
    heading_re: dict[str, Optional[re.Pattern[str]]]
    heading_words: dict[str, int]
    term_of: dict[str, dict[str, tuple[str, str]]]
    patterns: dict[str, list[tuple[str, re.Pattern[str]]]]
    thresholds: dict[str, float]
    ignore_lines: tuple[re.Pattern[str], ...]


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().casefold())


def _term_pattern(term: str) -> str:
    return r"\s+".join(re.escape(word) for word in term.split())


def _alternation(terms: list[str], *, line_start: bool) -> Optional[re.Pattern[str]]:
    if not terms:
        return None
    body = "|".join(_term_pattern(t) for t in sorted(set(terms), key=len, reverse=True))
    if line_start:
        return re.compile(rf"^[ \t]*({body})[ \t]*(?=[:\-]|$)", re.IGNORECASE | re.MULTILINE)
    return re.compile(rf"(?<![a-z0-9])({body})(?![a-z0-9])", re.IGNORECASE)


def _build(data: dict[str, Any]) -> Canon:
    groups: dict[tuple[str, str], Group] = {}
    term_of: dict[str, dict[str, tuple[str, str]]] = {}
    term_re: dict[str, Optional[re.Pattern[str]]] = {}
    line_re: dict[str, Optional[re.Pattern[str]]] = {}
    heading_re: dict[str, Optional[re.Pattern[str]]] = {}
    heading_words: dict[str, int] = {}
    patterns: dict[str, list[tuple[str, re.Pattern[str]]]] = {}
    for context, by_name in data["contexts"].items():
        plain: list[str] = []
        starts: list[str] = []
        term_of[context] = {}
        patterns[context] = []
        for name, spec in by_name.items():
            groups[(context, name)] = Group(
                context=context,
                name=name,
                weight=float(spec["weight"]),
                cap=float(spec.get("cap", spec["weight"])),
                subtype=str(spec.get("subtype") or "Others"),
                zone=str(spec.get("zone") or "any"),
                document_type=str(spec.get("document_type") or ""),
            )
            for term in spec.get("terms") or []:
                term_of[context][_norm(term)] = (name, term)
                (starts if spec.get("line_start") else plain).append(term)
            if spec.get("line_start"):
                heading_words[context] = max(
                    heading_words.get(context, 0), int(spec.get("heading_line_max_words") or 0)
                )
            for pattern in spec.get("patterns") or []:
                patterns[context].append((name, re.compile(pattern, re.IGNORECASE | re.MULTILINE)))
        term_re[context] = _alternation(plain, line_start=False)
        line_re[context] = _alternation(starts, line_start=True)
        heading_re[context] = _alternation(starts, line_start=False) if heading_words.get(context) else None
    return Canon(
        raw=data,
        groups=groups,
        term_re=term_re,
        line_re=line_re,
        heading_re=heading_re,
        heading_words=heading_words,
        term_of=term_of,
        patterns=patterns,
        thresholds={k: float(v) for k, v in data["thresholds"].items()},
        ignore_lines=tuple(
            re.compile(p, re.IGNORECASE) for p in data["short_page"].get("ignore_line_patterns") or []
        ),
    )


_CANON: CanonFile[Canon] = CanonFile(CANON_DIR / "blank_junk_context_canon.json", _build)


def canon() -> Canon:
    return _CANON.get()


# --- page geometry ------------------------------------------------------------------


@dataclass(frozen=True)
class Line:
    text: str
    top: float  # fraction of the page height, 0 = top
    bottom: float


def text_area(words: list[dict[str, Any]], width: Any, height: Any) -> Optional[float]:
    """The share of the page inside the box round every OCR word (0-1), None when unknown."""
    try:
        w, h = float(width or 0), float(height or 0)
    except (TypeError, ValueError):
        return None
    xs: list[float] = []
    ys: list[float] = []
    for word in words or []:
        polygon = word.get("polygon") or []
        if len(polygon) < 8 or not str(word.get("content") or "").strip():
            continue
        xs += [float(v) for v in polygon[0::2]]
        ys += [float(v) for v in polygon[1::2]]
    if not xs or w <= 0 or h <= 0:
        return None
    area = (min(max(xs), w) - max(min(xs), 0.0)) * (min(max(ys), h) - max(min(ys), 0.0)) / (w * h)
    return round(max(0.0, min(1.0, area)), 4)


def lines_from_words(words: list[dict[str, Any]], height: float) -> list[Line]:
    """OCR word boxes (``{content, polygon}``, top-left origin) grouped into text lines."""
    boxes = []
    for word in words or []:
        polygon = word.get("polygon") or []
        text = str(word.get("content") or "").strip()
        if not text or len(polygon) < 8 or not height:
            continue
        ys = [float(v) for v in polygon[1::2]]
        xs = [float(v) for v in polygon[0::2]]
        boxes.append((min(ys), max(ys), min(xs), text))
    if not boxes:
        return []
    heights = sorted(b[1] - b[0] for b in boxes)
    half = max(heights[len(heights) // 2] / 2.0, 1.0)
    boxes.sort(key=lambda b: ((b[0] + b[1]) / 2.0, b[2]))
    rows: list[list[tuple[float, float, float, str]]] = []
    for box in boxes:
        centre = (box[0] + box[1]) / 2.0
        if rows and abs(centre - sum((b[0] + b[1]) / 2.0 for b in rows[-1]) / len(rows[-1])) <= half:
            rows[-1].append(box)
        else:
            rows.append([box])
    return [
        Line(
            text=" ".join(b[3] for b in sorted(row, key=lambda b: b[2])),
            top=min(b[0] for b in row) / height,
            bottom=max(b[1] for b in row) / height,
        )
        for row in rows
    ]


def content_bottom(lines: list[Line]) -> Optional[float]:
    """Where the page's content ends (fraction of height), footers and stamps left out."""
    prof = canon()
    footer = float(prof.raw["zones"]["footer_band"])
    kept = [
        line for line in lines
        if line.top < 1.0 - footer and not any(p.search(line.text.strip()) for p in prof.ignore_lines)
    ]
    return max((line.bottom for line in kept), default=None)


# --- keyword scoring ----------------------------------------------------------------


@dataclass
class Scores:
    clinical: float = 0.0
    junk: float = 0.0
    demographic: float = 0.0
    groups: dict[str, float] = field(default_factory=dict)  # "junk.invoice" -> score
    hits: dict[str, list[str]] = field(default_factory=dict)  # "junk.invoice" -> terms
    # Junk terms only the Tesseract text had (pass 2), counted in ``hits`` too.
    tesseract_only: dict[str, list[str]] = field(default_factory=dict)

    def group(self, context: str, name: str) -> float:
        return self.groups.get(f"{context}.{name}", 0.0)

    def best_junk_group(self) -> Optional[str]:
        """Highest-scoring junk group; a tie goes to the higher weight."""
        prof = canon()
        found = [
            (score, prof.groups[("junk", key.split(".", 1)[1])].weight, key.split(".", 1)[1])
            for key, score in self.groups.items()
            if key.startswith("junk.") and score > 0
        ]
        return max(found)[2] if found else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "clinical": self.clinical,
            "junk": self.junk,
            "demographic": self.demographic,
            "junk_group": self.best_junk_group(),
            "hits": self.hits,
            **({"tesseract_only": self.tesseract_only} if self.tesseract_only else {}),
        }


def _spans(context: str, text: str) -> list[tuple[int, int, str, str]]:
    """(start, end, group, term) for every term and pattern hit of one context."""
    prof = canon()
    found: list[tuple[int, int, str, str]] = []
    for regex in (prof.term_re[context], prof.line_re[context]):
        if regex is None:
            continue
        for match in regex.finditer(text):
            entry = prof.term_of[context].get(_norm(match.group(1)))
            if entry:
                found.append((match.start(1), match.end(1), entry[0], entry[1]))
    # Heading lines: a short line containing a line-start term anywhere counts it.
    heading, max_words = prof.heading_re.get(context), prof.heading_words.get(context, 0)
    if heading is not None and max_words:
        offset = 0
        for line in text.split("\n"):
            if 0 < len(line.split()) <= max_words:
                for match in heading.finditer(line):
                    entry = prof.term_of[context].get(_norm(match.group(1)))
                    if entry:
                        found.append((offset + match.start(1), offset + match.end(1), entry[0], entry[1]))
            offset += len(line) + 1
    for name, pattern in prof.patterns[context]:
        for match in pattern.finditer(text):
            found.append((match.start(), match.end(), name, f"/{pattern.pattern}/"))
    return found


def _longest_first(spans: list[tuple[int, int, str, str]]) -> list[tuple[int, int, str, str]]:
    """Overlapping hits: the longest wins ('fax cover sheet' is not also 'fax cover')."""
    kept: list[tuple[int, int, str, str]] = []
    for span in sorted(spans, key=lambda s: (-(s[1] - s[0]), s[0])):
        if all(span[1] <= k[0] or span[0] >= k[1] for k in kept):
            kept.append(span)
    return kept


def score_text(
    text: str,
    lines: Optional[list[Line]] = None,
    also_junk: Optional[str] = None,
    document_type: str = "",
) -> Scores:
    """Weight x unique hits per group, capped; summed per context (BLANK_JUNK_LOGIC §2.1).

    With ``lines`` (pass 2), a junk term in the header or footer band counts only when the
    body has a junk hit too.

    ``also_junk`` (pass 2: the page's Tesseract text) adds the junk terms the final OCR
    missed, such as a vendor name in a logo or banner ('Optum'), as body hits. Clinical
    and demographic scores read ``text`` alone.

    A group with a ``document_type`` (id_card: "visual") counts only on pages of that
    page-tag type.
    """
    prof = canon()
    out = Scores()
    zones = prof.raw["zones"]
    header, footer = float(zones["header_band"]), float(zones["footer_band"])
    for context in ("clinical", "junk", "demographic"):
        if lines:
            unique: dict[str, set[str]] = {}
            edge: dict[str, set[str]] = {}
            body_junk = False
            for line in lines:
                at_edge = line.top < header or line.bottom > 1.0 - footer
                for _s, _e, name, term in _longest_first(_spans(context, line.text)):
                    target = edge if (context == "junk" and at_edge) else unique
                    target.setdefault(name, set()).add(term)
                    if context == "junk" and not at_edge:
                        body_junk = True
            if body_junk:
                for name, terms in edge.items():
                    unique.setdefault(name, set()).update(terms)
        else:
            unique = {}
            for _s, _e, name, term in _longest_first(_spans(context, text)):
                unique.setdefault(name, set()).add(term)
        if context == "junk" and also_junk:
            for _s, _e, name, term in _longest_first(_spans(context, also_junk)):
                if term not in unique.get(name, set()):
                    unique.setdefault(name, set()).add(term)
                    out.tesseract_only.setdefault(f"junk.{name}", []).append(term)
            for key in out.tesseract_only:
                out.tesseract_only[key] = sorted(set(out.tesseract_only[key]))
        total = 0.0
        for name, terms in unique.items():
            group = prof.groups[(context, name)]
            if group.document_type and group.document_type != document_type:
                continue
            score = min(group.weight * len(terms), group.cap)
            out.groups[f"{context}.{name}"] = score
            out.hits[f"{context}.{name}"] = sorted(terms)
            total += score
        setattr(out, context, total)
    return out


# --- DistilBERT ------------------------------------------------------------------------


def bucket(labels: list[str], probs: list[float]) -> tuple[float, float, str]:
    """(p_keep, p_junk, top junk label): p_junk = the JUNK_* labels only. BLANK is not junk
    (2026-11-04, user: as the teammate's service counts it); see blank_probability."""
    p_keep = p_junk = 0.0
    best: tuple[float, str] = (-1.0, "")
    for label, p in zip(labels, probs):
        p = max(0.0, float(p))
        if label.startswith("KEEP"):
            p_keep += p
        elif label.startswith("JUNK"):
            p_junk += p
            if p > best[0]:
                best = (p, label)
    return p_keep, p_junk, best[1]


def blank_probability(labels: list[str], probs: list[float]) -> float:
    """The BLANK label's probability (kept apart from p_junk since 2026-11-04)."""
    return sum(max(0.0, float(p)) for label, p in zip(labels, probs) if label.startswith("BLANK"))


# --- the decision --------------------------------------------------------------------


@dataclass
class PageInputs:
    text: str
    pass_no: int
    lines: Optional[list[Line]] = None  # pass 2 word boxes, as lines
    content_bottom: Optional[float] = None  # where the content ends (share of height); pass 2
    bert: Optional[dict[str, Any]] = None  # {"p_keep", "p_junk", "top_junk_label"}
    document_type: str = ""  # page-tag model: printed / handwritten / form / visual / ...
    page_tags: bool = False  # the page-tag model wrote document_type
    hw_area_pct: Optional[float] = None
    signed: bool = False  # Electronic Signature present (kv_extract, provider_signature.csv)
    # kv_extract found the member's name on the page (extraction.json "name"): the card rule's name.
    member_name_found: bool = False
    # The page's Electronic Signature repeats in the same spot (region + sentence / signer) on the previous / next
    # non-blank page: a stamp, not its own signing (stage._classify, 2026-11-15).
    neighbour_signed: bool = False
    signature_known: bool = False  # the KV signature output was there to read
    provider: bool = False  # provider details extracted (kv_extract's provider name / credentials); pass 2
    no_ink: bool = False  # the HW stage found no ink (blank_page / faint_marks_only)
    # 2026-11-19 (user): a handwritten / mixed or bad-quality (Low) page pass 1 never judged:
    # pass 2 runs pass 1's text rules on it once (form and visual pages: no).
    pass1_skipped: bool = False
    # The short page's outcome (stage._classify): {"code": CODE_* or None (to the classifier),
    # "rule": "S1".., "why": "..."}; None when the page is not short.
    short_verdict: Optional[dict[str, Any]] = None
    # kv_extract's provider on the page: (region, name words), for the short page's S3.
    # (region, name words, box centre or None): stage.provider_spots.
    provider_spot: Optional[tuple] = None
    # The previous and the next page are Progress Note pages (2+ note sections each; stage).
    flanked_by_notes: bool = False
    # A barcode on the page image (zxing-cpp format name), read for card candidates.
    barcode: Optional[str] = None
    body_ink: Optional[bool] = None  # ink below the short-page band; None = not measured
    body_ink_fraction: Optional[float] = None  # the share of that band that is ink
    # Pass 2: the page's Tesseract text, read for junk terms only (score_text also_junk).
    prelim_text: str = ""
    # Pass 2: the page's section headers (stage 6, in the Final OCR JSON), for the fax rule.
    headings: tuple[str, ...] = ()
    # Pass 2: the canon names stage 6 matched those headers to (matched_canonical).
    header_canon: tuple[str, ...] = ()
    # Pass 2: the share of the page inside the box round every OCR word (the card rule).
    text_area: Optional[float] = None
    # Pass 2: Final OCR 1 (Docling) markdown, for its tables (the invoice rule, 2026-11-14).
    final1_text: str = ""


@dataclass
class Verdict:
    code: int
    confidence: float
    rule: str
    reason: str
    votes: dict[str, Any] = field(default_factory=dict)
    # The rule-strength value the confidence was before 2026-11-14 (rule_strength): kept as the
    # weight continuity gives a junk run, so the new confidences change no decision.
    strength: Optional[float] = None

    @property
    def is_junk(self) -> bool:
        return self.code not in (CODE_MAIN, CODE_BLANK)


def _word_count(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9]+", text or ""))


def visual_max_words() -> int:
    return int(canon().raw["visual"].get("max_words", 20))


def is_visual(document_type: str, words: int) -> bool:
    """Visual = the page-tag model says visual AND the page has fewer than ``max_words``
    words (canon ``visual.max_words``, 20). A visual page with more text is judged like
    any other page."""
    return document_type == "visual" and words < visual_max_words()


def text_is_visual(document_type: str, text: Optional[str]) -> bool:
    return is_visual(document_type, _word_count(text or ""))


def _junk_code(subtype: str) -> int:
    return SUBTYPE_CODE.get(subtype, CODE_OTHERS)


def signature_keeps(signed: bool, p_junk: Optional[float], neighbour_signed: bool = False) -> bool:
    """THE signature condition, used everywhere a signature saves a page, on every page:
    kv_extract found an Electronic Signature on it, it is not repeated in the same spot on the previous
    / next non-blank page (2026-11-15, user; was "the previous and next pages have none"), AND the
    model's raw p(junk) is below ``distilbert.signature_overruled_above`` (0.95; 2026-11-07).
    Signature words in the text never count. No model score (None) leaves the signature standing."""
    if not signed or neighbour_signed:
        return False
    return p_junk is None or float(p_junk) < _thresholds()["signature_overruled_above"]


def page_p_junk(inputs: "PageInputs") -> Optional[float]:
    """The raw p(junk) of a page's model scores, None when the model did not run."""
    return float(inputs.bert["p_junk"]) if inputs.bert else None


def _thresholds() -> dict[str, float]:
    cfg = canon().raw.get("distilbert", {})
    return {
        "pass1_final_keep_above": float(cfg.get("pass1_final_keep_above", 0.95)),
        "pass1_final_junk_above": float(cfg.get("pass1_final_junk_above", 0.95)),
        "junk_at": float(cfg.get("junk_at", 0.70)),
        "review_junk_at": float(cfg.get("review_junk_at", 0.70)),
        "signature_overruled_above": float(cfg.get("signature_overruled_above", 0.95)),
    }


def is_final_keep(reason: Optional[str]) -> bool:
    """A pass-1 verdict pass 2 must not judge again (DistilBERT was sure it is a keep)."""
    return (reason or "").startswith(FINAL_KEEP)


def _short_page(inputs: PageInputs, p_keep: Optional[float], votes: dict[str, Any]) -> Optional[str]:
    """R6's reason when the page is short and empty below, else None; records
    ``votes.short_page`` either way. Not measured (no word boxes, unreadable image) never
    fires; nor does a page DistilBERT is sure to keep (p(keep) > ``skip_keep_above``)."""
    cfg = canon().raw["short_page"]
    top = float(canon().raw["zones"]["header_band"])
    if inputs.content_bottom is None:
        votes["short_page"] = "no_geometry"
        return None
    info: dict[str, Any] = {"content_bottom": round(inputs.content_bottom, 4)}
    votes["short_page"] = info
    if inputs.content_bottom > top:
        return None
    limit = float(cfg["body_ink_max_fraction"])
    info.update(
        ink="not_measured" if inputs.body_ink is None else inputs.body_ink,
        ink_fraction=inputs.body_ink_fraction,
        ink_limit=limit,
    )
    if inputs.body_ink is None or inputs.body_ink:
        return None
    keep_above = float(cfg.get("skip_keep_above", 0.9))
    if p_keep is not None and p_keep > keep_above:
        info["skipped"] = f"DistilBERT p(keep) {p_keep:.3f} > {keep_above}"
        return None
    return (
        f"R6 short page: content ends at {inputs.content_bottom:.1%} <= {top:.0%}, "
        f"ink below {inputs.body_ink_fraction or 0:.2%} < {limit:.1%} -> Junk / Others"
    )


# The junk verdicts KEEP_VETO_AT can overrule (2026-11-19, user: "just Fax / Cover").
KEEP_VETO_CODES = frozenset({CODE_LETTER_FAX, CODE_COVER_PAGE})


def rule_flips_model(code: int, p_keep: Optional[float], p_junk: Optional[float],
                     p_blank: Optional[float]) -> bool:
    """Does a rule's verdict go against DistilBERT's top class, Keep vs Junk / Blank? (Junk vs
    Blank is not a flip.) False without the model."""
    if p_keep is None or p_junk is None:
        return False
    model_keeps = float(p_keep) >= max(float(p_junk), float(p_blank or 0.0))
    return model_keeps != (code == CODE_MAIN)


def keep_veto_at() -> float:
    """DistilBERT's p(keep) at or above which no rule junks a page that is not short (canon
    ``keep_veto_at``, 0.99; 2026-11-19, user)."""
    return float(canon().raw.get("keep_veto_at", 0.99))


def verdict_confidence(code: int, p_keep: Optional[float], p_junk: Optional[float],
                       p_blank: Optional[float] = None, *, model_class: Optional[str] = None) -> float:
    """How sure the verdict is (2026-11-19, user: "make it real", was flipped 0.30 when the model's
    top class was another): the model's raw probability for the verdict's class -- Main
    p(keep), Blank p(blank), Junk p(junk) -- whichever rule decided; ``confidence.backup`` only
    without the model. ``model_class`` names the class the verdict stands for when it is not the
    code's own (an Invoice kept Main is the model's junk call; the service's Blank filed Junk /
    Others is its blank call)."""
    if p_keep is None or p_junk is None:
        return conf.backup()
    probs = {"keep": float(p_keep), "junk": float(p_junk), "blank": float(p_blank or 0.0)}
    mine = model_class or ("keep" if code == CODE_MAIN else "blank" if code == CODE_BLANK else "junk")
    return conf.chosen(probs[mine], module="blank_junk")


def rule_strength(code: int, rule: str, p_keep: Optional[float], p_junk: Optional[float]) -> float:
    """A rule's verdict confidence before 2026-11-14, kept as ``Verdict.strength`` (the
    junk-run weight continuity reads). R1-R2 (no text to read): the rule's strength. R4 / R6: DistilBERT's probability for the verdict (p(keep) for
    Main, p(junk) otherwise) lifted by the rule's strength, times ``keywords_disagree``
    when DistilBERT itself says the other side (>= 0.5); without DistilBERT the strength
    times ``no_model``."""
    strength = RULE_SUPPORT.get(rule, 0.5)
    if rule in NO_TEXT_RULES:
        return conf.score(strength, module="blank_junk")
    p = p_keep if code == CODE_MAIN else p_junk
    other = p_junk if code == CODE_MAIN else p_keep
    if p is None:
        return conf.score(strength, "no_model", module="blank_junk")
    return conf.score(conf.backed(p, strength), "keywords_disagree" if (other or 0.0) >= 0.5 else "",
                      module="blank_junk")


def rule_confidence(code: int, rule: str, p_keep: Optional[float], p_junk: Optional[float],
                    p_blank: Optional[float] = None) -> float:
    """A rule's verdict confidence (2026-11-14): ``verdict_confidence``; an Invoice kept
    Main is the model's junk call (p(junk))."""
    return verdict_confidence(code, p_keep, p_junk, p_blank, model_class="junk" if code == CODE_INVOICE else None)


# A cover mark on a page of fewer than COVER_MAX_WORDS words is a cover page, both passes
# (2026-11-05, user; 2026-11-08, user: under 10 words, was 5). "End of Report" on a page of
# fewer than COVER_MAX_WORDS words is its own rule (R1E), right after.
COVER_MARKS = frozenset({"accept", "accepted", "unaccept", "unaccepted", "cover"})
COVER_MAX_WORDS = 10
# Pass 2 rule 7: the service's JUNK stands only while the raw p(keep) is below this.
JUNK_UNLESS_KEEP_AT = 0.99


def _cover_mark(text: str) -> Optional[str]:
    """The cover mark on a page of fewer than COVER_MAX_WORDS words, else None."""
    if _word_count(text) >= COVER_MAX_WORDS:
        return None
    return next((w for w in re.findall(r"[A-Za-z]+", text or "") if w.casefold() in COVER_MARKS), None)


def header_only(inputs: PageInputs) -> bool:
    """Pass 2: the page's content ends in the top header band and the band below is measured
    free of ink. Not measured (no word boxes, no image) is never header-only."""
    if inputs.pass_no != 2 or inputs.content_bottom is None or inputs.body_ink is not False:
        return False
    return inputs.content_bottom <= float(canon().raw["zones"]["header_band"])


def decide(inputs: PageInputs) -> Verdict:
    """The page's verdict (2026-11-04, user: the simplified ladder; first rule that fires wins).

    Pass 1 (Tesseract, rules only): Blank (no ink / stage 1 blank / no text / 'blank'),
    'intentionally left blank' -> Blank, a cover mark in < 10 words -> Junk / Cover Page,
    End of Report in < 10 words -> Junk / Cover Page, < 15 characters
    -> Junk / Others; anything else waits for the Final OCR and pass 2.

    Pass 2 (final OCR + the teammate's PageClassifierService; p(keep) / p(junk) / p(blank)
    are the raw model numbers, the flag is the service's decision): the blank rules, card ->
    Junk / Cards, visual < 20 words -> Main, > 30% handwritten -> Main, signed -> Main unless
    raw p(junk) > 0.95, fax in the section headers and unsigned -> Junk / Letter/Fax, then the
    service: JUNK with raw p(keep) < 0.99 -> Junk / its subtype, BLANK -> Blank, else Main.
    The short page contained in a nearby page is applied after, in stage._classify."""
    prof = canon()
    t = _thresholds()
    text = inputs.text or ""
    words = _word_count(text)
    pass2 = inputs.pass_no == 2
    lines = inputs.lines if pass2 else None
    # Read only for the placeholder and card wording; nothing is voted.
    scores = score_text(text, lines, inputs.prelim_text if pass2 else None, inputs.document_type)
    visual = is_visual(inputs.document_type, words)
    bert = inputs.bert
    p_junk = float(bert["p_junk"]) if bert else None
    p_keep = float(bert["p_keep"]) if bert else None
    p_blank = float(bert.get("p_blank") or 0.0) if bert else None
    service = (bert or {}).get("service")
    votes: dict[str, Any] = {
        "pass": inputs.pass_no,
        "bert": (
            {"p_keep": round(p_keep, 4), "p_junk": round(p_junk, 4),
             "p_blank": round(float(bert.get("p_blank") or 0.0), 4), "label": (bert or {}).get("top_junk_label") or ""}
            if bert else {"note": "unavailable" if pass2 else "not run in pass 1"}
        ),
        "service": service or "none",
        "visual_source": "page_tags" if inputs.page_tags else "none",
        "signature_source": ("kv_extract" if inputs.signature_known else "kv_extract_missing") if pass2 else "not_in_pass_1",
        "hw_area": inputs.hw_area_pct if inputs.hw_area_pct is not None else "none",
    }
    if inputs.document_type == "visual" and not visual:
        votes["visual"] = f"document_type visual, but {words} words >= {visual_max_words()}: not Visual"

    def ruled(code: int, rule: str, reason: str, strength: Optional[float] = None,
              confidence: Optional[float] = None) -> Verdict:
        """``strength``: the pre-2026-11-14 value (rule_strength when None); ``confidence``:
        the new one (verdict_confidence when None)."""
        # 2026-11-19 (user): on a page that is not short, a rule may file it Junk / Letter/Fax or
        # Junk / Cover Page only while DistilBERT's p(keep) is under KEEP_VETO_AT (99%): a page
        # the model is that sure of stays Main. Other junk rules, the no-text rules (R1, R2), the
        # short page's own rules and the classifier (R5) are not overruled.
        if (code in KEEP_VETO_CODES and rule not in NO_TEXT_RULES + ("R5",) and not inputs.short_verdict
                and p_keep is not None and p_keep >= keep_veto_at()):
            votes["keep_veto"] = f"{rule} -> {code}: p(keep) {p_keep:.2%} >= {keep_veto_at():.0%}"
            votes["rule"] = "KEEP99"
            value = verdict_confidence(CODE_MAIN, p_keep, p_junk, p_blank)
            return Verdict(CODE_MAIN, float(value), "KEEP99",
                           f"KEEP99: {rule} would junk it, but p(keep) {p_keep:.1%} >= {keep_veto_at():.0%} -> Main",
                           votes, strength=round(float(value), 4))
        votes["rule"] = rule
        old = rule_strength(code, rule, p_keep, p_junk) if strength is None else strength
        if confidence is None and rule_flips_model(code, p_keep, p_junk, p_blank):
            # 2026-11-19 (user): a hard-coded rule that turns the model's Keep into Junk / Blank,
            # or its Junk / Blank into Main: the backup 40%.
            votes["flipped_model"] = True
            confidence = conf.backup()
        value = verdict_confidence(code, p_keep, p_junk, p_blank) if confidence is None else confidence
        return Verdict(code, float(value), rule, reason, votes, strength=round(float(old), 4))

    # ---- measured blanks: every pass (nothing to read, nothing missed).
    chars = len(re.sub(r"\s", "", text))
    votes["empty"] = bool(inputs.no_ink or (not visual and chars == 0))
    votes["card_keywords"] = scores.hits.get(f"junk.{prof.raw['visual']['card_group']}", [])
    if inputs.no_ink:
        return ruled(CODE_BLANK, "R1", "R1 blank: no ink", confidence=conf.measured())
    # Stage 1's Blank only when the OCR agrees: a page with text on it is not blank (2026-11-15).
    if inputs.document_type == "blank" and chars < MIN_TEXT_CHARS:
        return ruled(CODE_BLANK, "R1", "R1 blank: stage 1 page type blank", confidence=conf.measured())
    # ... or when that text is only a header line (2026-11-17, user): pass 2, measured.
    if inputs.document_type == "blank" and header_only(inputs):
        return ruled(CODE_BLANK, "R1", f"R1 blank: stage 1 page type blank, text only in the top "
                                       f"{float(prof.raw['zones']['header_band']):.0%} header band",
                     confidence=conf.measured())
    if inputs.document_type == "blank":
        votes["stage1_blank_overruled"] = f"{chars} characters of text"
    if not visual and chars == 0:
        return ruled(CODE_BLANK, "R1", "R1 blank: no text", confidence=conf.measured())

    # ---- pass 1's text rules (2026-11-19, user: pass 2 does not redo pass 1): in pass 1, and in
    # pass 2 only on a page pass 1 never judged (handwritten, mixed, form, visual, printed Low).
    if not pass2 or inputs.pass1_skipped:
        if not visual and chars < BLANK_WORD_MAX_CHARS and BLANK_WORD.search(text):
            return ruled(CODE_BLANK, "R1", f"R1 blank: the word 'blank' in < {BLANK_WORD_MAX_CHARS} characters",
                         confidence=conf.pass1_rule())
        if scores.group("junk", "placeholder") > 0 and words <= PLACEHOLDER_MAX_WORDS:
            return ruled(CODE_BLANK, "R2", "R2 placeholder ('intentionally left blank') -> Blank",
                         confidence=conf.pass1_rule())
        mark = _cover_mark(text)
        if mark is not None:
            return ruled(_junk_code("Cover Page"), "R1",
                         f"R1 cover mark '{mark}' on a page of < {COVER_MAX_WORDS} words -> Junk / Cover Page",
                         confidence=conf.pass1_rule())
        end = END_OF_REPORT.search(text) if _word_count(text) < COVER_MAX_WORDS else None
        if end is not None:
            return ruled(_junk_code("Cover Page"), "R1E",
                         f"R1E '{end.group(0)}' on a page of < {COVER_MAX_WORDS} words -> Junk / Cover Page", 1.0,
                         confidence=conf.pass1_rule())
        if not visual and chars < MIN_TEXT_CHARS:
            return ruled(_junk_code("Others"), "R1", f"R1 tiny page: < {MIN_TEXT_CHARS} characters -> Junk / Others",
                         confidence=conf.pass1_rule())
    if not pass2:
        votes["deferred"] = True
        return ruled(CODE_MAIN, "P1", "pass 1: no rule fired -> Main for now, judged in pass 2 on the final OCR",
                     conf.score(0.5, "degraded"), confidence=conf.backup())

    # ---- pass 2 (2026-11-19, user): contents and fax cover sheets, then the short page, then
    # the other pages: keeps, card, fax letter, stage 1's Blank, the classifier.
    # 1. a contents / index page (before the keeps: its entries read like a medication list).
    index = index_page(text, inputs.headings, inputs.lines)
    if index is not None:
        votes["index_page"] = index
        return ruled(_junk_code("Others"), "INDEX", f"INDEX: contents page ({index}) -> Junk / Others")
    # 2. a fax cover sheet (2026-11-15): Fax + 2 routing boxes, even when signed.
    cover = fax_cover_sheet(text)
    if cover is not None:
        votes["fax_cover"] = cover
        return ruled(CODE_LETTER_FAX, "FAX", f"FAX: cover sheet ({cover}) -> Junk / Letter/Fax")

    # 3. the short page (stage._classify measured it against the previous / next page).
    short = inputs.short_verdict
    if short:
        votes["short_page"] = short["why"]
        if short["code"] is not None:
            # 2026-11-19 (user): a short page the short rules decided: the rules-find 60%.
            return ruled(short["code"], short["rule"], f"{SHORT_RULE}{short['rule']}: {short['why']}",
                         confidence=conf.rules_find())
    to_classifier = bool(short)  # a short page no short rule decided goes to the classifier

    card = None
    found_card = None
    if not to_classifier:
        # The card is looked for first (a licence is often a visual page), and decided at 5,
        # after the keeps.
        card = (detect_card(text, headings=tuple(inputs.header_canon or inputs.headings or ()), text_area=inputs.text_area,
                            visual=inputs.document_type == "visual", name_found=inputs.member_name_found,
                            barcode=inputs.barcode)
                if inputs.document_type in CARD_DOCUMENT_TYPES else None)
        if card is not None and card.is_card:
            votes["card"] = {"type": card.card_type, "matched": card.matched}
            found_card = card
        elif card is not None and card.blocked_by:
            votes["card_near_miss"] = {"type": card.card_type, "blocked_by": card.blocked_by}
        # 4. keeps: a picture, handwriting, a signature, a lab result, medications, clinical content.
        if visual and found_card is None:
            return ruled(CODE_MAIN, "R3V", f"R3V visual: under {visual_max_words()} words, not a card -> Main", 0.90)
        hw_limit = float(prof.raw["handwritten_veto"]["min_handwritten_area_pct"])
        if inputs.hw_area_pct is not None and inputs.hw_area_pct > hw_limit:
            return ruled(CODE_MAIN, "HW", f"HW: {inputs.hw_area_pct:g}% handwritten > {hw_limit:g}% -> Main", 0.90)
        if signature_keeps(inputs.signed, p_junk, inputs.neighbour_signed):
            return ruled(CODE_MAIN, "R4", "R4 signed (Electronic Signature, not repeated nearby, raw p(junk) "
                                          f"{'n/a' if p_junk is None else f'{p_junk:.3f}'}) -> Main")
        if inputs.signed and inputs.neighbour_signed:
            votes["r4_not_alone"] = "Electronic Signature, but the previous or next page has the same signature in the same spot"
        elif inputs.signed:
            votes["r4_overruled"] = f"Electronic Signature, but raw p(junk) {p_junk:.3f} >= {t['signature_overruled_above']}"
        lab = lab_result_words(inputs)
        if lab:
            return ruled(CODE_MAIN, "LAB", f"LAB: lab result ({' / '.join(lab)}) -> Main")
        meds = medication_doses(text)
        if meds:
            return ruled(CODE_MAIN, MEDS_RULE, f"{MEDS_RULE}: medication list ({meds}) -> Main")
        clinical = clinical_content_signs(inputs)
        if clinical:
            return ruled(CODE_MAIN, CLINICAL_CONTENT_RULE, f"{CLINICAL_CONTENT_RULE}: {clinical} -> Main")

        # 5. card (printed / visual / uncertain): an ID, licence or insurance card.
        if found_card is not None:
            found = f"{found_card.card_type} ({'; '.join(found_card.matched)})"
            if p_keep is not None and p_keep >= JUNK_UNLESS_KEEP_AT:
                return ruled(CODE_MAIN, "R3", f"R3 card: {found}, but raw p(keep) {p_keep:.3f} >= {JUNK_UNLESS_KEEP_AT} -> Main")
            return ruled(CODE_CARDS, "R3", f"R3 card: {found} -> Junk / Cards", 0.95)

        # 6. the fax header (2026-11-20, user): Fax in the section headers and no signature ->
        # Junk / Letter/Fax. A page that is not short with a Progress Note page on each side
        # ("To: ... Fax" inside a note) is left to the model. (The fax letter rule -- Fax, a
        # letter's closing, no signature -- was removed the same day.)
        word = fax_unsigned(inputs)
        if word is not None:
            if not inputs.short_verdict and inputs.flanked_by_notes:
                votes["fax_unsigned"] = f"'{word}', but Progress Note pages on both sides: the model decides"
            else:
                votes["fax_unsigned"] = word
                return ruled(CODE_LETTER_FAX, "FAX", f"FAX: '{word}' in the section headers and no signature -> Junk / Letter/Fax")

        # 7. stage 1's Blank as the fallback (2026-11-17, user): no rule above kept it.
        if inputs.document_type == "blank":
            return ruled(CODE_BLANK, "R1", "R1 blank: stage 1 page type blank, no rule kept it (fallback)",
                         confidence=conf.backup())

    # 8. the classifier (the teammate's service), then the invoice post-processing.
    votes["reached_classifier"] = True
    if service is None:
        votes["review_required"] = True
        return ruled(CODE_MAIN, "R5", "R5: page classifier unavailable -> Main (review)", conf.score(0.5, "degraded"),
                     confidence=conf.backup())
    why = _service_why(service)
    if service.get("review_required"):
        votes["review_required"] = True
    if service["flag"] == "JUNK":
        if p_keep is not None and p_keep >= JUNK_UNLESS_KEEP_AT:
            return ruled(CODE_MAIN, "R5", f"R5 service {why}, but raw p(keep) {p_keep:.3f} >= {JUNK_UNLESS_KEEP_AT} -> Main")
        subtype = _service_subtype(service)
        # 2026-11-07 (user): a card is Junk / Cards, never Invoice: the card rule's near miss or the
        # service's insurance-ID tag says card. 2026-11-19 (user): a near miss only when nothing
        # but the card's own guards stopped it -- never a form, letter or note page (an
        # anesthesia note read as an invoice became Cards), never over the card length.
        near_card = (subtype == "Invoice" and card is not None and bool(card.card_type)
                     and not any(b.startswith(("form:", "letter / statement:", "too long")) for b in card.blocked_by)
                     and not note_content(inputs))
        if near_card and FACILITY_WORDS.search(text) and not CARD_WORD.search(text):
            near_card = False
            votes["card_not_kept"] = "care provider's page, no 'card' on it"
        if near_card or "INSURANCE_ID" in str(service.get("audit_tag") or ""):
            subtype = "Cards"
        verdict = ruled(_junk_code(subtype), "R5", f"R5 service {why} -> Junk / {subtype}",
                        conf.score(float(service["confidence"])))
        return _keep_invoice(verdict, inputs)
    if service["flag"] == "BLANK":
        # 2026-11-05 (user): the classifier's Blank is Junk / Others (Blank comes from R1 / R2).
        return ruled(_junk_code("Others"), "R5", f"R5 service {why} -> Junk / Others",
                     conf.score(float(service["confidence"])),
                     confidence=verdict_confidence(CODE_OTHERS, p_keep, p_junk, p_blank, model_class="blank"))
    return ruled(CODE_MAIN, "R5", f"R5 service {why} -> Main", conf.score(float(service["confidence"])))


def _service_subtype(service: dict[str, Any]) -> str:
    return MODEL_SUBTYPE.get(service.get("subclass") or "", "Others")


def _service_why(service: dict[str, Any]) -> str:
    probs = " ".join(f"{k}={service[k]:.3f}" for k in ("p_keep", "p_blank", "p_junk") if service.get(k) is not None)
    return f"{service['flag']} {service['confidence']:.3f} ({service.get('decision_reason') or 'argmax'}{'; ' + probs if probs else ''})"


def _handwritten(inputs: PageInputs, prof: Any) -> bool:
    hw_limit = float(prof.raw["handwritten_veto"]["min_handwritten_area_pct"])
    return inputs.hw_area_pct is not None and inputs.hw_area_pct > hw_limit


def _terms(words: list[str]) -> Optional[re.Pattern[str]]:
    words = [w for w in (words or []) if str(w).strip()]
    if not words:
        return None
    return re.compile(r"(?<![A-Za-z])(?:" + "|".join(re.escape(str(w)).replace(r"\ ", r"\s+")
                                                     for w in sorted(words, key=len, reverse=True)) + r")(?![A-Za-z])",
                      re.IGNORECASE)


FAX_WORD = re.compile(r"(?<![A-Za-z])(?:fax|facsimile)(?![A-Za-z])", re.IGNORECASE)
FAX_COVER_BOXES = re.compile(
    r"(?<![A-Za-z])(?:urgent|for\s+(?:your\s+)?review|please\s+comment|please\s+reply|reply\s+asap"
    r"|please\s+recycle|as\s+requested)(?![A-Za-z])", re.IGNORECASE)


def fax_cover_sheet(text: str) -> Optional[str]:
    """A fax cover sheet: the word Fax and 2+ of its routing boxes (Urgent, For Review, Please
    Comment, Please Reply, Please Recycle, As Requested), or None (2026-11-15, user)."""
    if not FAX_WORD.search(text or ""):
        return None
    boxes = list(dict.fromkeys(" ".join(m.group(0).lower().split()) for m in FAX_COVER_BOXES.finditer(text or "")))
    return f"Fax + {', '.join(boxes)}" if len(boxes) >= 2 else None


FAX_NUMBER = re.compile(
    r"(?<![A-Za-z])(?:fax|facsimile)(?:\s*(?:no\.?|number|#|ph(?:one)?))?\s*[:#.]?\s*"
    r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}", re.IGNORECASE)
FAX_ROUTING = re.compile(r"(?im)^\W*(?:to|attn|attention|recipient|from|sender)\s*[:\-]")


def fax_routing(text: str) -> Optional[str]:
    """A fax routing block (2026-11-19, user: a short "To: ... Fax: (555) 123-4567" page is Junk
    even when it names a provider): a fax number and a To / Attn / From line, or None."""
    number = FAX_NUMBER.search(text or "")
    if number is None or not FAX_ROUTING.search(text or ""):
        return None
    return "To / From + fax number"


def fax_unsigned(inputs: PageInputs) -> Optional[str]:
    """2026-11-02 (user): the fax word the page's header band prints when the page has no
    signature at all (canon ``fax_unsigned``); None otherwise or outside pass 2."""
    cfg = canon().raw.get("fax_unsigned") or {}
    if inputs.pass_no != 2 or not cfg.get("enabled", True) or signature_keeps(inputs.signed, page_p_junk(inputs), inputs.neighbour_signed):
        return None
    fax = _terms(cfg.get("fax_terms") or [])
    if fax is None:
        return None
    # The page's section headers (stage 6), not a band of the page (2026-11-03, user).
    header = "\n".join(inputs.headings)
    found = fax.search(header) if header else None
    return found.group(0) if found else None


SHORT_RULE = "short_page:"


def note_content(inputs: PageInputs) -> Optional[str]:
    """Why the page is part of a note (the fax rules never junk it), or None: a Progress Note
    header, ICD-10 codes or a medication list on it (2026-11-19, user)."""
    from modules.blank_junk.pn_headers import clinical_sections_on

    sections = clinical_sections_on(inputs.text or "", inputs.headings)
    if sections:
        return f"Progress Note header ({sections[0]})"
    codes = {m.group(0).upper() for m in ICD10_CODE.finditer(inputs.text or "")}
    if codes:
        return f"{len(codes)} ICD-10 code(s)"
    meds = medication_doses(inputs.text or "")
    return f"medications ({meds})" if meds else None


INVOICE_RULE = "INVOICE"


def post_process(verdict: Verdict, inputs: PageInputs) -> Verdict:
    """Nothing after the ladder any more (2026-11-08, user): the lab result and clinical content
    keeps are ladder rules (LAB, CLINICAL_CONTENT, after the signature) and the invoice check is
    part of the classifier's rule. Kept so callers need no change."""
    return verdict


# 2026-11-06 (user): an ICD-10-CM code as printed ("I11.0", "E11.319", "F03.C0", "Z68.1"):
# a letter, a digit, a digit or letter, a dot, 1-4 more.
ICD10_CODE = re.compile(r"(?<![A-Za-z0-9])[A-TV-Z][0-9][0-9A-Z]\.[0-9A-Z]{1,4}(?![A-Za-z0-9])")
SOCIAL_HISTORY = re.compile(r"(?<![A-Za-z])social\s+history(?![A-Za-z])", re.IGNORECASE)
SOCIAL_HISTORY_LINE = re.compile(r"^\W*social\s+history\W*$", re.IGNORECASE | re.MULTILINE)
# 2026-11-15 (user): a medication list with doses is Main, like a lab result. A dose = a number and
# a unit (mg, mcg, mL, units, mEq ...). 3+ different doses, or a Medication(s) / Rx / Prescription
# word and 2+.
MEDS_RULE = "MEDS"
RULE_SUPPORT[MEDS_RULE] = 0.95
DOSE = re.compile(
    r"(?<![\w.])\d+(?:[.,]\d+)?\s*(?:-\s*\d+(?:\.\d+)?\s*)?"
    r"(?:mg|mcg|µg|ug|g|gm|ml|mL|units?|iu|meq|mmol|mg/ml|mg/kg|mcg/act|puffs?|tabs?|tablets?|caps?|capsules?)"
    r"(?![A-Za-z])", re.IGNORECASE)
MEDS_WORD = re.compile(r"(?<![A-Za-z])(?:medications?|meds|rx|prescriptions?|prescribed|dosage)(?![A-Za-z])",
                       re.IGNORECASE)


def medication_doses(text: str) -> Optional[str]:
    """The doses that make the page a medication list, or None (MEDS_RULE)."""
    doses = list(dict.fromkeys(" ".join(m.group(0).lower().split()) for m in DOSE.finditer(text or "")))
    if len(doses) >= 3 or (len(doses) >= 2 and MEDS_WORD.search(text or "")):
        return f"{len(doses)} doses: {', '.join(doses[:4])}{' ...' if len(doses) > 4 else ''}"
    return None


CLINICAL_CONTENT_RULE = "CLINICAL_CONTENT"
RULE_SUPPORT[CLINICAL_CONTENT_RULE] = 0.95


def clinical_content_signs(inputs: PageInputs) -> Optional[str]:
    """2026-11-06 (user): why a junk page is clinical content -- 2+ distinct ICD-10 codes on it
    (canon ``clinical_content_keep.min_icd_codes``), or a Social History section header (stage 6,
    or a line of its own) -- or None."""
    cfg = canon().raw.get("clinical_content_keep") or {}
    if not cfg.get("enabled", True):
        return None
    text = f"{inputs.text or ''}\n{inputs.prelim_text or ''}"
    codes = sorted({m.group(0).upper() for m in ICD10_CODE.finditer(text)})
    if len(codes) >= int(cfg.get("min_icd_codes", 2)):
        shown = ", ".join(codes[:4]) + (f" (+{len(codes) - 4})" if len(codes) > 4 else "")
        return f"{len(codes)} ICD-10 codes ({shown})"
    # 2026-11-19 (user): two or more Progress Note sections on the page (pn_headers.py, matched
    # loosely: "Orders", "Plan of Care", "Diagnoses") make it a note.
    from modules.blank_junk.pn_headers import clinical_sections_on

    sections = clinical_sections_on(inputs.text or "", inputs.headings)
    if len(sections) >= int(cfg.get("min_note_sections", 2)):
        return f"{len(sections)} Progress Note sections ({', '.join(sections[:3])})"
    headers = tuple(inputs.header_canon or ()) + tuple(inputs.headings or ())
    if cfg.get("social_history", True) and (
            any(SOCIAL_HISTORY.search(str(h)) for h in headers) or SOCIAL_HISTORY_LINE.search(inputs.text or "")):
        return "Social History section header"
    return None


def _keep_clinical_content(verdict: Verdict, inputs: PageInputs) -> Verdict:
    """2026-11-06 (user): a junk page with ICD-10 codes or a Social History header is Main.
    Blank stays Blank; a Junk / Invoice page goes to the INVOICE rule."""
    if verdict.code in (CODE_MAIN, CODE_BLANK, CODE_INVOICE):
        return verdict
    why = clinical_content_signs(inputs)
    if why is None:
        return verdict
    verdict.votes["clinical_content_kept"] = verdict.reason
    verdict.votes["rule"] = CLINICAL_CONTENT_RULE
    p_keep, p_junk, p_blank = _model_probs(inputs)
    return Verdict(CODE_MAIN, rule_confidence(CODE_MAIN, CLINICAL_CONTENT_RULE, p_keep, p_junk, p_blank),
                   CLINICAL_CONTENT_RULE, f"{verdict.reason}; {why} -> Main (never junk)", verdict.votes,
                   strength=rule_strength(CODE_MAIN, CLINICAL_CONTENT_RULE, p_keep, p_junk))


def _model_probs(inputs: PageInputs) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """The page's raw p(keep), p(junk), p(blank); None each without the model."""
    bert = inputs.bert or {}
    if not bert:
        return None, None, None
    return float(bert["p_keep"]), float(bert["p_junk"]), float(bert.get("p_blank") or 0.0)


# 2026-11-08 (user): a contents page -- "Index" / "Contents" as a title and page numbers -- is
# Junk / Others. The title is a line of its own (or a section header), so "Body Mass Index"
# inside a sentence never counts; the page numbers are lines ending in a page number after
# dot leaders or spaces ("Progress Note ....... 12"), or "Page 3" / "Pages 4-7".
INDEX_TITLE = re.compile(r"^\W*(?:table\s+of\s+contents|contents|(?:document\s+|chart\s+|record\s+)?index)\W*$",
                         re.IGNORECASE | re.MULTILINE)
PAGE_NUMBER_LINE = re.compile(r"^(?=.*[A-Za-z]).*?(?:\.{2,}|\s)\s*\d{1,4}\s*$|\bpages?\s*\d{1,4}(?:\s*[-\u2013]\s*\d{1,4})?\b",
                              re.IGNORECASE)
INDEX_MIN_PAGE_LINES = 3
# 2026-11-15 (user): a contents page of encounters lists dates, not page numbers
# ("Encounter: 4/14/2026 9:15:00 AM"): a line with a date counts as an entry too.
INDEX_ENTRY_DATE = re.compile(r"(?<!\d)\d{1,2}/\d{1,2}/\d{2,4}(?!\d)|(?<!\d)\d{4}-\d{1,2}-\d{1,2}(?!\d)")


# 2026-11-20 (user): the contents title must sit in the top third of the page, and then it is
# enough on its own -- no page numbers or dates needed after it.
INDEX_TOP_FRACTION = 1 / 3


def _title_position(title_text: str, text: str, lines: Any = None) -> Optional[float]:
    """Where the contents title sits (its top, a share of the page height): from the OCR line
    positions when there are any, else its line's place among the page's text lines."""
    want = " ".join(re.findall(r"[a-z]+", title_text.lower()))
    for line in lines or ():
        got = " ".join(re.findall(r"[a-z]+", str(getattr(line, "text", "") or "").lower()))
        if got and got == want:
            return float(line.top)
    text_lines = [line for line in (text or "").splitlines() if line.strip()]
    for i, line in enumerate(text_lines):
        if INDEX_TITLE.fullmatch(line.strip()):
            return i / max(1, len(text_lines))
    return None


BARE_PAGE_NUMBER = re.compile(r"^\d{1,4}(?:\s*[-–]\s*\d{1,4})?$")


def _entry(line: str) -> bool:
    """A contents entry: a line ending in a page number (dot leaders, spaces, or a table cell:
    Docling writes a contents list as "| Progress Note | 12 |"), "Page 3" / "Pages 4-7", a page
    number on a line of its own, or a date (an encounter list)."""
    cells = " ".join(part.strip() for part in line.split("|") if part.strip())
    cells = re.sub(r"^[-:\s]+$", "", cells)  # a markdown table's ---|--- rule
    if not cells:
        return False
    return bool(PAGE_NUMBER_LINE.search(cells) or BARE_PAGE_NUMBER.match(cells) or INDEX_ENTRY_DATE.search(cells))


def index_page(text: str, headings: tuple[str, ...] = (), lines: Any = None) -> Optional[str]:
    """Why the page is a contents / index page, or None: "Table of Contents" / "Contents" /
    "Index" as a title (a line of its own, or a section header) in the top third of the page,
    followed by 3+ entries with page numbers or dates (2026-11-20, user)."""
    title = INDEX_TITLE.search(text or "")
    title_text = title.group(0).strip() if title else next(
        (h for h in headings or () if INDEX_TITLE.fullmatch(str(h).strip())), None)
    if not title_text:
        return None
    position = _title_position(title_text, text, lines)
    if position is None or position > INDEX_TOP_FRACTION:
        return None
    # The entries after the title (the whole page when the title is only a section header).
    after = (text or "")[title.end():] if title else (text or "")
    numbered = sum(1 for line in after.splitlines() if _entry(line.strip()))
    if numbered < INDEX_MIN_PAGE_LINES:
        return None
    return f"'{title_text}' in the top third ({position:.0%} down) and {numbered} lines with page numbers or dates"


def index_entries(text: str) -> int:
    """Lines that read as contents entries (a page number or a date), for a contents page's
    continuation (stage._classify, 2026-11-15)."""
    return sum(1 for line in (text or "").splitlines()
               if PAGE_NUMBER_LINE.search(line.strip()) or INDEX_ENTRY_DATE.search(line))


# 2026-11-08 (user): the short page rules (stage._classify) read these.
END_OF_REPORT = re.compile(r"\bend\s+of\s+(?:the\s+)?report\b", re.IGNORECASE)
CREDENTIAL = re.compile(
    r"(?<![A-Za-z])(?:M\.?D|D\.?O|N\.?P|PA-?C|P\.?A|FNP(?:-[BC]{1,2})?|APRN|ARNP|DNP|DPM|DDS|DMD|O\.?D|CNM|CRNA|"
    r"MBBS|LCSW|LPC|PhD|PsyD|PharmD|R\.?N)\.?(?![A-Za-z])")


def provider_or_date(text: str, provider: bool, signer: str = "") -> Optional[str]:
    """What on a page carries a signature on from the page before: the provider's name /
    credentials (kv_extract), the signer's name (kv_extract, on the page before), or credentials
    or a date in the text."""
    if provider:
        return "provider name / credentials (kv_extract)"
    names = [w for w in re.findall(r"[A-Za-z]{3,}", signer or "") if w.casefold() not in {"electronically", "signed"}]
    if names and all(re.search(rf"(?<![A-Za-z]){re.escape(w)}(?![A-Za-z])", text or "", re.IGNORECASE) for w in names):
        return "the signer's name"
    cred = CREDENTIAL.search(text or "")
    if cred:
        return f"credentials '{cred.group(0)}'"
    date = ANY_DATE.search(text or "")
    if date:
        return f"date '{date.group(0)}'"
    return None


ANY_DATE = re.compile(
    r"(?<!\d)(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{1,2}-\d{1,2})(?!\d)"
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}\b",
    re.IGNORECASE)


# 2026-11-14 (user): every Junk / Invoice page is Main (Invoice / Invoice Page) -- the money
# amount + Invoice / Due Date check is gone -- except a terms page. Terms wording: T&C, Terms and
# Conditions, Payment Terms and the like, and Payment Instructions.
TERMS_PAGE = re.compile(
    r"(?<![A-Za-z])(?:"
    r"T\s*(?:&|&amp;)\s*Cs?|T\s+and\s+Cs?"
    r"|terms\s*(?:and|&|&amp;)\s*conditions?"
    r"|conditions\s*(?:and|&|&amp;)\s*terms"
    r"|(?:standard|general)\s+terms"
    r"|payment\s+terms|terms\s+of\s+(?:payment|sale|service|use|business)"
    r"|payment\s+instructions?"
    r")(?![A-Za-z])",
    re.IGNORECASE)

# What makes the page an invoice, for the order check: the word Invoice / Bill To / Amount Due /
# Balance Due / Total, or a dollar amount.
INVOICE_MARK_TEXT = re.compile(
    r"(?<![A-Za-z])(?:invoice|bill\s+to|amount\s+due|balance\s+due|sub\s*-?\s*total|total)(?![A-Za-z])"
    r"|\$\s?\d[\d,]*(?:\.\d{2})?",
    re.IGNORECASE)

# A dollar amount inside a table: a markdown row (| ... | $25.00 |) or an HTML cell.
_DOLLAR = r"\$\s?\d[\d,]*(?:\.\d{2})?"
TABLE_DOLLAR = re.compile(
    rf"^[ \t]*\|[^\n]*\|[^\n]*{_DOLLAR}[^\n]*\|?[ \t]*$"
    rf"|<td[^>]*>[^<]*{_DOLLAR}[^<]*</td>",
    re.IGNORECASE | re.MULTILINE)


def invoice_table(inputs: PageInputs) -> Optional[str]:
    """A dollar amount in a table of Final OCR 1 (Docling), else of the Final OCR text: the
    amount, or None (2026-11-14, user: a table of dollar amounts is an invoice)."""
    for text in (inputs.final1_text, inputs.text):
        hit = TABLE_DOLLAR.search(text or "")
        if hit:
            amount = re.search(_DOLLAR, hit.group(0))
            return amount.group(0) if amount else hit.group(0).strip()
    return None


def terms_page(inputs: PageInputs) -> Optional[str]:
    """The terms wording on a Junk / Invoice page that comes BEFORE anything invoice-like, or
    None (2026-11-14, user: terms or payment instructions before the invoice = a terms page;
    after it, e.g. "Please refer to the Payment Terms & Conditions on next page", an invoice).
    Final OCR text first, else Tesseract's."""
    for text in (inputs.text or "", inputs.prelim_text or ""):
        hit = TERMS_PAGE.search(text)
        if hit is None:
            continue
        mark = INVOICE_MARK_TEXT.search(text)
        if mark is None or hit.start() < mark.start():
            return " ".join(hit.group(0).split())
    return None


CARD_WORD = re.compile(r"(?<![A-Za-z])cards?(?![A-Za-z])", re.IGNORECASE)

# 2026-11-15 (user): a care provider's own page (a surgery center's billing letter) the model
# filed as an invoice, with no invoice wording on it: Main, and page classification decides.
FACILITY_WORDS = re.compile(
    r"(?<![A-Za-z])(?:surg(?:ery|ical)\s+cent(?:er|re)|ambulatory\s+surgery|hospital|clinics?"
    r"|medical\s+cent(?:er|re)|health\s+cent(?:er|re))(?![A-Za-z])",
    re.IGNORECASE)


def _invoice_wording(inputs: PageInputs) -> bool:
    return any(INVOICE_MARK_TEXT.search(text or "") for text in (inputs.text, inputs.prelim_text))


def _keep_invoice(verdict: Verdict, inputs: PageInputs) -> Verdict:
    """There is no Junk / Invoice (2026-11-07, user). An invoice page is Main (page_classify:
    Invoice / Invoice Page), unless it is a terms page -> Junk / Others (2026-11-14, user):
      1. a table with a dollar amount (Final OCR 1 / Docling) -> Invoice; terms can't overrule it;
      2. terms wording / Payment Instructions before the invoice wording, or with none -> Junk / Others;
      3. no invoice wording (Invoice, Bill To, Amount / Balance Due, Total, a $ amount) but a care
         provider's name (Surgery Center, Hospital, Clinic, Medical Center ...) -> Main, NOT an
         invoice: page classification decides its type (2026-11-15, user);
      4. otherwise (invoice wording, terms after it or none) -> Invoice.
    A card the model called an invoice is already Junk / Cards (decide)."""
    if verdict.code != CODE_INVOICE:
        return verdict
    table = invoice_table(inputs)
    terms = None if table is not None else terms_page(inputs)
    if terms is not None:
        verdict.votes["invoice_not_kept"] = f"terms page ('{terms}' before the invoice)"
        # The terms rule moved the model's Invoice to Junk / Others: flipped (2026-11-14).
        return Verdict(CODE_OTHERS, conf.flipped(), verdict.rule,
                       f"{verdict.reason}; terms page ('{terms}' before the invoice) -> Junk / Others", verdict.votes,
                       strength=verdict.strength)
    facility = None if table is not None or _invoice_wording(inputs) else FACILITY_WORDS.search(
        f"{inputs.text or ''}\n{inputs.prelim_text or ''}")
    if facility is not None:
        word = " ".join(facility.group(0).split())
        verdict.votes["invoice_unjunked"] = f"no invoice wording; '{word}' on the page"
        # Against the model's Invoice: flipped.
        return Verdict(CODE_MAIN, conf.flipped(), verdict.rule,
                       f"{verdict.reason}; no invoice wording, '{word}' on the page -> Main (page classification "
                       f"decides)", verdict.votes, strength=verdict.strength)
    if table is not None:
        verdict.votes["invoice_table"] = table
    verdict.votes["invoice_kept"] = verdict.reason
    verdict.votes["rule"] = INVOICE_RULE
    p_keep, p_junk, p_blank = _model_probs(inputs)
    why = f"table with {table}; " if table is not None else ""
    # Scored as the invoice call it is (p(junk)), not as a keep the model disagrees with.
    return Verdict(CODE_MAIN, rule_confidence(CODE_INVOICE, INVOICE_RULE, p_keep, p_junk, p_blank), INVOICE_RULE,
                   f"{verdict.reason}; {why}{INVOICE_MARK} -> Main (Page Type {INVOICE_PAGE_TYPE} / "
                   f"{INVOICE_PAGE_SUBTYPE})", verdict.votes,
                   strength=rule_strength(CODE_INVOICE, INVOICE_RULE, p_keep, p_junk))


def lab_result_words(inputs: PageInputs) -> Optional[list[str]]:
    """2026-11-04 (user): the words that make a page a lab result -- one term of every group
    in canon ``lab_keep.all_of`` (Lab, Reference Range, Value) -- or None."""
    cfg = canon().raw.get("lab_keep") or {}
    if not cfg.get("enabled", True):
        return None
    text = f"{inputs.text or ''}\n{inputs.prelim_text or ''}"
    found: list[str] = []
    for group in cfg.get("all_of") or []:
        pattern = _terms(group)
        hit = pattern.search(text) if pattern is not None else None
        if hit is None:
            return None
        found.append(hit.group(0))
    return found or None


def _keep_lab(verdict: Verdict, inputs: PageInputs) -> Verdict:
    """The last rule, both passes: a junk page with Lab, Reference Range and Value on it is a lab
    result -> Main. Blank stays Blank."""
    if verdict.code in (CODE_MAIN, CODE_BLANK):
        return verdict
    found = lab_result_words(inputs)
    if not found:
        return verdict
    verdict.votes["lab_kept"] = verdict.reason
    verdict.votes["rule"] = "LAB"
    p_keep, p_junk, p_blank = _model_probs(inputs)
    return Verdict(CODE_MAIN, rule_confidence(CODE_MAIN, "LAB", p_keep, p_junk, p_blank), "LAB",
                   f"{verdict.reason}; lab result ({' / '.join(found)}) -> Main (never junk)", verdict.votes,
                   strength=rule_strength(CODE_MAIN, "LAB", p_keep, p_junk))


