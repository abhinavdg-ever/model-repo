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
  R3C card          pass 2, page-tag printed or visual: a STRONG ID or insurance card by
                    card_rules.detect (3+ card groups, no form / letter / clinical words,
                    <= 300 words) -> Junk / Cards (2026-10-31, user). Not undone by
                    post_process. A medium card goes on down the ladder, flagged for review.
                    In pass 1 a card is never settled: Main for now, judged in pass 2
  R3 Visual         document_type 'visual': card evidence (an id_card term or pattern;
                    vendor / insurer / retrieval / invoice wording adds weight, total >= 4)
                    -> Junk / Others; else < 20 words -> Main; else on
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
    R5 DistilBERT   p(junk) >= 0.85 -> Junk (DistilBERT's subtype); 0.50 - 0.85 -> Junk for
                    review; below 0.50 -> Main
    handwritten     a page more than 30% handwritten is never junked by R5: Main for review

After the ladder (``post_process``, 2026-10-26, user):
  content kept     pass 2: a page with an Electronic Signature or provider details
                   (name / credentials) extracted by kv_extract is never junk -> Main
                   (a DOS alone no longer keeps a page: 2026-10-26, user)

So junk is decided in pass 1 by R1-R3 and DistilBERT's sure calls (p(junk) > 0.95); the rest
of its junk calls wait for the Final OCR.
Confidence: R1 1.0, R2 0.99, R3 0.90, R4 0.95, R6 0.95; R5 (and the pass-1 keep) DistilBERT's
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
from stages._support import FINAL_JUNK
from stages.lib import confidence as conf
from stages.lib.canon_store import CANON_DIR, CanonFile


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

# Page-tag types the card rule R3C reads (2026-10-31, user).
CARD_DOCUMENT_TYPES = frozenset({"printed", "visual"})
# R3C junks a strong card only (2026-10-31, user); a medium one is flagged for review.
CARD_JUNK_CONFIDENCE = "strong"
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

# How strongly each rule backs its verdict (stages/lib/confidence.py). R1-R3 judge a page
# with (almost) no text -- empty, a placeholder, a Visual page -- where DistilBERT has
# nothing to read: their confidence is this strength. R4 and R6 back DistilBERT's
# probability for their verdict; R5 is DistilBERT alone.
RULE_SUPPORT = {"R1": 1.0, "R2": 0.99, "R3": 0.90, "R4": 0.95, "R6": 0.95, "CONTENT": 0.95}
NO_TEXT_RULES = ("R1", "R2", "R3")
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
    footer = float(prof.raw["short_page"]["ignore_footer_band"])
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
    """(p_keep, p_junk, top junk label): p_junk = every JUNK_* label plus BLANK."""
    p_keep = p_junk = 0.0
    best: tuple[float, str] = (-1.0, "")
    for label, p in zip(labels, probs):
        p = max(0.0, float(p))
        if label.startswith("KEEP"):
            p_keep += p
        elif label.startswith("JUNK") or label.startswith("BLANK"):
            p_junk += p
            if label.startswith("JUNK") and p > best[0]:
                best = (p, label)
    return p_keep, p_junk, best[1]


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
        "junk_at": float(cfg.get("junk_at", 0.85)),
        "review_junk_at": float(cfg.get("review_junk_at", 0.50)),
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
    top = float(cfg["top_fraction"])
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
    """A rule's verdict confidence (stages/lib/confidence.py). R1-R3 (no text to read):
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


def decide(inputs: PageInputs) -> Verdict:
    """The page's verdict: R1-R3, then in pass 1 the sure keep, in pass 2 R4, R6 and R5."""
    prof = canon()
    t = _thresholds()
    text = inputs.text or ""
    words = _word_count(text)
    pass2 = inputs.pass_no == 2
    lines = inputs.lines if pass2 else None
    # Read only for R1's cover term, R2's placeholder and R3's card wording; nothing is voted.
    scores = score_text(text, lines, inputs.prelim_text if pass2 else None, inputs.document_type)
    visual = is_visual(inputs.document_type, words)
    bert = inputs.bert
    p_junk = float(bert["p_junk"]) if bert else None
    p_keep = float(bert["p_keep"]) if bert else None
    subtype = MODEL_SUBTYPE.get((bert or {}).get("top_junk_label") or "", "Others")
    votes: dict[str, Any] = {
        "pass": inputs.pass_no,
        "bert": (
            {"p_keep": round(p_keep, 4), "p_junk": round(p_junk, 4), "label": (bert or {}).get("top_junk_label") or ""}
            if bert else {"note": "unavailable"}
        ),
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

    # R1 blank, or a tiny page that is junk
    chars = len(re.sub(r"\s", "", text))
    # For blank_junk.json (2026-10-23): no ink or no text, and the ID / insurance-card words
    # found (counted on Visual pages only, the R3 card evidence).
    votes["empty"] = bool(inputs.no_ink or (not visual and chars == 0))
    votes["card_keywords"] = scores.hits.get(f"junk.{prof.raw['visual']['card_group']}", [])
    if inputs.no_ink:
        return ruled(CODE_BLANK, "R1", "R1 blank: no ink")
    if not visual and chars == 0:
        return ruled(CODE_BLANK, "R1", "R1 blank: no text")
    if not visual and chars < BLANK_WORD_MAX_CHARS and BLANK_WORD.search(text):
        return ruled(CODE_BLANK, "R1", f"R1 blank: the word 'blank' in < {BLANK_WORD_MAX_CHARS} characters")
    if not visual and chars < MIN_TEXT_CHARS:
        cover = sum(scores.group("junk", g.name) for g in prof.groups.values() if g.context == "junk" and g.subtype == "Cover Page")
        tiny = "Cover Page" if cover > 0 else "Others"
        return ruled(_junk_code(tiny), "R1", f"R1 tiny page: < {MIN_TEXT_CHARS} characters -> Junk / {tiny}")
    # R2 placeholder: a page left blank on purpose is Blank, not junk.
    if scores.group("junk", "placeholder") > 0 and words <= PLACEHOLDER_MAX_WORDS:
        return ruled(CODE_BLANK, "R2", "R2 placeholder ('intentionally left blank') -> Blank")
    # R3C card (2026-10-31, user): a strong ID or insurance card on a printed or visual page,
    # judged in pass 2 on the Final OCR. In pass 1 a card is never settled either way.
    card = detect_card(text) if inputs.document_type in CARD_DOCUMENT_TYPES else None
    if card is not None and card.is_card:
        votes["card"] = {"type": card.card_type, "confidence": card.confidence, "matched": card.matched}
        if not pass2:
            votes["deferred"] = True
            return ruled(CODE_MAIN, "P1", f"pass 1: {card.card_type} card words -> Main for now, judged in pass 2",
                         conf.score(0.5, "degraded"))
        if card.confidence == CARD_JUNK_CONFIDENCE:
            return ruled(
                CODE_CARDS, "R3C",
                f"R3C card: {card.card_type} ({card.confidence}; {'; '.join(card.matched)}) -> Junk / Cards",
                0.95,
            )
        votes["review_required"] = True
        votes["card_review"] = f"{card.card_type} card ({card.confidence}): not junked, review"
    elif card is not None and card.blocked_by:
        votes["card_near_miss"] = {"type": card.card_type, "blocked_by": card.blocked_by}
    # R3 Visual. A visual page with card evidence (an insurance card, a driver's licence) is
    # junk, whatever its length; without a card a Visual page (< 20 words) is kept; a visual
    # page with more text goes on like any other.
    if inputs.document_type == prof.raw["visual"]["document_type"]:
        cfg = prof.raw["visual"]
        card = scores.group("junk", cfg["card_group"])
        weight = sum(scores.group("junk", g) for g in cfg["veto_junk_groups"])
        if card > 0 and weight >= float(cfg["veto_min_score"]):
            return ruled(CODE_OTHERS, "R3", f"R3 visual with card wording ({weight:g}) -> Junk / Others")
        if visual:
            return ruled(CODE_MAIN, "R3", "R3 Visual -> Main")

    if not pass2:
        # Pass 1 settles only what DistilBERT is sure of: a keep, or a junk page (which
        # pass 2's R4 / R6 / R5 would junk too, as R4 is overruled at the same p(junk)).
        # Everything else waits for the Final OCR and pass 2.
        limit = t["pass1_final_keep_above"]
        if p_keep is not None and p_keep > limit:
            return ruled(
                CODE_MAIN, "R5",
                f"{FINAL_KEEP}: DistilBERT p(keep) {p_keep:.3f} > {limit} -> Main; pass 2 skipped",
                conf.score(p_keep),
            )
        junk_limit = t["pass1_final_junk_above"]
        hw_limit = float(prof.raw["handwritten_veto"]["min_handwritten_area_pct"])
        handwritten = inputs.hw_area_pct is not None and inputs.hw_area_pct > hw_limit
        if p_junk is not None and p_junk > junk_limit and not handwritten:
            return ruled(
                _junk_code(subtype), "R5",
                f"{FINAL_JUNK}: DistilBERT p(junk) {p_junk:.3f} > {junk_limit} -> Junk / {subtype}; "
                f"pass 2 and Final OCR 2 skipped",
                conf.score(p_junk),
            )
        if p_junk is not None and p_junk > junk_limit:
            votes["veto"] = "handwritten"
        votes["deferred"] = True
        why = f"p(keep) {p_keep:.3f} <= {limit}" if p_keep is not None else "no DistilBERT score"
        return ruled(CODE_MAIN, "P1", f"pass 1: {why} -> Main for now, judged in pass 2 on the final OCR",
                     conf.score(p_keep) if p_keep is not None else conf.score(0.5, "degraded"))

    # R4 signature: kv_extract's Electronic Signature only.
    if inputs.signed:
        why = "Electronic Signature"
        over = t["signature_overruled_above"]
        if p_junk is not None and p_junk > over:
            votes["r4_overruled"] = f"{why}, but DistilBERT p(junk) {p_junk:.3f} > {over}"
        else:
            return ruled(CODE_MAIN, "R4", f"R4 signature ({why}) -> Main")

    # R6 short page: only a header / banner at the top and nothing drawn below it.
    short_page = _short_page(inputs, p_keep, votes)
    if short_page:
        return ruled(CODE_OTHERS, "R6", short_page)

    # R5 DistilBERT alone.
    if p_junk is None:
        votes["review_required"] = True
        return ruled(CODE_MAIN, "R5", "R5: DistilBERT unavailable -> Main (review)", conf.score(0.5, "degraded"))
    junk_at, review_at = t["junk_at"], t["review_junk_at"]
    if p_junk < review_at:
        return ruled(CODE_MAIN, "R5", f"R5 DistilBERT p(junk) {p_junk:.3f} < {review_at} -> Main", conf.score(p_keep))
    sure = p_junk >= junk_at
    hw_limit = float(prof.raw["handwritten_veto"]["min_handwritten_area_pct"])
    if inputs.hw_area_pct is not None and inputs.hw_area_pct > hw_limit:
        votes["veto"] = "handwritten"
        votes["review_required"] = True
        return ruled(
            CODE_MAIN, "R5",
            f"R5 DistilBERT p(junk) {p_junk:.3f}, but {inputs.hw_area_pct:g}% handwritten > {hw_limit:g}% -> Main (review)",
            conf.score(p_keep),
        )
    if sure:
        return ruled(_junk_code(subtype), "R5", f"R5 DistilBERT p(junk) {p_junk:.3f} >= {junk_at} -> Junk / {subtype}",
                     conf.score(p_junk))
    votes["review_required"] = True
    return ruled(
        _junk_code(subtype), "R5",
        f"R5 DistilBERT p(junk) {p_junk:.3f} in [{review_at}, {junk_at}) -> Junk / {subtype} (review)",
        conf.under_bar(p_junk, junk_at),
    )


def post_process(verdict: Verdict, inputs: PageInputs) -> Verdict:
    """Rules after the ladder (2026-10-26, user): in pass 2 a page with an Electronic
    Signature or provider details (name / credentials) extracted by kv_extract is never
    junk, whatever rule junked it (R4's p(junk) overrule included). Blank stays Blank.
    A DOS alone does not keep a page (removed 2026-10-26, user: it caught every date)."""
    found = [name for name, hit in (("Provider Signature", inputs.signed),
                                     ("provider details", inputs.provider)) if hit]
    if inputs.pass_no != 2 or not found or verdict.code in (CODE_MAIN, CODE_BLANK):
        return verdict
    if verdict.rule == "R3C":
        return verdict  # a card prints a PCP / provider name: still a card
    verdict.votes["content_kept"] = verdict.reason
    verdict.votes["rule"] = "CONTENT"
    bert = inputs.bert or {}
    p_keep = float(bert["p_keep"]) if bert else None
    p_junk = float(bert["p_junk"]) if bert else None
    reason = f"{' / '.join(found)} extracted on the page -> Main (never junk)"
    return Verdict(CODE_MAIN, rule_confidence(CODE_MAIN, "CONTENT", p_keep, p_junk), "CONTENT",
                   f"{verdict.reason}; {reason}", verdict.votes)
