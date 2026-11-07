"""Blank / junk decision: rules R1-R4, the short page R6, then DistilBERT (docs/BLANK_JUNK_LOGIC.md).

Inputs per page (``PageInputs``): the OCR text, its word boxes when the pass has them,
DistilBERT's probabilities (model_bridge.model_scores), the page type and handwritten
share from the page-tag model, and Provider Signature from KV extraction.

No keyword scoring or voting (removed 2026-10-20): keywords survive only as the fixed
phrases R1 (cover term), R2 and R3 look for. R4 reads only kv_extract's Electronic
Signature (no signature phrases since 2026-10-21). The ladder, first rule that fires decides:

  R1 blank / tiny   no ink -> Blank. Not Visual: 0 characters, or the word "blank" in
                    < 30 characters -> Blank; else < 15 characters -> Junk / Cover Page
                    when a Cover Page term is there, Junk / Others otherwise
  R2 placeholder    "intentionally left blank" on a page of <= 25 words -> Blank
  R3 card           pass 2, page-tag printed or visual: an ID or insurance card by
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
                    p(junk) > 0.95 (a signed records-request letter is still junk)
    R6 short page   the content ends in the top 15% and the band below has < 0.5% ink ->
                    Junk / Others, unless DistilBERT p(keep) > 0.90
    R5 DistilBERT   p(junk) >= 0.70 -> Junk (DistilBERT's subtype); below -> Main (2026-11-03,
                    user: no review band; canon junk_at = review_junk_at = 0.7)
    handwritten     a page more than 30% handwritten is never junked by R5: Main for review

After the ladder (``post_process``, 2026-10-26, user):
  content kept     pass 2: a page with an Electronic Signature or provider details
                   (name / credentials) extracted by kv_extract is never junk -> Main
                   (a DOS alone no longer keeps a page: 2026-10-26, user)

So junk is decided in pass 1 by R1-R3 and DistilBERT's sure calls (p(junk) > 0.95); the rest
of its junk calls wait for the Final OCR.
Confidence: R1 1.0, R2 0.99, R3 0.95, R4 0.95, R6 0.95; R5 (and the pass-1 keep) DistilBERT's
probability for its verdict. Thresholds live in ``keyword-canon/blank_junk_context_canon.json``
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

# Page-tag types the card rule R3 reads (2026-10-31, user).
CARD_DOCUMENT_TYPES = frozenset({"printed", "visual"})
# DistilBERT labels -> subtype.
MODEL_SUBTYPE = {
    "JUNK_INVOICE": "Invoice",
    "JUNK_INSURANCE_ID": "Invoice",
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

# How strongly each rule backs its verdict (modules/confidence.py). R1-R2 judge a page
# with (almost) no text -- empty, a placeholder -- where DistilBERT has
# nothing to read: their confidence is this strength. R4 and R6 back DistilBERT's
# probability for their verdict; R5 is DistilBERT alone.
RULE_SUPPORT = {"R1": 1.0, "R2": 0.99, "R3": 0.95, "R4": 0.95, "R6": 0.95, "CONTENT": 0.95, "FAX": 0.85, "LAB": 0.95, "INVOICE": 0.95}
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
    signature_known: bool = False  # the KV signature output was there to read
    provider: bool = False  # provider details extracted (kv_extract's provider name / credentials); pass 2
    no_ink: bool = False  # the HW stage found no ink (blank_page / faint_marks_only)
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


@dataclass
class Verdict:
    code: int
    confidence: float
    rule: str
    reason: str
    votes: dict[str, Any] = field(default_factory=dict)

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


def rule_confidence(code: int, rule: str, p_keep: Optional[float], p_junk: Optional[float]) -> float:
    """A rule's verdict confidence (modules/confidence.py). R1-R2 (no text to read):
    the rule's strength. R4 / R6: DistilBERT's probability for the verdict (p(keep) for
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


# 2026-11-05 (user): a cover mark on a page of fewer than COVER_MAX_WORDS words is a cover page
# (pass 1 rule 4; was: the page's only words are cover marks, 2026-11-04).
COVER_MARKS = frozenset({"accept", "accepted", "unaccept", "unaccepted", "cover"})
COVER_MAX_WORDS = 5
# Pass 2 rule 4: an explicit signed phrase on the page (not the bare word "signature").
SIGNED_PHRASE = re.compile(
    r"(?<![A-Za-z])(?:electronically\s+signed|digitally\s+signed|signed\s+by|e-?signed|co-?signed)(?![A-Za-z])",
    re.IGNORECASE,
)
# Pass 2 rule 7: the service's JUNK stands only while the raw p(keep) is below this.
JUNK_UNLESS_KEEP_AT = 0.99


def _cover_mark(text: str) -> Optional[str]:
    """The cover mark on a page of fewer than COVER_MAX_WORDS words, else None."""
    if _word_count(text) >= COVER_MAX_WORDS:
        return None
    return next((w for w in re.findall(r"[A-Za-z]+", text or "") if w.casefold() in COVER_MARKS), None)


def decide(inputs: PageInputs) -> Verdict:
    """The page's verdict (2026-11-04, user: the simplified ladder; first rule that fires wins).

    Pass 1 (Tesseract, rules only): Blank (no ink / stage 1 blank / no text / 'blank'),
    'intentionally left blank' -> Blank, a cover mark in < 5 words -> Junk / Cover Page, < 15 characters
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

    def ruled(code: int, rule: str, reason: str, confidence: Optional[float] = None) -> Verdict:
        votes["rule"] = rule
        value = rule_confidence(code, rule, p_keep, p_junk) if confidence is None else confidence
        return Verdict(code, float(value), rule, reason, votes)

    # ---- blank, placeholder, cover marks, tiny: both passes (a handwritten page skips pass 1)
    chars = len(re.sub(r"\s", "", text))
    votes["empty"] = bool(inputs.no_ink or (not visual and chars == 0))
    votes["card_keywords"] = scores.hits.get(f"junk.{prof.raw['visual']['card_group']}", [])
    if inputs.no_ink:
        return ruled(CODE_BLANK, "R1", "R1 blank: no ink")
    if inputs.document_type == "blank":
        return ruled(CODE_BLANK, "R1", "R1 blank: stage 1 page type blank")
    if not visual and chars == 0:
        return ruled(CODE_BLANK, "R1", "R1 blank: no text")
    if not visual and chars < BLANK_WORD_MAX_CHARS and BLANK_WORD.search(text):
        return ruled(CODE_BLANK, "R1", f"R1 blank: the word 'blank' in < {BLANK_WORD_MAX_CHARS} characters")
    if scores.group("junk", "placeholder") > 0 and words <= PLACEHOLDER_MAX_WORDS:
        return ruled(CODE_BLANK, "R2", "R2 placeholder ('intentionally left blank') -> Blank")
    mark = _cover_mark(text)
    if mark is not None:
        return ruled(_junk_code("Cover Page"), "R1",
                     f"R1 cover mark '{mark}' on a page of < {COVER_MAX_WORDS} words -> Junk / Cover Page")
    if not visual and chars < MIN_TEXT_CHARS:
        return ruled(_junk_code("Others"), "R1", f"R1 tiny page: < {MIN_TEXT_CHARS} characters -> Junk / Others")
    if not pass2:
        votes["deferred"] = True
        return ruled(CODE_MAIN, "P1", "pass 1: no rule fired -> Main for now, judged in pass 2 on the final OCR",
                     conf.score(0.5, "degraded"))

    # ---- pass 2
    # 1. card (printed or visual): an ID / insurance card, junked at 5 below.
    card = (detect_card(text, headings=tuple(inputs.header_canon or inputs.headings or ()), text_area=inputs.text_area)
            if inputs.document_type in CARD_DOCUMENT_TYPES else None)
    # 2026-11-05 (user): card or no card (no strong / medium). A card goes through the same keeps
    # as any other junk (more than 30% handwritten, signed, raw p(keep) >= 0.99, then LAB in
    # post_process) before Junk / Cards.
    found_card = None
    if card is not None and card.is_card:
        votes["card"] = {"type": card.card_type, "matched": card.matched}
        found_card = card
    elif card is not None and card.blocked_by:
        votes["card_near_miss"] = {"type": card.card_type, "blocked_by": card.blocked_by}
    # 2. visual and under 20 words: a picture (an ECG strip, an X-ray print).
    if visual and found_card is None:
        return ruled(CODE_MAIN, "R3V", f"R3V visual: under {visual_max_words()} words, not a card -> Main", 0.90)
    # 3. more than 30% handwritten.
    hw_limit = float(prof.raw["handwritten_veto"]["min_handwritten_area_pct"])
    if inputs.hw_area_pct is not None and inputs.hw_area_pct > hw_limit:
        return ruled(CODE_MAIN, "HW", f"HW: {inputs.hw_area_pct:g}% handwritten > {hw_limit:g}% -> Main", 0.90)
    # 4. signed: kv_extract's Electronic Signature or an explicit signed phrase.
    phrase = SIGNED_PHRASE.search(text) or SIGNED_PHRASE.search(inputs.prelim_text or "")
    if inputs.signed or phrase:
        why = "Electronic Signature" if inputs.signed else f"'{phrase.group(0)}' on the page"
        over = t["signature_overruled_above"]
        if p_junk is not None and p_junk > over:
            votes["r4_overruled"] = f"{why}, but raw p(junk) {p_junk:.3f} > {over}"
        else:
            return ruled(CODE_MAIN, "R4", f"R4 signed ({why}) -> Main")
    if found_card is not None:
        found = f"{found_card.card_type} ({'; '.join(found_card.matched)})"
        if p_keep is not None and p_keep >= JUNK_UNLESS_KEEP_AT:
            return ruled(CODE_MAIN, "R3", f"R3 card: {found}, but raw p(keep) {p_keep:.3f} >= {JUNK_UNLESS_KEEP_AT} -> Main",
                         conf.score(p_keep))
        return ruled(CODE_CARDS, "R3", f"R3 card: {found} -> Junk / Cards", 0.95)
    # 6. fax in the section headers and no signature anywhere.
    word = fax_unsigned(inputs)
    if word is not None:
        votes["fax_unsigned"] = word
        return ruled(CODE_LETTER_FAX, "FAX", f"FAX: '{word}' in the section headers and no signature -> Junk / Letter/Fax")
    # 6b. the contained page (stage._classify) decides here, before the classifier: it needs the
    # neighbours, so the stage applies it to every page that reached this point (2026-11-05, user).
    votes["reached_classifier"] = True
    # 7-9. the service's decision.
    if service is None:
        votes["review_required"] = True
        return ruled(CODE_MAIN, "R5", "R5: page classifier unavailable -> Main (review)", conf.score(0.5, "degraded"))
    why = _service_why(service)
    if service.get("review_required"):
        votes["review_required"] = True
    if service["flag"] == "JUNK":
        if p_keep is not None and p_keep >= JUNK_UNLESS_KEEP_AT:
            return ruled(CODE_MAIN, "R5", f"R5 service {why}, but raw p(keep) {p_keep:.3f} >= {JUNK_UNLESS_KEEP_AT} -> Main",
                         conf.score(p_keep))
        subtype = _service_subtype(service)
        return ruled(_junk_code(subtype), "R5", f"R5 service {why} -> Junk / {subtype}",
                     conf.score(float(service["confidence"])))
    if service["flag"] == "BLANK":
        # 2026-11-05 (user): the classifier's Blank is Junk / Others (Blank comes from R1 / R2).
        return ruled(_junk_code("Others"), "R5", f"R5 service {why} -> Junk / Others",
                     conf.score(float(service["confidence"])))
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


def fax_unsigned(inputs: PageInputs) -> Optional[str]:
    """2026-11-02 (user): the fax word the page's header band prints when the page has no
    signature at all (canon ``fax_unsigned``); None otherwise or outside pass 2."""
    cfg = canon().raw.get("fax_unsigned") or {}
    if inputs.pass_no != 2 or not cfg.get("enabled", True) or inputs.signed:
        return None
    fax = _terms(cfg.get("fax_terms") or [])
    signature = _terms(cfg.get("signature_terms") or [])
    if fax is None:
        return None
    # The page's section headers (stage 6), not a band of the page (2026-11-03, user).
    header = "\n".join(inputs.headings)
    found = fax.search(header) if header else None
    if not found:
        return None
    if signature is not None and (signature.search(inputs.text or "") or signature.search(inputs.prelim_text or "")):
        return None
    return found.group(0)


INVOICE_RULE = "INVOICE"


def post_process(verdict: Verdict, inputs: PageInputs) -> Verdict:
    """The processing rules after the ladder, both passes: a junk page with Lab, Reference
    Range and Value on it is a lab result -> Main (LAB, 2026-11-04); a junk page with 2+
    ICD-10 codes or a Social History header -> Main (CLINICAL_CONTENT, 2026-11-06); a Junk /
    Invoice page -> Main, page type Invoice / Invoice Page (INVOICE, 2026-11-05). Blank stays
    Blank."""
    return _keep_invoice(_keep_clinical_content(_keep_lab(verdict, inputs), inputs), inputs)


# 2026-11-06 (user): an ICD-10-CM code as printed ("I11.0", "E11.319", "F03.C0", "Z68.1"):
# a letter, a digit, a digit or letter, a dot, 1-4 more.
ICD10_CODE = re.compile(r"(?<![A-Za-z0-9])[A-TV-Z][0-9][0-9A-Z]\.[0-9A-Z]{1,4}(?![A-Za-z0-9])")
SOCIAL_HISTORY = re.compile(r"(?<![A-Za-z])social\s+history(?![A-Za-z])", re.IGNORECASE)
SOCIAL_HISTORY_LINE = re.compile(r"^\W*social\s+history\W*$", re.IGNORECASE | re.MULTILINE)
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
    bert = inputs.bert or {}
    p_keep = float(bert["p_keep"]) if bert else None
    p_junk = float(bert["p_junk"]) if bert else None
    return Verdict(CODE_MAIN, rule_confidence(CODE_MAIN, CLINICAL_CONTENT_RULE, p_keep, p_junk),
                   CLINICAL_CONTENT_RULE, f"{verdict.reason}; {why} -> Main (never junk)", verdict.votes)


def _keep_invoice(verdict: Verdict, inputs: PageInputs) -> Verdict:
    """A Junk / Invoice page is Main; page_classify gives it Invoice / Invoice Page."""
    if verdict.code != CODE_INVOICE:
        return verdict
    verdict.votes["invoice_kept"] = verdict.reason
    verdict.votes["rule"] = INVOICE_RULE
    bert = inputs.bert or {}
    p_keep = float(bert["p_keep"]) if bert else None
    p_junk = float(bert["p_junk"]) if bert else None
    # Scored as the invoice call it is (p(junk)), not as a keep the model disagrees with.
    return Verdict(CODE_MAIN, rule_confidence(CODE_INVOICE, INVOICE_RULE, p_keep, p_junk), INVOICE_RULE,
                   f"{verdict.reason}; {INVOICE_MARK} -> Main (Page Type {INVOICE_PAGE_TYPE} / "
                   f"{INVOICE_PAGE_SUBTYPE})", verdict.votes)


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
    bert = inputs.bert or {}
    p_keep = float(bert["p_keep"]) if bert else None
    p_junk = float(bert["p_junk"]) if bert else None
    return Verdict(CODE_MAIN, rule_confidence(CODE_MAIN, "LAB", p_keep, p_junk), "LAB",
                   f"{verdict.reason}; lab result ({' / '.join(found)}) -> Main (never junk)", verdict.votes)


