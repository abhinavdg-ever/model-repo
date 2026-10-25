"""Where one document ends and the next begins (docs/APPROACH.md §4).

**Gaps, not pages.** Every gap between two consecutive pages gets a *break score*, a plain
sum of points (``continuity_canon.json`` ``points``: 1 weak, 2 moderate, 3 strong, 5
decisive; + toward a break, - toward joined), from three kinds of evidence:

* **ending** — the earlier page alone (signed in its closing lines, "page N of N", where
  its text stops, "continued");
* **opening** — the later page alone (a title, where its text begins);
* **pair** — the two compared (dates, page numbers running on, page type, sections in
  order, visit number, the same header / footer line, wording across the gap, junk runs).

A page's Break After and the next page's Break Before are that one number. ``score > 0``
breaks, ``< 0`` joins, ``0`` joins and is flagged. A 5-point (decisive) signal settles the
gap unless another 5 opposes it; then a different date wins, at medium confidence, flagged.

**Blank pages, single junk pages and duplicates are transparent**: the gap is scored between
the pages either side (a blank adds +1). A run of two or more junk pages is scored like
content and broken off at both ends, so it becomes its own segment (``Junk J1``).

**The final pass** then looks at whole documents: it splits a document holding two dates
read from a label, and merges neighbours across a *weak* break (same date, an orphan page,
same type with no title, "continued", two junk segments of one kind).

Every page ends up with a tag from its two scores — New / Continue / Last / Standalone —
and every page of a document takes its first page's page type, date of service and
encounter type as Final values (``document_finals``).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from stages.lib.canon_store import CANON_DIR, CanonFile

from .signals import (
    Corpus,
    Semantic,
    clip,
    cut_mid_sentence,
    fill_value,
    fit_corpus,
    furniture,
    head_block,
    normalized_line,
    repeated_line,
    says_continued,
    section_forward,
    section_ranks,
    start_value,
    starts_mid_thought,
    tail_block,
    visit_numbers,
)

# Every signal the engine can produce; the canon's points may name only these.
SIGNALS = (
    "different_dos", "same_dos", "pagination_follows", "signed_in_tail", "signed_anywhere",
    "last_numbered_page", "has_title", "page_type_change", "junk_run", "says_continued",
    "sections_forward", "same_visit_number", "page_fill", "text_start", "same_header_line",
    "wording_char", "same_junk_kind", "blank_between", "same_footer_line", "wording_word",
    "cut_mid_sentence", "starts_mid_thought", "same_page_type", "visit_heading",
)
DECISIVE = frozenset({"different_dos", "same_dos", "pagination_follows", "junk_run"})
# Every decided_by the engine writes (page_continuity_results' CHECK must allow each).
DECIDED_BY = (
    "chart_start", "decisive", "decisive_conflict", "sum", "tie",
    "conflict_a", "conflict_b", "final_pass", "attached",
)


# --- inputs ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Line:
    """One line of page text, lower-cased, with its vertical extent as a fraction of the
    page height when the OCR gave one."""

    text: str
    top: Optional[float] = None
    bottom: Optional[float] = None


@dataclass
class PageInput:
    page_id: Any
    page_name: str = ""
    page_number: Optional[int] = None
    text: str = ""
    lines: list[Line] = field(default_factory=list)
    # Section headings: [{"text", "matched_canonical", "top"}] from the OCR JSON, plus the
    # extraction's accepted section headings ({"text"}).
    section_headers: list[dict[str, Any]] = field(default_factory=list)
    page_type: str = ""  # the family (L1) the pages are compared on
    page_subtype: str = ""
    # Every name of the page, most specific first (axes.names_of): the full type path.
    names: tuple[str, ...] = ()
    page_confidence: Optional[float] = None
    title_hit: bool = False
    dos_from: str = ""
    dos_to: str = ""
    # Points a date found this way is worth (dos_trust): 5 label, 3 edge keyword, 2 keyless.
    dos_trust: Optional[int] = None
    signed: bool = False  # signed anywhere on the page
    signed_in_tail: bool = False  # a signature in the page's closing lines
    printed: Optional[tuple[int, int]] = None  # printed "page N of M"
    # Visit / encounter / FIN / CSN / account numbers (the extraction's encounter-type IDs).
    visit_ids: frozenset[str] = frozenset()
    # main | junk | blank | duplicate
    kind: str = "main"
    junk_subtype: str = ""
    junk_confidence: Optional[float] = None
    duplicate_of: Any = None
    # A Visual page (page-tag document_type visual, few words; blank_junk.method.is_visual).
    visual: bool = False

    @property
    def located(self) -> bool:
        return any(line.top is not None for line in self.lines)

    @property
    def dos(self) -> tuple[str, str]:
        return (self.dos_from, self.dos_to or self.dos_from) if self.dos_from else ("", "")


# --- settings -------------------------------------------------------------------------


@dataclass(frozen=True)
class Rules:
    pagination: tuple[re.Pattern, ...]
    max_total: int
    head_top: float
    foot_bottom: float
    head_lines: int
    foot_lines: int
    text_band_fraction: float
    points: dict[str, dict[str, Any]]
    dos_trust: dict[str, int]
    junk_decisive_at: float
    fill: dict[str, float]
    text_proxy_lines: int
    text_proxy_chars: int
    start: dict[str, float]
    start_apply: bool
    semantic: Semantic
    section_rank: dict[str, int]
    section_steps: int
    default_dos_types: frozenset[str]
    high_at: float
    medium_at: float
    curve_a: float
    curve_b: float
    disagreement_factor: float
    review_below: float
    conflict_at: float
    conflict_apply: bool
    weak_score: float
    max_passes: int
    final_rules: dict[str, bool]
    progress_note_families: tuple[str, ...]
    review_long_run: int
    # A heading that opens a visit ("Chief Complaint") in the page's top band (2026-10-23).
    visit_headings: tuple[re.Pattern[str], ...]
    visit_heading_top: float
    # A page of these types never shares a document with a page of other types (2026-10-23).
    demographics_types: frozenset[str]
    visit_heading_breaks: bool
    visual_joins: bool

    def pts(self, name: str) -> float:
        return float(self.points[name]["points"])


def _build(data: dict[str, Any]) -> Rules:
    pag, bands = data["pagination"], data["bands"]
    points = {k: v for k, v in (data.get("points") or {}).items() if isinstance(v, dict)}
    unknown = sorted(set(points) - set(SIGNALS))
    if unknown:
        raise ValueError(f"continuity_canon.json points name signals the engine does not produce: {unknown}")
    missing = sorted(set(SIGNALS) - set(points))
    if missing:
        raise ValueError(f"continuity_canon.json points are missing: {missing}")
    sem = data.get("semantic") or {}
    char, word = sem.get("char") or {}, sem.get("word") or {}
    order = data.get("section_order") or {}
    steps = [step for step in order.get("steps") or [] if isinstance(step, list) and len(step) == 2]
    fill = data.get("page_fill") or {}
    start = data.get("text_start") or {}
    decision = data.get("decision") or {}
    conflict = data.get("conflict_rules") or {}
    final = data.get("final_pass") or {}
    return Rules(
        pagination=tuple(re.compile(p, re.IGNORECASE) for p in [*pag["patterns"], pag["bare_pattern"]]),
        max_total=int(pag["max_total"]),
        head_top=float(bands["head_top"]),
        foot_bottom=float(bands["foot_bottom"]),
        head_lines=int(bands["head_lines"]),
        foot_lines=int(bands["foot_lines"]),
        text_band_fraction=float(bands["text_band_fraction"]),
        points=points,
        dos_trust={k: int(v) for k, v in (data.get("dos_trust") or {}).items() if k != "comment"},
        junk_decisive_at=float((data.get("junk_run") or {}).get("decisive_at", 4.5)),
        fill={k: float(fill[k]) for k in ("full_at", "taper_at", "band_high", "band_low", "empty_at")},
        text_proxy_lines=int(fill.get("text_proxy_lines", 28)),
        text_proxy_chars=int(fill.get("text_proxy_chars", 1800)),
        start={k: float(start[k]) for k in ("straight_at", "near_at", "air_at")},
        start_apply=bool(start.get("apply", False)),
        semantic=Semantic(
            furniture_min_chars=int(sem.get("furniture_min_chars", 12)),
            tail_nth_word=int(sem.get("tail_nth_word", 20)),
            char_ngram=tuple(char.get("ngram", (3, 5))),
            word_ngram=tuple(word.get("ngram", (1, 2))),
            min_df=int(char.get("min_df", 2)),
            char_divisor=float(char.get("norm_divisor", 0.25)),
            word_divisor=float(word.get("norm_divisor", 0.15)),
        ),
        section_rank={name.casefold(): i for i, (_key, names) in enumerate(steps) for name in names},
        section_steps=len(steps),
        default_dos_types=frozenset(
            t.casefold() for t in (data.get("default_final_dos") or {}).get("page_types") or ()
        ),
        high_at=float(decision.get("high_at", 5)),
        medium_at=float(decision.get("medium_at", 2)),
        curve_a=float(decision.get("a", 2.0)),
        curve_b=float(decision.get("b", 0.0)),
        disagreement_factor=float(decision.get("section_disagreement_factor", 0.6)),
        review_below=float(decision.get("review_below", 0.25)),
        conflict_at=float(conflict.get("at", 4)),
        conflict_apply=bool(conflict.get("apply", False)),
        weak_score=float(final.get("weak_score", 2)),
        max_passes=int(final.get("max_passes", 3)),
        final_rules={k: bool(v) for k, v in (final.get("rules") or {}).items()},
        progress_note_families=tuple(final.get("progress_note_families") or ("Progress Note",)),
        review_long_run=int((data.get("review_triggers") or {}).get("long_run_without_pagination", 25)),
        visit_headings=tuple(
            re.compile(rf"^\W*{re.escape(term)}\b", re.IGNORECASE)
            for term in (data.get("visit_headings") or {}).get("terms") or ()
        ),
        visit_heading_top=float((data.get("visit_headings") or {}).get("top_fraction", 0.33)),
        visit_heading_breaks=bool((data.get("visit_headings") or {}).get("decisive", True)),
        visual_joins=bool((data.get("visual_join") or {}).get("enabled", True)),
        demographics_types=frozenset(
            t.casefold() for t in (data.get("demographics_break") or {}).get("page_types") or ()
        ),
    )


_RULES: CanonFile[Rules] = CanonFile(CANON_DIR / "continuity_canon.json", _build)
# DOS_DEFAULT_DATE: the one place the default date is set (the DOS stage reads it too).
_DOS_DEFAULT: CanonFile[Optional[str]] = CanonFile(
    CANON_DIR / "dos_canon.json", lambda data: str(data.get("DOS_DEFAULT_DATE") or "") or None
)


def load_rules() -> Rules:
    return _RULES.get()


def trust_points(tier: Optional[str], rules: Optional[Rules] = None) -> int:
    """Points a date found by this DOS tier is worth (canon ``dos_trust``)."""
    table = (rules or load_rules()).dos_trust
    return int(table.get((tier or "").strip().casefold(), table.get("default", 3)))


# --- lines, bands, page numbers -------------------------------------------------------


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def text_lines(text: str) -> list[Line]:
    return [Line(normalize(line)) for line in (text or "").splitlines() if line.strip()]


def head(page: PageInput, rules: Rules) -> list[str]:
    if page.located:
        return [l.text for l in page.lines if l.top is not None and l.top < rules.head_top]
    return [l.text for l in page.lines[: rules.head_lines]]


def foot(page: PageInput, rules: Rules) -> list[str]:
    if page.located:
        return [l.text for l in page.lines if l.bottom is not None and l.bottom > rules.foot_bottom]
    return [l.text for l in page.lines[-rules.foot_lines :]] if page.lines else []


def body(page: PageInput, rules: Rules) -> list[Line]:
    """Lines between the header and footer bands, top to bottom."""
    if page.located:
        rows = [
            l for l in page.lines
            if l.top is not None and l.top >= rules.head_top and l.top < rules.foot_bottom
        ]
        return sorted(rows, key=lambda l: (l.top, l.bottom))
    n = len(page.lines)
    return page.lines[min(rules.head_lines, n) : max(min(rules.head_lines, n), n - rules.foot_lines)]


def _pagination_band(page: PageInput, rules: Rules) -> str:
    if page.located:
        return "\n".join(head(page, rules) + foot(page, rules))
    lines = [l.text for l in page.lines]
    k = max(1, int(len(lines) * rules.text_band_fraction))
    return "\n".join(lines[:k] + lines[-k:])


def pagination(page: PageInput, rules: Rules) -> Optional[tuple[int, int]]:
    """(page, total) printed on the page: the extraction's, else read from the bands."""
    if page.printed:
        return page.printed
    band = _pagination_band(page, rules)
    for pattern in rules.pagination:
        for match in pattern.finditer(band):
            number, total = int(match.group(1)), int(match.group(2))
            if 1 <= number <= total <= rules.max_total:
                return number, total
    return None


def body_end(page: PageInput, rules: Rules) -> Optional[float]:
    """Where the page's text stops: the bottom of its last line starting above the footer
    band, as a share of the page height; None without line positions."""
    bottoms = [
        l.bottom for l in page.lines
        if l.top is not None and l.bottom is not None and l.top < rules.foot_bottom
    ]
    return max(bottoms) if bottoms else None


def same_type_probability(previous: PageInput, current: PageInput) -> Optional[float]:
    """How likely ``current`` is the previous page's type: the classifier's confidence when
    the types (L1) agree, one minus it when they differ; None without either."""
    if not previous.page_type or not current.page_type or current.page_confidence is None:
        return None
    confidence = float(current.page_confidence)
    return confidence if current.page_type == previous.page_type else 1.0 - confidence


# --- one gap --------------------------------------------------------------------------


@dataclass
class Gap:
    """The decision about the space between two scored pages (``before`` / ``after`` are
    indexes into the chart's pages)."""

    before: int
    after: int
    ending: dict[str, float] = field(default_factory=dict)
    opening: dict[str, float] = field(default_factory=dict)
    pair: dict[str, float] = field(default_factory=dict)
    # Computed but not allowed to move the score (text_start until measured, conflicts).
    recorded: dict[str, Any] = field(default_factory=dict)
    decisive: list[str] = field(default_factory=list)
    total: float = 0.0
    is_break: bool = False
    decided_by: str = "sum"
    level: str = "low"
    confidence: float = 0.0
    flags: list[str] = field(default_factory=list)
    final_pass: str = ""
    # What a signal found, in words (the visit heading's text), for continuity.json.
    evidence: dict[str, str] = field(default_factory=dict)

    @property
    def sections(self) -> tuple[float, float, float]:
        return (
            round(sum(self.ending.values()), 2),
            round(sum(self.opening.values()), 2),
            round(sum(self.pair.values()), 2),
        )

    @property
    def decisive_break(self) -> bool:
        return self.decided_by in ("decisive", "decisive_conflict") and self.is_break

    def parts(self) -> dict[str, float]:
        return {**self.ending, **self.opening, **self.pair}

    def summary(self) -> str:
        top = sorted(self.parts().items(), key=lambda kv: -abs(kv[1]))[:4]
        signals = ", ".join(f"{n} {v:+g}" for n, v in top) or "no signal"
        verdict = "break" if self.is_break else "joined"
        text = f"{self.total:+g} {verdict}, {self.level} ({signals})"
        if self.final_pass:
            text += f"; final pass: {self.final_pass}"
        return text

    def to_dict(self) -> dict[str, Any]:
        e, o, p = self.sections
        return {
            "score": self.total,
            "decision": "break" if self.is_break else "joined",
            "decided_by": self.decided_by,
            "confidence": self.confidence,
            "level": self.level,
            "ending": self.ending, "opening": self.opening, "pair": self.pair,
            "sub_scores": {"ending": e, "opening": o, "pair": p},
            "recorded": self.recorded,
            "flags": self.flags,
            "final_pass": self.final_pass or None,
            "evidence": self.evidence,
        }


@dataclass
class _Context:
    rules: Rules
    corpus: Corpus
    run_length: dict[int, int]
    run_confidence: dict[int, float]


def _run_value(index: int, ctx: _Context) -> tuple[float, bool]:
    """(points, decisive) for the edge of the junk run ``index`` belongs to."""
    spec = ctx.rules.points["junk_run"]
    length = ctx.run_length.get(index, 0)
    base = float(spec.get("points_three_or_more", 5)) if length >= 3 else float(spec["points"])
    value = round(base * ctx.run_confidence.get(index, 1.0), 2)
    return value, length >= 3 and value >= ctx.rules.junk_decisive_at


def score_gap(
    pages: Sequence[PageInput],
    a_index: int,
    b_index: int,
    *,
    open_dos: tuple[str, str],
    open_trust: Optional[int],
    blanks: int,
    ctx: _Context,
) -> Gap:
    rules = ctx.rules
    a, b = pages[a_index], pages[b_index]
    gap = Gap(a_index, b_index)
    E, O, P = gap.ending, gap.opening, gap.pair
    pts = rules.pts
    proxy_fill = False

    a_body, b_body = body(a, rules), body(b, rules)
    a_body_text = [l.text for l in a_body]
    b_body_text = [l.text for l in b_body]
    pa, pb = pagination(a, rules), pagination(b, rules)

    # ending: the earlier page alone
    if a.signed_in_tail:
        E["signed_in_tail"] = pts("signed_in_tail")
    if a.signed:
        E["signed_anywhere"] = pts("signed_anywhere")
    if pa and pa[0] == pa[1]:
        E["last_numbered_page"] = pts("last_numbered_page")
    if says_continued((a_body_text or [l.text for l in a.lines])[-2:]):
        E["says_continued"] = pts("says_continued")
    end = body_end(a, rules)
    if end is not None:
        value = round(pts("page_fill") * fill_value(end, rules.fill), 2)
        if value:
            E["page_fill"] = value
    elif len(a.lines) >= rules.text_proxy_lines or len(a.text) >= rules.text_proxy_chars:
        # Without line positions only the join side is known (a full page).
        E["page_fill"] = -pts("page_fill")
        proxy_fill = True

    # opening: the later page alone
    if b.title_hit:
        O["has_title"] = pts("has_title")
    heading = visit_heading(b, rules)
    if heading:
        O["visit_heading"] = pts("visit_heading")
        gap.evidence["visit_heading"] = heading
    if b.located and b_body:
        head_bottoms = [l.bottom for l in b.lines if l.top is not None and l.top < rules.head_top and l.bottom]
        base = max(head_bottoms) if head_bottoms else 0.0
        start = round(pts("text_start") * start_value(max(0.0, (b_body[0].top or 0.0) - base), rules.start), 2)
        if rules.start_apply and start:
            O["text_start"] = start
        else:
            gap.recorded["text_start"] = start

    # pair: the two compared
    if b.kind == "main" and b.dos_from and open_dos[0]:
        trust = min(int(b.dos_trust or 3), int(open_trust or 3))
        if b.dos == open_dos:
            P["same_dos"] = -float(trust)
        else:
            P["different_dos"] = float(trust)
        if trust >= 5:
            gap.decisive.append("same_dos" if b.dos == open_dos else "different_dos")
    if pa and pb and pa[1] == pb[1] and pb[0] == pa[0] + 1:
        P["pagination_follows"] = pts("pagination_follows")
        gap.decisive.append("pagination_follows")
    same = same_type_probability(a, b)
    if same is not None:
        change = (1.0 - same) * float(b.page_confidence or 0.0)
        if change >= 0.05:
            P["page_type_change"] = round(pts("page_type_change") * change, 2)
    if a.names and a.names == b.names:
        P["same_page_type"] = pts("same_page_type")
    forward = section_forward(
        section_ranks(a.section_headers, rules.section_rank),
        section_ranks(b.section_headers, rules.section_rank),
        rules.section_steps,
    )
    if forward > 0:
        P["sections_forward"] = round(pts("sections_forward") * forward, 2)
    if (a.visit_ids | visit_numbers(a.text)) & (b.visit_ids | visit_numbers(b.text)):
        P["same_visit_number"] = pts("same_visit_number")
    min_chars = rules.semantic.furniture_min_chars
    if repeated_line(head(a, rules), head(b, rules), min_chars):
        P["same_header_line"] = pts("same_header_line")
    if repeated_line(foot(a, rules), foot(b, rules), min_chars):
        P["same_footer_line"] = pts("same_footer_line")
    shared = furniture([l.text for l in a.lines], [l.text for l in b.lines], min_chars)
    a_rest = [t for t in a_body_text if normalized_line(t) not in shared]
    b_rest = [t for t in b_body_text if normalized_line(t) not in shared]
    tail, top = tail_block(a_rest, rules.semantic.tail_nth_word), head_block(b_rest, rules.semantic.tail_nth_word)
    if tail and top:
        sem = rules.semantic
        char = clip(ctx.corpus.char.cosine(tail, top) / sem.char_divisor) if ctx.corpus.char else 0.0
        word = clip(ctx.corpus.word.cosine(tail, top) / sem.word_divisor) if ctx.corpus.word else 0.0
        if char >= 0.05:
            P["wording_char"] = round(pts("wording_char") * char, 2)
            # Tie-breakers only beside a wording signal that already fires.
            if a_rest and cut_mid_sentence(a_rest[-1]):
                E["cut_mid_sentence"] = pts("cut_mid_sentence")
            if b_rest and starts_mid_thought(b_rest[0]):
                O["starts_mid_thought"] = pts("starts_mid_thought")
        if word >= 0.05:
            P["wording_word"] = round(pts("wording_word") * word, 2)
    a_junk, b_junk = a.kind == "junk", b.kind == "junk"
    if a_junk and b_junk and a.junk_subtype and a.junk_subtype == b.junk_subtype:
        P["same_junk_kind"] = pts("same_junk_kind")
    if a_junk != b_junk:
        value, decisive = _run_value(a_index if a_junk else b_index, ctx)
        P["junk_run"] = value
        if decisive:
            gap.decisive.append("junk_run")
    if blanks:
        P["blank_between"] = pts("blank_between")

    _decide(gap, rules, proxy_fill)
    # Demographics never shares a document with another kind of page (2026-10-23, user):
    # a break whatever the score, which no later pass merges back.
    if a.kind == "main" and b.kind == "main" and is_demographics(a, rules) != is_demographics(b, rules):
        gap.is_break, gap.decided_by, gap.level = True, "decisive", "high"
        gap.confidence = max(gap.confidence, 0.9)
        gap.evidence["demographics_boundary"] = "Demographics page beside another kind of page"
        gap.flags.append("Demographics boundary: always a break")
    # A visit heading at the top (Reason for Appointment, HPI, ...) opens a new Progress
    # Note (2026-10-23, user) -- unless the printed page N of M says it runs on.
    if (
        rules.visit_heading_breaks and heading and a.kind == "main" and b.kind == "main"
        and "pagination_follows" not in P
    ):
        gap.is_break, gap.decided_by, gap.level = True, "decisive", "high"
        gap.confidence = max(gap.confidence, 0.9)
        gap.flags.append(f"visit heading '{heading}': always a break")
    # A Visual page that is not junk belongs to the page before it (2026-10-23, user).
    if rules.visual_joins and b.visual and b.kind == "main" and a.kind == "main":
        gap.is_break, gap.decided_by, gap.level = False, "decisive", "high"
        gap.confidence = max(gap.confidence, 0.9)
        gap.evidence["visual_join"] = "Visual page joins the page before it"
        gap.flags.append("Visual page: always joins the previous page")
    return gap


def is_demographics(page: "PageInput", rules: Rules) -> bool:
    """The page is a Demographics page: any of its names (or its family) is one of the
    canon's ``demographics_break.page_types``."""
    names = {name.casefold() for name in (*page.names, page.page_type) if name}
    return bool(names & rules.demographics_types)


def visit_heading(page: "PageInput", rules: Rules) -> Optional[str]:
    """A heading that opens a visit ("Chief Complaint", "Reason for Visit") among the
    page's lines in its top band -- the first lines when the OCR gave no positions -- or
    among its section headings there. The matched text, or None."""
    if not rules.visit_headings:
        return None
    located = [line for line in page.lines if line.top is not None]
    lines = (
        [line.text for line in located if line.top < rules.visit_heading_top]
        if located else [line.text for line in page.lines[:8]]
    )
    lines += [
        str(header.get("text") or "")
        for header in page.section_headers
        if header.get("top") is None or float(header.get("top") or 0) < rules.visit_heading_top
    ]
    for text in lines:
        for pattern in rules.visit_headings:
            if pattern.search(text.strip()):
                return text.strip()[:60]
    return None


def _level(total: float, rules: Rules) -> str:
    size = abs(total)
    return "high" if size >= rules.high_at else "medium" if size >= rules.medium_at else "low"


_DOWN = {"high": "medium", "medium": "low", "low": "low"}


def _decide(gap: Gap, rules: Rules, proxy_fill: bool) -> None:
    parts = gap.parts()
    gap.total = round(sum(parts.values()), 2)
    pushes = {name: parts[name] for name in gap.decisive}
    breaks = [n for n, v in pushes.items() if v > 0]
    joins = [n for n, v in pushes.items() if v < 0]
    e, o, p = gap.sections
    at = rules.conflict_at
    pattern = "A" if e >= at and p <= -at else "B" if o >= at and p <= -at else ""

    if breaks and joins:
        # A date is a fact about the care; a page number about the printing.
        gap.is_break = "different_dos" in breaks or not joins
        gap.decided_by, gap.level = "decisive_conflict", "medium"
        gap.flags.append("decisive signals disagree: " + ", ".join(sorted(breaks + joins)))
    elif breaks or joins:
        gap.is_break = bool(breaks)
        gap.decided_by, gap.level = "decisive", "high"
    else:
        gap.is_break = gap.total > 0
        gap.decided_by = "sum" if gap.total else "tie"
        gap.level = _level(gap.total, rules)
        if not gap.total:
            gap.flags.append("score 0: joined")
        if pattern:
            gap.recorded["conflict"] = pattern
            if rules.conflict_apply:
                gap.is_break = pattern == "A"
                gap.decided_by, gap.level = f"conflict_{pattern.lower()}", "medium"
    if pattern:
        if gap.decided_by not in ("conflict_a", "conflict_b"):
            gap.level = _DOWN[gap.level]
        gap.flags.append(
            "finished, but still matching (pattern A)" if pattern == "A"
            else "suspected title misfire (pattern B)"
        )

    # How sure: one curve over the score (a, b to be fitted on the labelled gaps).
    probability = 1.0 / (1.0 + math.exp(-(gap.total - rules.curve_b) / rules.curve_a))
    confidence = abs(2.0 * probability - 1.0)
    signs = {s > 0 for s in (e, o, p) if s}
    if len(signs) > 1:
        confidence *= rules.disagreement_factor
    if gap.decided_by == "decisive":
        confidence = max(confidence, 0.9)
    gap.confidence = round(confidence, 4)
    if gap.confidence < rules.review_below or gap.level == "low":
        gap.flags.append("low confidence")
    if proxy_fill:
        without = gap.total - parts.get("page_fill", 0.0)
        if (without > 0) != gap.is_break or without == 0:
            gap.flags.append("page fill from the text proxy decided it")


# --- the chart ------------------------------------------------------------------------


@dataclass
class _Segment:
    kind: str  # main | junk
    pages: list[int]  # scored pages, in order


def _junk_runs(pages: Sequence[PageInput]) -> tuple[dict[int, int], dict[int, float]]:
    """Each junk page's run length (consecutive junk pages; blanks and duplicates in between
    do not break a run) and the run's mean junk confidence."""
    lengths: dict[int, int] = {}
    confidence: dict[int, float] = {}
    run: list[int] = []

    def close() -> None:
        if run:
            values = [float(pages[i].junk_confidence) for i in run if pages[i].junk_confidence is not None]
            mean = sum(values) / len(values) if values else 1.0
            for i in run:
                lengths[i], confidence[i] = len(run), mean
            run.clear()

    for i, page in enumerate(pages):
        if page.kind == "junk":
            run.append(i)
        elif page.kind == "main":
            close()
    close()
    return lengths, confidence


def _corpus(pages: Sequence[PageInput], scored: list[int], rules: Rules) -> Corpus:
    blocks: list[str] = []
    n = rules.semantic.tail_nth_word
    for a, b in zip(scored, scored[1:]):
        blocks += [tail_block([l.text for l in body(pages[a], rules)], n),
                   head_block([l.text for l in body(pages[b], rules)], n)]
    return fit_corpus(blocks, rules.semantic)


def _doc_dos(pages: Sequence[PageInput], seg: _Segment) -> tuple[tuple[str, str], Optional[int]]:
    for i in seg.pages:
        if pages[i].kind == "main" and pages[i].dos_from:
            return pages[i].dos, pages[i].dos_trust
    return ("", ""), None


def _doc_type(pages: Sequence[PageInput], seg: _Segment) -> tuple[str, ...]:
    for i in seg.pages:
        if pages[i].kind == "main":
            return pages[i].names or ((pages[i].page_type,) if pages[i].page_type else ())
    return ()


def _final_pass(pages: Sequence[PageInput], segments: list[_Segment], gaps: dict[tuple[int, int], Gap], rules: Rules) -> list[_Segment]:
    on = rules.final_rules

    def boundary(left: _Segment, right: _Segment) -> Gap:
        return gaps[(left.pages[-1], right.pages[0])]

    def weak(gap: Gap) -> bool:
        return not gap.decisive_break and (gap.level == "low" or gap.total <= rules.weak_score)

    # Split: a document holding two different dates read from a label.
    if on.get("split_two_dates", True):
        out: list[_Segment] = []
        for seg in segments:
            current = ""
            start = 0
            for k, i in enumerate(seg.pages):
                page = pages[i]
                if page.kind != "main" or not page.dos_from or int(page.dos_trust or 0) < 5:
                    continue
                if not current:
                    current = page.dos_from
                elif page.dos_from != current and k > start:
                    gap = gaps[(seg.pages[k - 1], i)]
                    gap.is_break, gap.final_pass, gap.level = True, "split: two dates in one document", "medium"
                    gap.flags.append("final pass split")
                    out.append(_Segment(seg.kind, seg.pages[start:k]))
                    start, current = k, page.dos_from
            out.append(_Segment(seg.kind, seg.pages[start:]))
        segments = out

    def merge(left: int, rule: str) -> None:
        gap = boundary(segments[left], segments[left + 1])
        gap.is_break, gap.final_pass = False, f"merged: {rule}"
        segments[left] = _Segment(segments[left].kind, segments[left].pages + segments[left + 1].pages)
        del segments[left + 1]

    for _ in range(rules.max_passes):
        changed = False
        i = 0
        while i < len(segments) - 1:
            a, b = segments[i], segments[i + 1]
            gap = boundary(a, b)
            if not weak(gap):
                i += 1
                continue
            (a_dos, a_trust), (b_dos, b_trust) = _doc_dos(pages, a), _doc_dos(pages, b)
            two_dates = a_dos[0] and b_dos[0] and a_dos != b_dos
            if a.kind == b.kind == "junk":
                kinds = {pages[j].junk_subtype for j in a.pages + b.pages}
                if on.get("junk_rejoin", True) and len(kinds) == 1:
                    merge(i, "junk_rejoin")
                    changed = True
                    continue
            elif a.kind == b.kind == "main" and not two_dates:
                rule = ""
                last_lines = [l.text for l in body(pages[a.pages[-1]], rules)][-2:]
                if on.get("same_date", True) and a_dos[0] and a_dos == b_dos:
                    rule = "same_date"
                elif on.get("continued_across", True) and says_continued(last_lines):
                    rule = "continued_across"
                elif (
                    on.get("same_type_untitled", True)
                    and _doc_type(pages, a) and _doc_type(pages, a) == _doc_type(pages, b)
                    and not pages[a.pages[0]].title_hit and not pages[b.pages[0]].title_hit
                ):
                    rule = "same_type_untitled"
                elif (
                    on.get("orphan_page", True) and i + 2 < len(segments)
                    and len(b.pages) == 1 and not b_dos[0]
                    and segments[i + 2].kind == "main"
                    and _doc_type(pages, a) and _doc_type(pages, a) == _doc_type(pages, segments[i + 2])
                    and weak(boundary(b, segments[i + 2]))
                ):
                    merge(i + 1, "orphan_page")  # the orphan joins forward
                    changed = True
                    continue
                if rule:
                    merge(i, rule)
                    changed = True
                    continue
            i += 1
        if not changed:
            break

    # A document that opens on a Progress Note runs on over the documents after it until
    # a new strong date (read from a label), a signature on its last page, a decisive
    # break, or a junk group. A lab table or a questionnaire after the note stays in it.
    if on.get("progress_note_runs_on", True):
        families = {f.casefold() for f in rules.progress_note_families}
        i = 0
        while i < len(segments) - 1:
            a, b = segments[i], segments[i + 1]
            first = next((pages[j] for j in a.pages if pages[j].kind == "main"), None)
            if a.kind != "main" or first is None or (first.page_type or "").casefold() not in families:
                i += 1
                continue
            gap = boundary(a, b)
            last = pages[a.pages[-1]]
            b_first = next((pages[j] for j in b.pages if pages[j].kind == "main"), None)
            (a_dos, _), (b_dos, b_trust) = _doc_dos(pages, a), _doc_dos(pages, b)
            new_date = (
                b_first is not None and b_first.dos_from and int(b_first.dos_trust or 0) >= 5
                and b_first.dos != a_dos
            )
            if (
                b.kind == "junk" or gap.decisive_break or new_date
                or last.signed or last.signed_in_tail
            ):
                i += 1
                continue
            merge(i, "progress_note_runs_on")
    return segments


def assign(pages: Sequence[PageInput], rules: Optional[Rules] = None) -> list[dict[str, Any]]:
    """One row per page, in the order given (which must be page order)."""
    rules = rules or load_rules()
    run_length, run_conf = _junk_runs(pages)
    scored = [
        i for i, p in enumerate(pages)
        if p.kind == "main" or (p.kind == "junk" and run_length.get(i, 0) >= 2)
    ]
    ctx = _Context(rules, _corpus(pages, scored, rules), run_length, run_conf)

    gaps: dict[tuple[int, int], Gap] = {}
    segments: list[_Segment] = []
    open_dos: tuple[str, str] = ("", "")
    open_trust: Optional[int] = None
    for k, i in enumerate(scored):
        page = pages[i]
        kind = "junk" if page.kind == "junk" else "main"
        if k == 0:
            segments.append(_Segment(kind, [i]))
        else:
            prev = scored[k - 1]
            blanks = sum(1 for j in range(prev + 1, i) if pages[j].kind == "blank")
            gap = score_gap(pages, prev, i, open_dos=open_dos, open_trust=open_trust, blanks=blanks, ctx=ctx)
            gaps[(prev, i)] = gap
            if gap.is_break:
                segments.append(_Segment(kind, [i]))
                # A junk segment owns no date: the last document's date stays open across it,
                # so the first page after a records request is still compared with it.
                if kind == "main":
                    open_dos, open_trust = ("", ""), None
            else:
                segments[-1].pages.append(i)
                if kind == "main" and segments[-1].kind == "junk":
                    segments[-1].kind = "main"
        if page.kind == "main" and page.dos_from and not open_dos[0]:
            open_dos, open_trust = page.dos, page.dos_trust

    segments = _final_pass(pages, segments, gaps, rules)
    return _rows(pages, scored, segments, gaps, rules)


def _rows(
    pages: Sequence[PageInput],
    scored: list[int],
    segments: list[_Segment],
    gaps: dict[tuple[int, int], Gap],
    rules: Rules,
) -> list[dict[str, Any]]:
    # One numbering for documents and junk groups, so document_seq carries both
    # ('New (Doc 3)', 'New (Junk 4)').
    seg_of: dict[int, int] = {}
    numbers: list[tuple[str, int]] = []
    for s, seg in enumerate(segments):
        numbers.append(("junk" if seg.kind == "junk" else "main", s + 1))
        for i in seg.pages:
            seg_of[i] = s

    # Transparent pages (blank, single junk, duplicate) sit inside a segment or between two.
    members: dict[int, list[int]] = {s: list(seg.pages) for s, seg in enumerate(segments)}
    between: set[int] = set()
    gap_before: dict[int, Gap] = {gap.after: gap for gap in gaps.values()}
    gap_after: dict[int, Gap] = {gap.before: gap for gap in gaps.values()}
    position = {i: k for k, i in enumerate(scored)}
    for i, page in enumerate(pages):
        if i in seg_of or page.kind == "duplicate":
            continue
        prev = next((j for j in reversed(scored) if j < i), None)
        nxt = next((j for j in scored if j > i), None)
        if prev is not None and nxt is not None:
            joined = seg_of[prev] == seg_of[nxt]
            if joined or page.kind == "junk":
                seg_of[i] = seg_of[prev]  # inside, or a single junk page ends the document before
            else:
                between.add(i)
        elif page.kind == "junk" and (prev is not None or nxt is not None):
            seg_of[i] = seg_of[prev if prev is not None else nxt]
        else:
            between.add(i)
        if i in seg_of:
            members[seg_of[i]].append(i)
    by_id = {p.page_id: i for i, p in enumerate(pages)}
    for i, page in enumerate(pages):
        if page.kind == "duplicate":
            original = by_id.get(page.duplicate_of)
            if original is not None and original in seg_of:
                seg_of[i] = seg_of[original]
                members[seg_of[i]].append(i)
            else:
                between.add(i)
    for s in members:
        members[s].sort()

    # Final values per document, from its content pages.
    finals: dict[int, dict[str, Any]] = {}
    for s, seg in enumerate(segments):
        if seg.kind != "main":
            continue
        content = [pages[i] for i in members[s] if pages[i].kind == "main"]
        if content:
            finals[s] = document_finals([
                {"page_type": p.page_type, "page_subtype": p.page_subtype, "names": p.names,
                 "dos_from": p.dos_from, "dos_to": p.dos_to}
                for p in content
            ])

    rows: list[dict[str, Any]] = []
    for i, page in enumerate(pages):
        s = seg_of.get(i)
        kind, number = numbers[s] if s is not None else ("", None)
        inside = members.get(s, []) if s is not None else []
        seq = inside.index(i) + 1 if s is not None else None
        tag = ""
        if s is not None:
            first, last = inside[0] == i, inside[-1] == i
            tag = "standalone" if first and last else "new" if first else "last" if last else "continue"
        before, after = gap_before.get(i), gap_after.get(i)
        transparent = i not in position
        unsure = any(g is not None and g.level == "low" for g in (before, after))
        rows.append({
            "page_id": page.page_id,
            "page_name": page.page_name,
            "page_number": page.page_number,
            "kind": page.kind,
            "segment_kind": kind or None,
            "segment_seq": number,
            # Every page of a document or junk group, blank and duplicate pages included.
            # imaging_final carries a document's Final values to its content pages only.
            "document_seq": number,
            "seq": seq,
            "tag": tag or None,
            "position": {"new": "first", "standalone": "single"}.get(tag, tag) or None,
            "label": _label(page, tag, kind, number, unsure, i in between),
            "relation": "" if s is None else "new_document" if inside[0] == i else "continue",
            "decided_by": (
                "attached" if transparent and s is not None
                else ("final_pass" if before.final_pass else before.decided_by) if before is not None
                else "chart_start" if s is not None else ""
            ),
            "break_before": before.total if before is not None else None,
            "break_after": after.total if after is not None else None,
            "score": before.total if before is not None else None,
            "confidence": (
                before.confidence if before is not None
                else 1.0 if s is not None and not transparent else None
            ),
            "confidence_level": before.level if before is not None else ("high" if s is not None and not transparent else ""),
            "review_required": bool((before and before.flags) or (after and after.flags)),
            "evidence": (
                before.summary() if before is not None
                else "first page of the chart" if s is not None and not transparent
                else f"{page.kind} page" if page.kind != "main" else ""
            ),
            "signals": {
                "before": before.to_dict() if before is not None else None,
                "after": after.to_dict() if after is not None else None,
            },
            "page_type": page.page_type,
            "dos_from": page.dos_from,
            "dos_to": page.dos_to,
            "section_headers": page.section_headers,
            "_segment": s,
        })
    _finish(rows, pages, members, segments, finals, gaps, rules)
    for row in rows:
        row.pop("_segment", None)
    return rows


def _label(page: PageInput, tag: str, kind: str, number: Optional[int], unsure: bool, between: bool) -> str:
    """'New (Doc 3)', 'Continue? (Doc 3)', 'Last (Junk 4)', 'Blank (in Doc 3)'."""
    where = f"Doc {number}" if kind == "main" else f"Junk {number}"
    if page.kind == "blank":
        return "Blank" if number is None or between else f"Blank (in {where})"
    if not tag or number is None:
        return ""
    return f"{tag.capitalize()}{'?' if unsure else ''} ({where})"


def position_label(position: Optional[str], document: Optional[int]) -> str:
    """Legacy: 'First (Doc 3)' from a position; '' outside a document."""
    if not position or document is None:
        return ""
    return f"{position.capitalize()} (Doc {document})"


def _finish(
    rows: list[dict[str, Any]],
    pages: Sequence[PageInput],
    members: dict[int, list[int]],
    segments: list[_Segment],
    finals: dict[int, dict[str, Any]],
    gaps: dict[tuple[int, int], Gap],
    rules: Rules,
) -> None:
    """Printed page numbers, link strength, start confirmation, long-run flag, Final values."""
    gap_before = {gap.after: gap for gap in gaps.values()}
    for row, page in zip(rows, pages):
        printed = pagination(page, rules) if page.kind == "main" else None
        row["page_no"], row["page_total"] = printed if printed else (None, None)
        row["link_strength"] = None
        row["start_confirmed"] = False
        row["final_page_type"] = row["final_dos_from"] = row["final_dos_to"] = None

    for s, seg in enumerate(segments):
        inside = members[s]
        first = pages[inside[0]]
        printed = pagination(first, rules)
        confirmed = first.title_hit or bool(printed and printed[0] == 1)
        content = [i for i in inside if pages[i].kind == "main"]
        long_unpaginated = (
            seg.kind == "main" and len(content) > rules.review_long_run
            and not any(rows[i]["page_no"] for i in content)
        )
        carried = finals.get(s)
        if carried and carried.get("dos_default") and rows[inside[0]]["evidence"]:
            rows[inside[0]]["evidence"] += (
                f"; {first.page_subtype or first.page_type} is not a visit: "
                f"Final DOS is the default {carried['dos_from']}"
            )
        chain = "strong"
        for k, i in enumerate(inside):
            row = rows[i]
            row["start_confirmed"] = confirmed
            if k:
                gap = gap_before.get(i)
                link = (
                    gap is not None and not gap.is_break
                    and any(gap.parts().get(n, 0) <= -5 for n in ("pagination_follows", "same_dos"))
                )
                chain = "strong" if chain == "strong" and link else "weak"
                row["link_strength"] = chain
            if carried and pages[i].kind == "main":
                row["final_page_type"] = carried["page_type"]
                row["final_dos_from"] = carried["dos_from"]
                row["final_dos_to"] = carried["dos_to"]
            if long_unpaginated:
                row["review_required"] = True


def document_finals(members: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The values a document's pages share, from its pages in order.

    Each member has ``page_type``, ``dos_from``, ``dos_to`` and, optionally,
    ``page_subtype``, ``names`` (its names), ``classification_category``
    and ``encounter_type``. Every page takes the first page's page type, category and
    encounter type, and the DOS of the first page that has one (the first page's when it
    is dated; a later page's when the document opens undated). A stand-alone
    non-visit document — every page a ``default_final_dos`` type — takes DOS_DEFAULT_DATE
    instead (``dos_default``); such a page inside a visit's document takes the visit's DOS.
    """
    opener = members[0]
    finals = {
        "page_type": opener.get("page_type") or None,
        "classification_category": opener.get("classification_category") or None,
        "encounter_type": opener.get("encounter_type") or None,
        "dos_default": False,
    }
    default_date = _DOS_DEFAULT.get()
    if default_date and all(
        uses_default_final_dos(*(m.get("names") or (m.get("page_type"), m.get("page_subtype"))))
        for m in members
    ):
        return {**finals, "dos_from": default_date, "dos_to": default_date, "dos_default": True}
    dated = next((m for m in members if m.get("dos_from")), opener)
    return {
        **finals,
        "dos_from": dated.get("dos_from") or None,
        "dos_to": (dated.get("dos_to") or dated.get("dos_from")) or None,
    }


def uses_default_final_dos(*page_types: Optional[str]) -> bool:
    """A non-visit page type or sub-type: its stand-alone document's Final DOS is the default date."""
    listed = load_rules().default_dos_types
    return any((name or "").strip().casefold() in listed for name in page_types)
