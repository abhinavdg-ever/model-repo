"""Which document each page belongs to.

Pages are taken in order (blank, junk and duplicate pages are passed over and
belong to no document). Each page is judged against the open document:

  * A page with a date of service of its own (the DOS stage's Extracted DOS)
    joins the open document when the dates are the same, From and To, and
    starts a new document when they differ. Same-DOS pages are always one
    document.
  * A page without a date is a continue page. It joins the open document
    unless the document ended on the previous page:
      1. the previous page carries a signature, or its printed page number
         says it was the last ("page N of N");
      2. the page type changes, and the page classifier is sure of it: the
         probability that this page is the previous page's type is at most
         ``max_same_type_probability`` (10%; its own confidence when the
         types agree, one minus it when they differ) and its confidence in
         the new type is above ``min_new_type_confidence`` (70%);
      3. the previous page's text stops in the middle of the page: its last
         body line (footer band excluded) ends above ``page_end_max`` of the
         page height. Needs line positions from the OCR JSON;
      4. this page looks like a first page: the keyword model's title for a
         page type other than the previous page's.
    Printed page numbers never split a document: they only end one ("page N
    of N") or confirm a continue page — an undated page whose number follows
    the previous page's (N -> N+1 of the same M) continues whatever the four
    checks say.
  * A blank or junk page in between always ends the document before it.

Every page of a document takes the first page's page type (the sub-type can
differ), date of service and encounter type as its Final values
(``document_finals``, used by ``imaging_final``). A stand-alone document whose
every page is a non-visit type (``default_final_dos``: demographics,
registration, growth chart, problem list, immunizations) takes
DOS_DEFAULT_DATE instead. Each page's Extracted values are untouched.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from stages.lib.canon_store import CANON_DIR, CanonFile


@dataclass(frozen=True)
class Line:
    """One line of page text, lower-cased, with its vertical extent as a
    fraction of the page height when the OCR gave one."""

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
    page_type: str = ""
    page_subtype: str = ""
    # The page classifier's confidence in ``page_type``, in [0, 1].
    page_confidence: Optional[float] = None
    # The keyword model found a title term in the page's title zone.
    title_hit: bool = False
    dos_from: str = ""
    dos_to: str = ""
    signed: bool = False
    # Printed page number the key/value extraction chose, when it chose one.
    printed: Optional[tuple[int, int]] = None
    # Blank, junk or duplicate: passed over, belongs to no document.
    skipped: bool = False
    # A skipped page that also ends the document before it (blank or junk; a
    # duplicate is a copy of a real page and does not).
    breaks: bool = False

    @property
    def located(self) -> bool:
        return any(line.top is not None for line in self.lines)

    @property
    def dos(self) -> tuple[str, str]:
        return (self.dos_from, self.dos_to or self.dos_from) if self.dos_from else ("", "")


@dataclass(frozen=True)
class Verdict:
    relation: str  # new_document | continue
    decided_by: str  # first_page | blank_junk | same_dos | new_dos | pagination | signature | page_number_end | first_page_marker | page_type | page_end | continue_page
    confidence_level: str  # high | medium
    evidence: str


@dataclass(frozen=True)
class Rules:
    pagination: tuple[re.Pattern, ...]
    max_total: int
    head_top: float
    foot_bottom: float
    head_lines: int
    foot_lines: int
    text_band_fraction: float
    page_end_max: float
    max_same_type_probability: float
    min_new_type_confidence: float
    # Page types / sub-types (casefolded) whose stand-alone document takes the default Final DOS.
    default_dos_types: frozenset[str] = frozenset()


def _build(data: dict[str, Any]) -> Rules:
    pag = data["pagination"]
    bands = data["bands"]
    end = data["document_end"]
    return Rules(
        pagination=tuple(
            re.compile(p, re.IGNORECASE) for p in [*pag["patterns"], pag["bare_pattern"]]
        ),
        max_total=int(pag["max_total"]),
        head_top=float(bands["head_top"]),
        foot_bottom=float(bands["foot_bottom"]),
        head_lines=int(bands["head_lines"]),
        foot_lines=int(bands["foot_lines"]),
        text_band_fraction=float(bands["text_band_fraction"]),
        page_end_max=float(end["page_end_max"]),
        max_same_type_probability=float(end["max_same_type_probability"]),
        min_new_type_confidence=float(end["min_new_type_confidence"]),
        default_dos_types=frozenset(
            t.casefold() for t in (data.get("default_final_dos") or {}).get("page_types") or ()
        ),
    )


_RULES: CanonFile[Rules] = CanonFile(CANON_DIR / "continuity_canon.json", _build)
# DOS_DEFAULT_DATE: the one place the default date is set (the DOS stage reads it too).
_DOS_DEFAULT: CanonFile[Optional[str]] = CanonFile(
    CANON_DIR / "dos_canon.json", lambda data: str(data.get("DOS_DEFAULT_DATE") or "") or None
)


def load_rules() -> Rules:
    return _RULES.get()


# --- lines and bands ----------------------------------------------------------


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
        return [
            l.text for l in page.lines if l.bottom is not None and l.bottom > rules.foot_bottom
        ]
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


# --- where a document ends ------------------------------------------------------


def body_end(page: PageInput, rules: Rules) -> Optional[float]:
    """Where the page's text stops (bottom of its last line above the footer
    band), as a fraction of the page height; None without line positions."""
    bottoms = [
        l.bottom for l in page.lines
        if l.top is not None and l.bottom is not None and l.top < rules.foot_bottom
    ]
    return max(bottoms) if bottoms else None


def same_type_probability(previous: PageInput, current: PageInput) -> Optional[float]:
    """How likely ``current`` is the previous page's type, from the classifier's
    confidence in its own answer; None when either type or the confidence is
    missing."""
    if not previous.page_type or not current.page_type or current.page_confidence is None:
        return None
    confidence = float(current.page_confidence)
    return confidence if current.page_type == previous.page_type else 1.0 - confidence


def first_page_marker(previous: PageInput, current: PageInput) -> str:
    """Why ``current`` looks like a document's first page ('' when it does not):
    a title for a page type other than the previous page's."""
    if current.title_hit and current.page_type and current.page_type != previous.page_type:
        return f"{current.page_type} title"
    return ""


def consecutive_pages(previous: PageInput, current: PageInput, rules: Rules) -> str:
    """'page N->N+1' when the printed numbers follow on (same total), else ''."""
    before, now = pagination(previous, rules), pagination(current, rules)
    if before and now and before[1] == now[1] and now[0] == before[0] + 1:
        return f"page {before[0]}->{now[0]} of {now[1]}"
    return ""


def document_ended(previous: PageInput, current: PageInput, rules: Rules) -> Optional[Verdict]:
    """The reason the previous page was its document's last, or None."""
    if previous.signed:
        return Verdict("new_document", "signature", "high", "the previous page was signed")
    printed = pagination(previous, rules)
    if printed and printed[0] == printed[1]:
        return Verdict(
            "new_document", "page_number_end", "high",
            f"the previous page was the last (page {printed[0]} of {printed[1]})",
        )
    marker = first_page_marker(previous, current)
    if marker:
        return Verdict("new_document", "first_page_marker", "medium", f"looks like a first page: {marker}")
    chance = same_type_probability(previous, current)
    if (
        chance is not None
        and round(chance, 4) <= rules.max_same_type_probability
        and float(current.page_confidence or 0) > rules.min_new_type_confidence
    ):
        return Verdict(
            "new_document", "page_type", "medium",
            f"page type changes ({previous.page_type} -> {current.page_type}, "
            f"{chance:.0%} it is still {previous.page_type})",
        )
    end = body_end(previous, rules)
    if end is not None and end < rules.page_end_max:
        return Verdict(
            "new_document", "page_end", "medium",
            f"the previous page's text ends {end:.0%} down the page",
        )
    return None


# --- the chart ----------------------------------------------------------------


def _show(dos: tuple[str, str]) -> str:
    return dos[0] if dos[0] == dos[1] else f"{dos[0]} - {dos[1]}"


def assign(pages: Sequence[PageInput], rules: Optional[Rules] = None) -> list[dict[str, Any]]:
    """One row per page, in the order given (which must be page order)."""
    rules = rules or load_rules()
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

        if previous is None:
            verdict = Verdict("new_document", "first_page", "high", "first page of chart")
        elif broken:
            verdict = Verdict("new_document", "blank_junk", "high", "after a blank or junk page")
        elif page.dos_from:
            if page.dos == document_dos:
                verdict = Verdict("continue", "same_dos", "high", f"same DOS {_show(page.dos)}")
            else:
                verdict = Verdict("new_document", "new_dos", "high", f"DOS {_show(page.dos)}")
        elif consecutive := consecutive_pages(previous, page, rules):
            verdict = Verdict("continue", "pagination", "high", f"no DOS of its own; {consecutive}")
        else:
            verdict = document_ended(previous, page, rules) or Verdict(
                "continue", "continue_page", "medium",
                "no DOS of its own; previous page unsigned, same kind of page, text runs on",
            )

        if verdict.relation != "continue":
            document += 1
            document_dos = page.dos
        rows.append(_row(page, verdict, document))
        previous = page
        broken = False

    _finish(rows, pages, rules)
    return rows


def _row(page: PageInput, verdict: Optional[Verdict], document: Optional[int]) -> dict[str, Any]:
    return {
        "page_id": page.page_id,
        "page_name": page.page_name,
        "page_number": page.page_number,
        "document_seq": document,
        "relation": verdict.relation if verdict else "",
        "decided_by": verdict.decided_by if verdict else "blank_junk",
        "confidence_level": verdict.confidence_level if verdict else "",
        # A shared date of service or consecutive printed page numbers link
        # this page to the previous one; any other continue page is a weak link.
        "link": (
            None if not verdict or verdict.relation != "continue"
            else "strong" if verdict.decided_by in ("same_dos", "pagination")
            else "weak"
        ),
        "evidence": verdict.evidence if verdict else "blank, junk or duplicate page",
        "page_type": page.page_type,
        "dos_from": page.dos_from,
        "dos_to": page.dos_to,
        "section_headers": page.section_headers,
    }


def _finish(rows: list[dict[str, Any]], pages: Sequence[PageInput], rules: Rules) -> None:
    """Position within the document, printed page numbers, and Final values."""
    by_document: dict[int, list[tuple[dict[str, Any], PageInput]]] = {}
    for row, page in zip(rows, pages):
        printed = None if page.skipped else pagination(page, rules)
        row["page_no"], row["page_total"] = printed if printed else (None, None)
        row["seq"] = row["position"] = row["link_strength"] = None
        row["label"] = ""
        row["start_confirmed"] = False
        row["final_page_type"] = row["final_dos_from"] = row["final_dos_to"] = None
        if row["document_seq"] is not None:
            by_document.setdefault(row["document_seq"], []).append((row, page))

    for members in by_document.values():
        count = len(members)
        opener = members[0][1]
        opener_counter = pagination(opener, rules)
        # The real first page was seen: it has its own title or says page 1.
        confirmed = opener.title_hit or bool(opener_counter and opener_counter[0] == 1)
        chain = "strong"
        carried = document_finals(
            [
                {"page_type": p.page_type, "page_subtype": p.page_subtype,
                 "dos_from": p.dos_from, "dos_to": p.dos_to}
                for _, p in members
            ]
        )
        if carried["dos_default"]:
            members[0][0]["evidence"] += (
                f"; {opener.page_subtype or opener.page_type} is not a visit: "
                f"Final DOS is the default {carried['dos_from']}"
            )
        for index, (row, page) in enumerate(members):
            row["seq"] = index + 1
            row["start_confirmed"] = confirmed
            if index == 0:
                row["link_strength"] = None
            else:
                chain = "strong" if chain == "strong" and row["link"] == "strong" else "weak"
                row["link_strength"] = chain
            row["position"] = (
                "single" if count == 1
                else "first" if index == 0
                else "last" if index == count - 1
                else "continue"
            )
            row["label"] = position_label(row["position"], row["document_seq"])
            row["final_page_type"] = carried["page_type"]
            row["final_dos_from"] = carried["dos_from"]
            row["final_dos_to"] = carried["dos_to"]


def position_label(position: Optional[str], document: Optional[int]) -> str:
    """'First (Doc 3)', 'Continue (Doc 3)', 'Last (Doc 3)', 'Single (Doc 3)'; '' outside a document."""
    if not position or document is None:
        return ""
    return f"{position.capitalize()} (Doc {document})"


def document_finals(members: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The values a document's pages share, from its pages in order.

    Each member has ``page_type``, ``dos_from``, ``dos_to`` and, optionally,
    ``page_subtype``, ``classification_category`` and ``encounter_type``. Every
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
        uses_default_final_dos(m.get("page_type"), m.get("page_subtype")) for m in members
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
