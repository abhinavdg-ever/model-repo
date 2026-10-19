"""Blank / junk decision: rules R1-R8, then two voters (docs/BLANK_JUNK_LOGIC.md).

Inputs per page (``PageInputs``): the OCR text, its word boxes when the pass has them,
DistilBERT's raw probabilities (model_bridge.model_scores), the page type and
handwritten share from the page-tag model, and Provider Signature from KV extraction.

The ladder, first rule that fires decides (R4, R5 and R3's KV half need pass 2):

  R1 blank / tiny   no ink -> Blank. Not Visual: 0 characters, or the word "blank" in
                    < 30 characters -> Blank; else < 15 characters -> Junk / Cover Page
                    when a Cover Page term is there, Junk / Others otherwise
  R2 placeholder    "intentionally left blank" on a page of <= 25 words -> Junk / Others
  R3 signature      Provider Signature = Yes, or a canon signature phrase -> Main, unless
                    both voters say JUNK (JJ): then the vote (R9) decides
  R4 DistilBERT     p(junk) > 0.85 -> Junk, DistilBERT's subtype, unless the canon finds
                    clinical (C) or demographic (D) evidence: then the vote decides
  R5 short page     content ends in the top 15% of the page height, under 0.5% ink below
                    (17%-94%) -> Junk / Others, unless both voters say KEEP (KK) and
                    DistilBERT's p(keep) > 0.8
  R6 retrieval      record_request + roi_vendors >= 7 and not C+ -> Junk / Record Request
  R7 strong clinical  C+ -> Main
  R8 Visual         document_type 'visual' with < 20 words -> Main, unless retrieval /
                    invoice / ID-card wording >= 7 (a visual page with more text is not Visual)
  R9 vote           DistilBERT and the keyword canon (BLANK_JUNK_LOGIC §5)

Pass 1 only lets R1, R2, R6, R8 and two strong JUNK ballots decide; every other page stays
Main and is judged again in pass 2 on the final OCR.

Every page gets one verdict. Its confidence is the evidence for that verdict (BLANK_JUNK_LOGIC §6):
DistilBERT's probability, the keyword score and, for R5-R8, the rule's own value. Signals
that agree reinforce each other; signals that disagree are averaged, so a contested call
comes out low on its own. R1-R3 are observations and keep a fixed confidence.

The words, weights, thresholds and geometry live in
``keyword-canon/blank_junk_context_canon.json`` (reloads on change).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from classify import (
    CODE_BLANK,
    CODE_COVER_PAGE,
    CODE_INSTRUCTIONS,
    CODE_INVOICE,
    CODE_LETTER_FAX,
    CODE_MAIN,
    CODE_OTHERS,
    CODE_RECORD_REQUEST,
)
from stages.lib.canon_store import CANON_DIR, CanonFile

KEEP, JUNK, ABSTAIN = "KEEP", "JUNK", "ABSTAIN"
STRONG, WEAK = "strong", "weak"

# Subtype names in the canon and in the schema -> classify.py codes.
SUBTYPE_CODE = {
    "Invoice": CODE_INVOICE,
    "Cover Page": CODE_COVER_PAGE,
    "Record Request/Transmittal": CODE_RECORD_REQUEST,
    "Instructions": CODE_INSTRUCTIONS,
    "Letter/Fax": CODE_LETTER_FAX,
    "Others": CODE_OTHERS,
}
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
# id_card: an insurance / ID card or driver's licence prints a name and DOB, which must
# not rescue it as a face sheet.
HARD_JUNK_GROUPS = ("record_request", "roi_vendors", "invoice", "id_card")

# The two voters, in the order their letters are written in the vote
# ("R9 vote JA (DistilBERT, keywords)"): K = Keep, J = Junk, A = Abstain.
VOTER_NAMES = {"bert": "DistilBERT", "kw": "keywords"}

# R1-R3 are observations: their confidence is fixed. R5-R8 are judgements: their value is
# one signal, combined with DistilBERT and the keywords (_confidence).
FIXED_CONFIDENCE = {"R1": 1.0, "R2": 0.99, "R3": 0.95}
RULE_PRIOR = {"R5": 0.95, "R6": 0.97, "R7": 0.95, "R8": 0.90}
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


@dataclass(frozen=True)
class Canon:
    raw: dict[str, Any]
    groups: dict[tuple[str, str], Group]
    # Per context: one alternation of every plain term (longest first), one of the
    # line-start terms, and the regex patterns. Matched text -> (group, term).
    term_re: dict[str, Optional[re.Pattern[str]]]
    line_re: dict[str, Optional[re.Pattern[str]]]
    term_of: dict[str, dict[str, tuple[str, str]]]
    patterns: dict[str, list[tuple[str, re.Pattern[str]]]]
    signature_terms: frozenset[str]
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
            )
            for term in spec.get("terms") or []:
                term_of[context][_norm(term)] = (name, term)
                (starts if spec.get("line_start") else plain).append(term)
            for pattern in spec.get("patterns") or []:
                patterns[context].append((name, re.compile(pattern, re.IGNORECASE | re.MULTILINE)))
        term_re[context] = _alternation(plain, line_start=False)
        line_re[context] = _alternation(starts, line_start=True)
    signature = data["contexts"].get("clinical", {}).get("signature", {})
    return Canon(
        raw=data,
        groups=groups,
        term_re=term_re,
        line_re=line_re,
        term_of=term_of,
        patterns=patterns,
        signature_terms=frozenset(_norm(t) for t in signature.get("terms") or []),
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
    signature_phrase: bool = False
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


def score_text(text: str, lines: Optional[list[Line]] = None, also_junk: Optional[str] = None) -> Scores:
    """Weight x unique hits per group, capped; summed per context (BLANK_JUNK_LOGIC §2.1).

    With ``lines`` (pass 2), a junk term in the header or footer band counts only when the
    body has a junk hit too.

    ``also_junk`` (pass 2: the page's Tesseract text) adds the junk terms the final OCR
    missed, such as a vendor name in a logo or banner ('Optum'), as body hits. Clinical
    and demographic scores read ``text`` alone.
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
            score = min(group.weight * len(terms), group.cap)
            out.groups[f"{context}.{name}"] = score
            out.hits[f"{context}.{name}"] = sorted(terms)
            total += score
        setattr(out, context, total)
    out.signature_phrase = any(
        _norm(term) in prof.signature_terms for term in out.hits.get("clinical.signature", [])
    )
    return out


# --- voters --------------------------------------------------------------------------


@dataclass
class Ballot:
    vote: str  # KEEP / JUNK / ABSTAIN
    strength: str = ""
    p_junk: Optional[float] = None
    subtype: str = ""
    note: str = ""

    def letter(self) -> str:
        return {KEEP: "K", JUNK: "J"}.get(self.vote, "A")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"vote": self.vote}
        if self.strength:
            out["strength"] = self.strength
        if self.p_junk is not None:
            out["p_junk"] = round(self.p_junk, 4)
        if self.subtype:
            out["subtype"] = self.subtype
        if self.note:
            out["note"] = self.note
        return out


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


def bert_ballot(model: Optional[dict[str, Any]], words: int) -> Ballot:
    if not model:
        return Ballot(ABSTAIN, note="unavailable")
    cfg = canon().raw["voters"]["bert"]
    p_keep, p_junk = float(model["p_keep"]), float(model["p_junk"])
    subtype = MODEL_SUBTYPE.get(model.get("top_junk_label") or "", "Others")
    if words < int(cfg["min_words"]):
        return Ballot(ABSTAIN, p_junk=p_junk, note=f"< {cfg['min_words']} words")
    if p_junk >= float(cfg["junk_strong"]):
        return Ballot(JUNK, STRONG, p_junk, subtype)
    if p_junk >= float(cfg["junk_weak"]):
        return Ballot(JUNK, WEAK, p_junk, subtype)
    if p_keep >= float(cfg["keep_strong"]):
        return Ballot(KEEP, STRONG, p_junk)
    if p_keep >= float(cfg["keep_weak"]):
        return Ballot(KEEP, WEAK, p_junk)
    return Ballot(ABSTAIN, p_junk=p_junk)


def keyword_ballot(scores: Scores) -> Ballot:
    """BLANK_JUNK_LOGIC §5.1. Abstains when the canon finds nothing."""
    t = canon().thresholds
    c_hit, c_strong = scores.clinical >= t["clinical"], scores.clinical >= t["clinical_strong"]
    j_hit, j_strong = scores.junk >= t["junk"], scores.junk >= t["junk_strong"]
    d_hit = scores.demographic >= t["demographic"]
    p_junk = min(1.0, scores.junk / 10.0)
    if c_strong:
        return Ballot(KEEP, STRONG, p_junk)
    if c_hit:
        return Ballot(KEEP, WEAK, p_junk)
    if j_hit:
        best = scores.best_junk_group() or ""
        if d_hit and best not in HARD_JUNK_GROUPS:
            return Ballot(KEEP, WEAK, p_junk, note="demographic")
        subtype = canon().groups[("junk", best)].subtype if best else "Others"
        return Ballot(JUNK, STRONG if j_strong else WEAK, p_junk, subtype)
    if d_hit:
        return Ballot(KEEP, WEAK, p_junk)
    return Ballot(ABSTAIN, p_junk=p_junk)


# --- the decision --------------------------------------------------------------------


@dataclass
class PageInputs:
    text: str
    pass_no: int
    lines: Optional[list[Line]] = None  # pass 2 word boxes, as lines
    bert: Optional[dict[str, Any]] = None  # {"p_keep", "p_junk", "top_junk_label"}
    document_type: str = ""  # page-tag model: printed / handwritten / form / visual / ...
    page_tags: bool = False  # the page-tag model wrote document_type
    hw_area_pct: Optional[float] = None
    signed: bool = False  # Provider Signature = Yes (KV extraction)
    signature_known: bool = False  # the KV signature output was there to read
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


def _signals(junk: bool, inputs: PageInputs, scores: Scores, ballots: dict[str, Ballot], rule: str) -> dict[str, float]:
    """Each signal's probability that the verdict (junk or not) is right (BLANK_JUNK_LOGIC §6)."""
    out: dict[str, float] = {}
    if rule in RULE_PRIOR:
        out["rule"] = RULE_PRIOR[rule]
    # DistilBERT counts when it scored the page. Under min_words it is not trusted, except
    # by R4, which is DistilBERT's own call.
    if inputs.bert and (rule == "R4" or ballots["bert"].note == ""):
        out["bert"] = float(inputs.bert["p_junk"] if junk else inputs.bert["p_keep"])
    kw = ballots["kw"]
    if kw.vote == JUNK:
        strength = min(1.0, scores.junk / 10.0)
    elif kw.vote == KEEP:
        strength = min(1.0, max(scores.clinical, scores.demographic) / 10.0)
    else:
        return out
    out["keywords"] = strength if (kw.vote == JUNK) == junk else 1.0 - strength
    return out


def _confidence(
    junk: bool, inputs: PageInputs, scores: Scores, ballots: dict[str, Ballot], votes: dict[str, Any], rule: str,
) -> float:
    """Agreeing signals reinforce (1 - product of their doubts); a disagreement is averaged.

    The keywords agree when their ballot points the verdict's way; the others when their
    probability is at least 0.5. No signal at all is 0.5.
    """
    signals = _signals(junk, inputs, scores, ballots, rule)
    kw = ballots["kw"]
    agree = all(
        (kw.vote == (JUNK if junk else KEEP)) if name == "keywords" else p >= 0.5
        for name, p in signals.items()
    )
    if not signals:
        confidence = 0.5
    elif agree:
        doubt = 1.0
        for p in signals.values():
            doubt *= 1.0 - p
        confidence = 1.0 - doubt
    else:
        confidence = sum(signals.values()) / len(signals)
    votes["confidence_from"] = {
        **{name: round(p, 4) for name, p in signals.items()},
        "combined": "agree" if agree else "disagree",
    }
    return round(confidence, 4)


def _vote(inputs: PageInputs, scores: Scores, ballots: dict[str, Ballot], votes: dict[str, Any]) -> Verdict:
    """R9: the two-voter table (BLANK_JUNK_LOGIC §5.2), then the handwritten veto (§5.3)."""
    # One letter per voter, always in VOTER_NAMES order (DistilBERT, keywords),
    # so "JA" says who cast which ballot.
    letters = "".join(ballots[name].letter() for name in VOTER_NAMES)
    who = ", ".join(VOTER_NAMES.values())
    bert, kw = ballots["bert"], ballots["kw"]
    alone_min = float(canon().raw["voters"]["keyword"]["junk_alone_min"])

    if bert.vote == JUNK and kw.vote == JUNK:
        # Both say junk: Junk, whatever the strength (pass 1 still waits for two strong).
        row, verdict = 1, JUNK
    elif bert.vote == JUNK and kw.vote == ABSTAIN:
        row, verdict = 2, JUNK
    elif bert.vote == ABSTAIN and kw.vote == JUNK:
        row, verdict = 3, JUNK if scores.junk >= alone_min else KEEP
    elif JUNK in (bert.vote, kw.vote):
        row, verdict = 4, KEEP
    elif bert.vote == KEEP and kw.vote == KEEP:
        row, verdict = 5, KEEP
    elif KEEP in (bert.vote, kw.vote):
        row, verdict = 6, KEEP
    else:
        row, verdict = 7, KEEP

    if inputs.pass_no == 1:
        # Pass 1 only takes two strong JUNK ballots; anything else waits for final OCR.
        if not (row == 1 and bert.strength == kw.strength == STRONG):
            verdict = KEEP
    votes.update(ballots=letters, row=row, by_voter={name: b.letter() for name, b in ballots.items()})

    # The one veto: a mostly handwritten page. Clinical (C) and demographic (D without hard
    # junk wording) need none: the keyword ballot is KEEP on them, and a vote with a K in it
    # is never Junk.
    veto = ""
    if verdict == JUNK:
        hw_limit = float(canon().raw["handwritten_veto"]["min_handwritten_area_pct"])
        if inputs.hw_area_pct is not None and inputs.hw_area_pct > hw_limit:
            veto = "handwritten"
        if veto:
            verdict = KEEP
            votes["veto"] = veto

    if verdict == KEEP:
        confidence = _confidence(False, inputs, scores, ballots, votes, "R9")
        reason = f"R9 vote {letters} ({who}) row {row} -> Main" + (f" (veto: {veto})" if veto else "")
        if inputs.pass_no == 1 and JUNK in (bert.vote, kw.vote):
            reason += " (pass 1: deferred to final OCR)"
        return Verdict(CODE_MAIN, confidence, "R9", reason, votes)

    if kw.vote == JUNK:
        subtype = kw.subtype
    elif bert.vote == JUNK:
        subtype = bert.subtype
    else:
        subtype = "Others"
    confidence = _confidence(True, inputs, scores, ballots, votes, "R9")
    return Verdict(_junk_code(subtype), confidence, "R9", f"R9 vote {letters} ({who}) row {row} -> Junk / {subtype}", votes)


def decide(inputs: PageInputs) -> Verdict:
    """The page's verdict: rules R1-R8 in order, else the vote (R9)."""
    prof = canon()
    t = prof.thresholds
    text = inputs.text or ""
    words = _word_count(text)
    pass2 = inputs.pass_no == 2
    lines = inputs.lines if pass2 else None
    scores = score_text(text, lines, inputs.prelim_text if pass2 else None)
    visual = is_visual(inputs.document_type, words)
    if inputs.document_type == "visual" and not visual:
        votes_visual_note = f"document_type visual, but {words} words >= {visual_max_words()}: not Visual"
    else:
        votes_visual_note = None
    ballots = {
        "bert": bert_ballot(inputs.bert, words),
        "kw": keyword_ballot(scores),
    }
    votes: dict[str, Any] = {
        "pass": inputs.pass_no,
        "bert": ballots["bert"].to_dict(),
        "kw": {**ballots["kw"].to_dict(), **scores.to_dict()},
        "visual_source": "page_tags" if inputs.page_tags else "none",
        "signature_source": (
            "kv_extract" if inputs.signature_known else "canon_only"
        ) if pass2 else "not_in_pass_1",
        "hw_area": inputs.hw_area_pct if inputs.hw_area_pct is not None else "none",
    }
    if votes_visual_note:
        votes["visual"] = votes_visual_note
    if inputs.bert:
        votes["bert"]["label"] = inputs.bert.get("top_junk_label") or ""

    def ruled(code: int, rule: str, reason: str) -> Verdict:
        votes["rule"] = rule
        if rule in FIXED_CONFIDENCE:
            confidence = FIXED_CONFIDENCE[rule]
        else:
            confidence = _confidence(code != CODE_MAIN, inputs, scores, ballots, votes, rule)
        return Verdict(code, confidence, rule, reason, votes)

    # R1 blank, or a tiny page that is junk
    chars = len(re.sub(r"\s", "", text))
    if inputs.no_ink:
        return ruled(CODE_BLANK, "R1", "R1 blank: no ink")
    if not visual and chars == 0:
        return ruled(CODE_BLANK, "R1", "R1 blank: no text")
    if not visual and chars < BLANK_WORD_MAX_CHARS and BLANK_WORD.search(text):
        return ruled(CODE_BLANK, "R1", f"R1 blank: the word 'blank' in < {BLANK_WORD_MAX_CHARS} characters")
    if not visual and chars < MIN_TEXT_CHARS:
        cover = sum(scores.group("junk", g.name) for g in prof.groups.values() if g.context == "junk" and g.subtype == "Cover Page")
        subtype = "Cover Page" if cover > 0 else "Others"
        return ruled(_junk_code(subtype), "R1", f"R1 tiny page: < {MIN_TEXT_CHARS} characters -> Junk / {subtype}")
    # R2 placeholder
    if scores.group("junk", "placeholder") > 0 and words <= PLACEHOLDER_MAX_WORDS:
        return ruled(CODE_OTHERS, "R2", "R2 placeholder ('intentionally left blank') -> Junk / Others")
    # R3 signature (Provider Signature from KV extraction exists only from pass 2). Not when
    # both voters say JUNK (JJ): a signed ROI / transmittal letter is still junk. The vote then
    # decides, with its handwritten veto.
    if pass2 and (inputs.signed or scores.signature_phrase):
        if all(b.vote == JUNK for b in ballots.values()):
            votes["r3_skipped"] = "JJ"
            votes["rule"] = "R9"
            return _vote(inputs, scores, ballots, votes)
        why = "Provider Signature = Yes" if inputs.signed else "signature phrase"
        return ruled(CODE_MAIN, "R3", f"R3 signature ({why}) -> Main")
    # R4 DistilBERT force. A face sheet or a clinical page DistilBERT is sure about is
    # left to the vote, where the keyword ballot is KEEP on them.
    force = float(prof.raw["voters"]["bert"]["force_junk_above"])
    protected = scores.clinical >= t["clinical"] or scores.demographic >= t["demographic"]
    if pass2 and inputs.bert and float(inputs.bert["p_junk"]) > force and protected:
        votes["r4_skipped"] = "clinical" if scores.clinical >= t["clinical"] else "demographic"
    elif pass2 and inputs.bert and float(inputs.bert["p_junk"]) > force:
        subtype = ballots["bert"].subtype or MODEL_SUBTYPE.get(inputs.bert.get("top_junk_label") or "", "Others")
        return ruled(
            _junk_code(subtype), "R4", f"R4 DistilBERT p(junk) {float(inputs.bert['p_junk']):.3f} > {force} -> Junk / {subtype}",
        )
    # R5 short page
    if pass2:
        short = prof.raw["short_page"]
        if not lines:
            votes["short_page"] = "no_geometry"
        else:
            bottom = content_bottom(lines)
            votes["short_page"] = {"content_bottom": round(bottom, 3) if bottom is not None else None}
            if inputs.body_ink is not None:
                # Why R5 did or did not fire: the ink found below the band, against the limit.
                votes["short_page"].update(
                    ink=inputs.body_ink,
                    ink_fraction=inputs.body_ink_fraction,
                    ink_limit=float(short["body_ink_max_fraction"]),
                )
            if inputs.body_ink is None:
                votes["short_page"]["ink"] = "not_measured"
            elif bottom is not None and bottom <= float(short["top_fraction"]) and inputs.body_ink is False:
                # Both voters KEEP (KK) with DistilBERT sure of it undo the short page;
                # one KEEP, or a hesitant DistilBERT, does not.
                undo_above = float(short.get("undo_bert_keep_above", 0.8))
                p_keep = float(inputs.bert["p_keep"]) if inputs.bert else 0.0
                if all(b.vote == KEEP for b in ballots.values()) and p_keep > undo_above:
                    votes["short_page"]["skipped"] = f"KK, DistilBERT p(keep) {p_keep:.2f} > {undo_above}"
                else:
                    return ruled(CODE_OTHERS, "R5", f"R5 short page: content ends at {bottom:.0%} of the height -> Junk / Others")
    # R6 retrieval counter-veto
    retrieval = sum(scores.group("junk", g) for g in RETRIEVAL_GROUPS)
    if retrieval >= t["junk_strong"] and scores.clinical < t["clinical_strong"]:
        return ruled(CODE_RECORD_REQUEST, "R6", f"R6 retrieval wording {retrieval:g} -> Junk / Record Request/Transmittal")
    # R7 strong clinical
    if scores.clinical >= t["clinical_strong"]:
        return ruled(CODE_MAIN, "R7", f"R7 strong clinical ({scores.clinical:g}) -> Main")
    # R8 Visual
    if visual:
        hard = {g: scores.group("junk", g) for g in HARD_JUNK_GROUPS}
        best = max(hard, key=hard.get)
        if sum(hard.values()) >= float(prof.raw["visual"]["veto_min_score"]):
            subtype = prof.groups[("junk", best)].subtype
            return ruled(_junk_code(subtype), "R8", f"R8 Visual with retrieval / invoice wording -> Junk / {subtype}")
        return ruled(CODE_MAIN, "R8", "R8 Visual -> Main")
    votes["rule"] = "R9"
    return _vote(inputs, scores, ballots, votes)
