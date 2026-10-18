"""Which document each page belongs to (docs/CONTINUITY_LOGIC.md).

Pages are taken in order. Blank, junk and duplicate pages belong to no document; a blank
or junk page also ends the open one (a duplicate does not). Every other page is judged
against the open document, in three layers:

1. **Gates** — the first that applies decides:
   first page of the chart (new) · a blank / junk page just before (new) · the page's own
   DOS equal to the open document's, From and To (continue) · a different DOS (new) ·
   printed page numbers following on, N -> N+1 of M (continue) · the previous page printed
   "page N of N" (new). DOS outranks pagination; pagination never starts a document.
2. **Weighted evidence** — an undated page no gate decided:
   ``score = sum(weight x value)`` over the signals of ``signals.py`` (weights in
   ``continuity_canon.json``). ``score >= continue_at`` continues, ``<= new_at`` starts a
   new document, between is **unknown**: the page stays in the open document and is
   flagged for review.
3. **Signature terminator** — a previous page with a signature block in its tail closes
   its document even when the score kept it open.

Every page of a document takes the first page's page type (the sub-type can differ), date
of service and encounter type as its Final values (``document_finals``, used by
``imaging_final``). A stand-alone document whose every page is a non-visit type
(``default_final_dos``) takes DOS_DEFAULT_DATE instead.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from stages.lib.canon_store import CANON_DIR, CanonFile

from .signals import (
    SIGNALS,
    Corpus,
    Semantic,
    clip,
    fit_corpus,
    graded_fill,
    pair_text,
    section_forward,
    section_ranks,
    visit_numbers,
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
    # [{"text", "matched_canonical", "top"}] from the OCR JSON.
    section_headers: list[dict[str, Any]] = field(default_factory=list)
    page_type: str = ""  # the family (v24 L1) the pages are compared on
    page_subtype: str = ""
    # Every v24 name of the page, most specific first (axes.names_of).
    names: tuple[str, ...] = ()
    # The page classifier's confidence in ``page_type``, in [0, 1].
    page_confidence: Optional[float] = None
    # A title term for its page type heads the page.
    title_hit: bool = False
    dos_from: str = ""
    dos_to: str = ""
    # Signed anywhere (provider_signature.csv, or a signature block in the text).
    signed: bool = False
    # A signature block in the page's tail: closes its document.
    signed_in_tail: bool = False
    # Printed page number the key/value stage stored, when it stored one.
    printed: Optional[tuple[int, int]] = None
    # Blank, junk or duplicate: passed over, belongs to no document.
    skipped: bool = False
    # A skipped page that also ends the document before it (blank or junk).
    breaks: bool = False

    @property
    def located(self) -> bool:
        return any(line.top is not None for line in self.lines)

    @property
    def dos(self) -> tuple[str, str]:
        return (self.dos_from, self.dos_to or self.dos_from) if self.dos_from else ("", "")


@dataclass(frozen=True)
class Verdict:
    relation: str  # new_document | continue | unknown (stays in the open document, flagged)
    decided_by: str  # first_page | blank_junk | same_dos | new_dos | pagination | page_number_end | weighted | signature
    confidence_level: str  # high | medium | low
    evidence: str
    score: Optional[float] = None
    # page_fill came from the text proxy (no line positions) and the weighted sum decided.
    fill_from_text: bool = False


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
    # Page types / sub-types (casefolded) whose stand-alone document takes the default Final DOS.
    default_dos_types: frozenset[str] = frozenset()
    weights: dict[str, float] = field(default_factory=dict)
    continue_at: float = 4.0
    new_at: float = -4.0
    semantic: Semantic = field(default_factory=Semantic)
    fill_ended_at: float = 0.60
    fill_continues_at: float = 0.92
    fill_graded: bool = True
    text_proxy_lines: int = 28
    text_proxy_chars: int = 1800
    # section_header_canon name (casefolded) -> its step in note order.
    section_rank: dict[str, int] = field(default_factory=dict)
    section_steps: int = 0
    opens_note_ranks: frozenset[int] = frozenset()
    signature_terminator: bool = True
    signature_tail_fraction: float = 0.33
    review_unknown: bool = True
    review_long_run: int = 25
    review_text_fill: bool = True


def _build(data: dict[str, Any]) -> Rules:
    pag, bands = data["pagination"], data["bands"]
    weights = {
        name: float(spec["w"]) for name, spec in (data.get("weights") or {}).items()
        if isinstance(spec, dict) and "w" in spec
    }
    unknown = sorted(set(weights) - set(SIGNALS))
    if unknown:
        raise ValueError(f"continuity_canon.json weights name signals the engine does not produce: {unknown}")
    decision = data.get("decision") or {}
    continue_at, new_at = float(decision.get("continue_at", 4.0)), float(decision.get("new_at", -4.0))
    if not new_at < continue_at:
        raise ValueError(f"continuity_canon.json decision band is inverted: new_at {new_at} >= continue_at {continue_at}")
    sem = data.get("semantic") or {}
    char, word = sem.get("char") or {}, sem.get("word") or {}
    fill = data.get("page_fill") or {}
    order = data.get("section_order") or {}
    steps = [step for step in order.get("steps") or [] if isinstance(step, list) and len(step) == 2]
    key_rank = {key: i for i, (key, _names) in enumerate(steps)}
    signature = data.get("signature_terminator") or {}
    review = data.get("review_triggers") or {}
    return Rules(
        pagination=tuple(re.compile(p, re.IGNORECASE) for p in [*pag["patterns"], pag["bare_pattern"]]),
        max_total=int(pag["max_total"]),
        head_top=float(bands["head_top"]),
        foot_bottom=float(bands["foot_bottom"]),
        head_lines=int(bands["head_lines"]),
        foot_lines=int(bands["foot_lines"]),
        text_band_fraction=float(bands["text_band_fraction"]),
        default_dos_types=frozenset(
            t.casefold() for t in (data.get("default_final_dos") or {}).get("page_types") or ()
        ),
        weights=weights,
        continue_at=continue_at,
        new_at=new_at,
        semantic=Semantic(
            furniture_min_chars=int(sem.get("furniture_min_chars", 12)),
            tail_nth_word=int(sem.get("tail_nth_word", 20)),
            char_ngram=tuple(char.get("ngram", (3, 5))),
            word_ngram=tuple(word.get("ngram", (1, 2))),
            min_df=int(char.get("min_df", 2)),
            char_divisor=float(char.get("norm_divisor", 0.25)),
            word_divisor=float(word.get("norm_divisor", 0.15)),
        ),
        fill_ended_at=float(fill.get("ended_at", 0.60)),
        fill_continues_at=float(fill.get("continues_at", 0.92)),
        fill_graded=bool(fill.get("graded", True)),
        text_proxy_lines=int(fill.get("text_proxy_lines", 28)),
        text_proxy_chars=int(fill.get("text_proxy_chars", 1800)),
        section_rank={name.casefold(): i for i, (_key, names) in enumerate(steps) for name in names},
        section_steps=len(steps),
        opens_note_ranks=frozenset(key_rank[k] for k in order.get("opens_note") or () if k in key_rank),
        signature_terminator=bool(signature.get("enabled", True)),
        signature_tail_fraction=float(signature.get("tail_fraction", 0.33)),
        review_unknown=bool(review.get("unknown_relation", True)),
        review_long_run=int(review.get("long_run_without_pagination", 25)),
        review_text_fill=bool(review.get("page_fill_from_text_proxy", True)),
    )


_RULES: CanonFile[Rules] = CanonFile(CANON_DIR / "continuity_canon.json", _build)
# DOS_DEFAULT_DATE: the one place the default date is set (the DOS stage reads it too).
_DOS_DEFAULT: CanonFile[Optional[str]] = CanonFile(
    CANON_DIR / "dos_canon.json", lambda data: str(data.get("DOS_DEFAULT_DATE") or "") or None
)


def load_rules() -> Rules:
    return _RULES.get()


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


def _pagination_band(page: PageInput, rules: Rules) -> str:
    if page.located:
        return "\n".join(head(page, rules) + foot(page, rules))
    lines = [l.text for l in page.lines]
    k = max(1, int(len(lines) * rules.text_band_fraction))
    return "\n".join(lines[:k] + lines[-k:])


def pagination(page: PageInput, rules: Rules) -> Optional[tuple[int, int]]:
    """(page, total) printed in the header or footer, or None."""
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


# --- the weighted evidence ------------------------------------------------------------


def _fill(page: PageInput, rules: Rules) -> tuple[float, bool]:
    """(page_fill value, from the text proxy). The previous page's text reaching the
    footer means it runs on."""
    end = body_end(page, rules)
    if end is not None:
        fill = min(1.0, end / rules.foot_bottom)
        if rules.fill_graded:
            return graded_fill(fill, rules.fill_ended_at, rules.fill_continues_at), False
        return (1.0 if fill >= rules.fill_continues_at else 0.0), False
    full = len(page.lines) >= rules.text_proxy_lines or len(page.text) >= rules.text_proxy_chars
    return (1.0 if full else 0.0), True


def _repeats(a: list[str], b: list[str], min_chars: int) -> float:
    return 1.0 if {l for l in a if len(l) >= min_chars} & {l for l in b if len(l) >= min_chars} else 0.0


def pair_signals(previous: PageInput, current: PageInput, rules: Rules, corpus: Corpus) -> tuple[dict[str, float], bool]:
    """Every signal's value for one pair, and whether page_fill came from the text proxy."""
    sem = rules.semantic
    text = pair_text([l.text for l in previous.lines], [l.text for l in current.lines], sem)
    char = corpus.char.cosine(text.tail, text.head) if corpus.char and text.tail and text.head else 0.0
    word = corpus.word.cosine(text.tail, text.head) if corpus.word and text.tail and text.head else 0.0
    fill, fill_from_text = _fill(previous, rules)
    same = same_type_probability(previous, current)
    change = (1.0 - same) * float(current.page_confidence or 0.0) if same is not None else 0.0
    prev_ranks = section_ranks(previous.section_headers, rules.section_rank)
    cur_ranks = section_ranks(current.section_headers, rules.section_rank)
    values = {
        "section_forward": section_forward(prev_ranks, cur_ranks, rules.section_steps),
        "page_type_change": clip(change),
        "cur_has_title": 1.0 if current.title_hit else 0.0,
        "same_visit_number": 1.0 if visit_numbers(previous.text) & visit_numbers(current.text) else 0.0,
        "page_fill": fill,
        "header_line_repeats": _repeats(head(previous, rules), head(current, rules), sem.furniture_min_chars),
        "prev_signed": 1.0 if previous.signed else 0.0,
        "body_cosine_char": clip(char / sem.char_divisor),
        "full_path_same": 1.0 if previous.names and previous.names == current.names else 0.0,
        "footer_line_repeats": _repeats(foot(previous, rules), foot(current, rules), sem.furniture_min_chars),
        "body_cosine_word": clip(word / sem.word_divisor),
        "cur_opens_note": 1.0 if cur_ranks and cur_ranks[0] in rules.opens_note_ranks else 0.0,
        "prev_signed_in_tail": 1.0 if previous.signed_in_tail else 0.0,
    }
    return values, fill_from_text


def _weighted(previous: PageInput, current: PageInput, rules: Rules, corpus: Corpus) -> Verdict:
    values, fill_from_text = pair_signals(previous, current, rules, corpus)
    parts = {name: rules.weights.get(name, 0.0) * value for name, value in values.items()}
    score = round(sum(parts.values()), 2)
    top = sorted(((n, v) for n, v in parts.items() if abs(v) >= 0.05), key=lambda nv: -abs(nv[1]))[:4]
    because = ", ".join(f"{n} {v:+.1f}" for n, v in top) or "no signal"
    used_fill = fill_from_text and abs(parts.get("page_fill", 0.0)) >= 0.05
    if score >= rules.continue_at:
        verdict = Verdict("continue", "weighted", "medium", f"score {score:+.1f} ({because})", score, used_fill)
    elif score <= rules.new_at:
        verdict = Verdict("new_document", "weighted", "medium", f"score {score:+.1f} ({because})", score, used_fill)
    else:
        verdict = Verdict(
            "unknown", "weighted", "low",
            f"score {score:+.1f} between {rules.new_at:+.0f} and {rules.continue_at:+.0f}: kept in the document, for review ({because})",
            score, used_fill,
        )
    if rules.signature_terminator and previous.signed_in_tail and verdict.relation != "new_document":
        verdict = Verdict(
            "new_document", "signature", "medium",
            f"the previous page ends with a signature (score {score:+.1f} would have kept it)", score, False,
        )
    return verdict


def _corpus(pages: Sequence[PageInput], rules: Rules) -> Corpus:
    """TF-IDF fitted on every tail / head block of the chart's consecutive pairs."""
    kept = [p for p in pages if not p.skipped]
    blocks: list[str] = []
    for previous, current in zip(kept, kept[1:]):
        text = pair_text([l.text for l in previous.lines], [l.text for l in current.lines], rules.semantic)
        blocks += [text.tail, text.head]
    return fit_corpus(blocks, rules.semantic)


# --- the chart ------------------------------------------------------------------------


def _show(dos: tuple[str, str]) -> str:
    return dos[0] if dos[0] == dos[1] else f"{dos[0]} - {dos[1]}"


def _gate(previous: Optional[PageInput], page: PageInput, broken: bool, document_dos: tuple[str, str], rules: Rules) -> Optional[Verdict]:
    if previous is None:
        return Verdict("new_document", "first_page", "high", "first page of chart")
    if broken:
        return Verdict("new_document", "blank_junk", "high", "after a blank or junk page")
    if page.dos_from:
        if page.dos == document_dos:
            return Verdict("continue", "same_dos", "high", f"same DOS {_show(page.dos)}")
        return Verdict("new_document", "new_dos", "high", f"DOS {_show(page.dos)}")
    before, now = pagination(previous, rules), pagination(page, rules)
    if before and now and before[1] == now[1] and now[0] == before[0] + 1:
        return Verdict("continue", "pagination", "high", f"no DOS of its own; page {before[0]}->{now[0]} of {now[1]}")
    if before and before[0] == before[1]:
        return Verdict("new_document", "page_number_end", "high",
                       f"the previous page was the last (page {before[0]} of {before[1]})")
    return None


def assign(pages: Sequence[PageInput], rules: Optional[Rules] = None) -> list[dict[str, Any]]:
    """One row per page, in the order given (which must be page order)."""
    rules = rules or load_rules()
    corpus = _corpus(pages, rules)
    rows: list[dict[str, Any]] = []
    previous: Optional[PageInput] = None
    document = 0
    document_dos: tuple[str, str] = ("", "")
    broken = False  # a blank or junk page since the previous page
    for page in pages:
        if page.skipped:
            rows.append(_row(page, None, None))
            broken = broken or page.breaks
            continue
        verdict = _gate(previous, page, broken, document_dos, rules) or _weighted(previous, page, rules, corpus)
        if verdict.relation == "new_document":
            document += 1
            document_dos = page.dos
        rows.append(_row(page, verdict, document))
        previous = page
        broken = False
    _finish(rows, pages, rules)
    return rows


def _row(page: PageInput, verdict: Optional[Verdict], document: Optional[int]) -> dict[str, Any]:
    if verdict is None or verdict.relation == "new_document":
        link = None
    elif verdict.relation == "unknown":
        link = "unknown"
    else:
        link = "strong" if verdict.decided_by in ("same_dos", "pagination") else "weak"
    return {
        "page_id": page.page_id,
        "page_name": page.page_name,
        "page_number": page.page_number,
        "document_seq": document,
        "relation": verdict.relation if verdict else "",
        "decided_by": verdict.decided_by if verdict else "blank_junk",
        "confidence_level": verdict.confidence_level if verdict else "",
        "score": verdict.score if verdict else None,
        "review_required": bool(verdict and verdict.relation == "unknown"),
        "fill_from_text": bool(verdict and verdict.fill_from_text),
        "link": link,
        "evidence": verdict.evidence if verdict else "blank, junk or duplicate page",
        "page_type": page.page_type,
        "dos_from": page.dos_from,
        "dos_to": page.dos_to,
        "section_headers": page.section_headers,
    }


def _finish(rows: list[dict[str, Any]], pages: Sequence[PageInput], rules: Rules) -> None:
    """Position within the document, printed page numbers, review flags, Final values."""
    by_document: dict[int, list[tuple[dict[str, Any], PageInput]]] = {}
    for row, page in zip(rows, pages):
        printed = None if page.skipped else pagination(page, rules)
        row["page_no"], row["page_total"] = printed if printed else (None, None)
        row["seq"] = row["position"] = row["link_strength"] = None
        row["label"] = ""
        row["start_confirmed"] = False
        row["final_page_type"] = row["final_dos_from"] = row["final_dos_to"] = None
        if not rules.review_unknown:
            row["review_required"] = False
        if rules.review_text_fill and row.pop("fill_from_text", False):
            row["review_required"] = True
        row.pop("fill_from_text", None)
        if row["document_seq"] is not None:
            by_document.setdefault(row["document_seq"], []).append((row, page))

    for members in by_document.values():
        count = len(members)
        opener = members[0][1]
        opener_counter = pagination(opener, rules)
        confirmed = opener.title_hit or bool(opener_counter and opener_counter[0] == 1)
        # A long run with no printed page number anywhere: a footer template may be
        # binding unrelated documents.
        long_unpaginated = count > rules.review_long_run and not any(r["page_no"] for r, _ in members)
        carried = document_finals([
            {"page_type": p.page_type, "page_subtype": p.page_subtype, "names": p.names,
             "dos_from": p.dos_from, "dos_to": p.dos_to}
            for _, p in members
        ])
        if carried["dos_default"]:
            members[0][0]["evidence"] += (
                f"; {opener.page_subtype or opener.page_type} is not a visit: "
                f"Final DOS is the default {carried['dos_from']}"
            )
        chain = "strong"
        for index, (row, _page) in enumerate(members):
            row["seq"] = index + 1
            row["start_confirmed"] = confirmed
            if index:
                chain = "strong" if chain == "strong" and row["link"] == "strong" else "weak"
                row["link_strength"] = chain
            row["position"] = (
                "single" if count == 1 else "first" if index == 0
                else "last" if index == count - 1 else "continue"
            )
            row["label"] = position_label(row["position"], row["document_seq"])
            row["final_page_type"] = carried["page_type"]
            row["final_dos_from"] = carried["dos_from"]
            row["final_dos_to"] = carried["dos_to"]
            if long_unpaginated:
                row["review_required"] = True


def position_label(position: Optional[str], document: Optional[int]) -> str:
    """'First (Doc 3)', 'Continue (Doc 3)', 'Last (Doc 3)', 'Single (Doc 3)'; '' outside a document."""
    if not position or document is None:
        return ""
    return f"{position.capitalize()} (Doc {document})"


def document_finals(members: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The values a document's pages share, from its pages in order.

    Each member has ``page_type``, ``dos_from``, ``dos_to`` and, optionally,
    ``page_subtype``, ``names`` (its v24 names), ``classification_category``
    and ``encounter_type``. Every
    page takes the first page's page type, category, DOS and encounter type
    (a continue page has no DOS of its own, or the same one). A stand-alone
    non-visit document — every page a ``default_final_dos`` type — takes
    DOS_DEFAULT_DATE instead (``dos_default``); such a page inside a visit's
    document takes the visit's DOS.
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
    return {
        **finals,
        "dos_from": opener.get("dos_from") or None,
        "dos_to": (opener.get("dos_to") or opener.get("dos_from")) or None,
    }


def uses_default_final_dos(*page_types: Optional[str]) -> bool:
    """A non-visit page type or sub-type: its stand-alone document's Final DOS is the default date."""
    listed = load_rules().default_dos_types
    return any((name or "").strip().casefold() in listed for name in page_types)
