"""Which document each page belongs to, and the values its pages share.

Every page is judged against the previous page (blank, junk and duplicate
pages are passed over). Three tiers, in order, from ``continuity_canon.json``:

  1. Printed pagination ("Page 3 of 7") in the header or footer decides on its
     own: the next number of the same total continues; page 1, a different
     total, or a number that does not step forward starts a new document.
  2. Page furniture: a line repeated in the header or footer band of both
     pages, headers or footers that look the same, a shared accession, order
     or visit number. These add to a score.
  3. Structure: a title at the top of this page or a signature on the previous
     one subtracts; a "continued" marker adds.

A score at ``continue_at`` or above continues, at ``new_document_at`` or below
starts a new document, and anything between is unknown. An unknown page starts
its own document and is flagged for review, so no value is copied onto it on
no evidence.

A document whose first page is a progress note stays open through pages the
signals cannot decide. It closes after the page with the signature, before a
page the signals or the pagination call a new document, and before the next
encounter: a page with its own, different date of service that carries a
visit-opening section header. The next encounter closes the note even while
printed pagination continues, because one EMR printout paginates several
visits as a single job.

Each document's first page sets the Final page type and the Final date of
service of every page in it. When the first page has no date, the first page
in the document that has one does, and the evidence says so.
"""
from __future__ import annotations

import difflib
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
    # The keyword model found a title term in the page's title zone.
    title_hit: bool = False
    dos_from: str = ""
    dos_to: str = ""
    signed: bool = False
    # Printed page number the key/value extraction chose, when it chose one.
    printed: Optional[tuple[int, int]] = None
    # Blank, junk or duplicate: passed over, belongs to no document.
    skipped: bool = False

    @property
    def located(self) -> bool:
        return any(line.top is not None for line in self.lines)


@dataclass(frozen=True)
class Verdict:
    relation: str  # new_document | continue | unknown
    decided_by: str  # first_page | pagination | signals | progress_note
    confidence_level: str  # high | medium | low
    score: Optional[float]
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
    min_line_chars: int
    similarity_threshold: float
    fingerprint: dict[str, float]
    identity: dict[str, re.Pattern]
    identity_weights: dict[str, float]
    title_top: float
    continued: re.Pattern
    structure: dict[str, float]
    continue_at: float
    new_document_at: float
    open_page_types: frozenset[str]
    encounter_headers: frozenset[str]


def _build(data: dict[str, Any]) -> Rules:
    pag = data["pagination"]
    bands = data["bands"]
    fp = data["fingerprint"]
    ident = data["identity"]
    struct = data["structure"]
    decision = data["decision"]
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
        min_line_chars=int(fp["min_line_chars"]),
        similarity_threshold=float(fp["similarity_threshold"]),
        fingerprint={k: float(v) for k, v in fp["weights"].items()},
        identity={k: re.compile(v, re.IGNORECASE) for k, v in ident["patterns"].items()},
        identity_weights={k: float(v) for k, v in ident["weights"].items()},
        title_top=float(struct["title_top"]),
        continued=re.compile(struct["continued_pattern"], re.IGNORECASE),
        structure={k: float(v) for k, v in struct["weights"].items()},
        continue_at=float(decision["continue_at"]),
        new_document_at=float(decision["new_document_at"]),
        open_page_types=frozenset(data["open_until_signature"]["page_types"]),
        encounter_headers=frozenset(
            h.casefold() for h in data["open_until_signature"]["encounter_headers"]
        ),
    )


_RULES: CanonFile[Rules] = CanonFile(CANON_DIR / "continuity_canon.json", _build)


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


def identity(text: str, rules: Rules) -> dict[str, str]:
    found = {}
    for key, pattern in rules.identity.items():
        match = pattern.search(text or "")
        if match:
            found[key] = match.group(1).strip().lower()
    return found


def has_title(page: PageInput, rules: Rules) -> bool:
    """A matched section header near the top. Unmatched header candidates do
    not count: any bold line ("Respiratory:") is one."""
    return any(
        h.get("matched_canonical") and h.get("top") is not None and float(h["top"]) < rules.title_top
        for h in page.section_headers
    )


# --- one pair -----------------------------------------------------------------


def judge(previous: PageInput, current: PageInput, rules: Rules) -> Verdict:
    """Does ``current`` continue ``previous``'s document?"""
    before, now = pagination(previous, rules), pagination(current, rules)
    if now and now[0] == 1 and before != now:
        return Verdict("new_document", "pagination", "high", None, f"page 1 of {now[1]}")
    if before and now and before[1] == now[1] and now[0] == before[0] + 1:
        return Verdict(
            "continue", "pagination", "high", None, f"page {before[0]}->{now[0]} of {now[1]}"
        )
    if before and now:
        return Verdict(
            "new_document", "pagination", "high", None,
            f"pagination resets ({before[0]}/{before[1]} -> {now[0]}/{now[1]})",
        )

    score, why = 0.0, []
    w = rules.fingerprint
    big = lambda lines: {l for l in lines if len(l) >= rules.min_line_chars}  # noqa: E731
    look_alike = lambda a, b: (  # noqa: E731
        bool(a) and bool(b)
        and difflib.SequenceMatcher(None, " ".join(a), " ".join(b)).ratio()
        > rules.similarity_threshold
    )
    head_a, head_b = head(previous, rules), head(current, rules)
    foot_a, foot_b = foot(previous, rules), foot(current, rules)
    if big(head_a) & big(head_b):
        score += w["repeated_head_line"]
        why.append("repeated header line")
    if big(foot_a) & big(foot_b):
        score += w["repeated_foot_line"]
        why.append("repeated footer line")
    if look_alike(head_a, head_b):
        score += w["head_looks_the_same"]
        why.append("header looks the same")
    if look_alike(foot_a, foot_b):
        score += w["foot_looks_the_same"]
        why.append("footer looks the same")

    ids_a, ids_b = identity(previous.text, rules), identity(current.text, rules)
    for key in sorted(ids_a.keys() & ids_b.keys()):
        if ids_a[key] == ids_b[key]:
            score += rules.identity_weights.get(key, 0.0)
            why.append(f"same {key.replace('_', ' ')}")

    s = rules.structure
    if has_title(current, rules):
        score += s["current_has_title"]
        why.append("page has its own title")
    if previous.signed:
        score += s["previous_had_signature"]
        why.append("previous page was signed")
    if rules.continued.search("\n".join(head_b) or current.text[:300]):
        score += s["current_has_continued_marker"]
        why.append("marked continued")

    if score >= rules.continue_at:
        relation, level = "continue", "medium"
    elif score <= rules.new_document_at:
        relation, level = "new_document", "medium"
    else:
        relation, level = "unknown", "low"
    return Verdict(relation, "signals", level, round(score, 1), "; ".join(why) or "no signal")


# --- the chart ----------------------------------------------------------------


def _dates_differ(page: PageInput, dos: tuple[str, str]) -> bool:
    own = (page.dos_from, page.dos_to or page.dos_from)
    return bool(page.dos_from and dos[0]) and own != dos


def encounter_header(page: PageInput, rules: Rules) -> str:
    """The first section header on the page that opens a visit, or ''."""
    for header in page.section_headers:
        name = str(header.get("matched_canonical") or "")
        if name.casefold() in rules.encounter_headers:
            return name
    return ""


def assign(pages: Sequence[PageInput], rules: Optional[Rules] = None) -> list[dict[str, Any]]:
    """One row per page, in the order given (which must be page order)."""
    rules = rules or load_rules()
    rows: list[dict[str, Any]] = []
    previous: Optional[PageInput] = None
    document = 0
    first: Optional[PageInput] = None  # first page of the open document
    # The open document began with a progress note: the next encounter ends it.
    note_document = False
    # ... and no signature yet: pages the signals cannot decide stay in it.
    note_open = False
    note_dos: tuple[str, str] = ("", "")

    for page in pages:
        if page.skipped:
            rows.append(_row(page, None, None))
            continue

        if previous is None:
            verdict = Verdict("new_document", "first_page", "high", None, "first page of chart")
        else:
            verdict = judge(previous, page, rules)
            if note_document and verdict.relation != "new_document":
                opens = encounter_header(page, rules)
                if opens and _dates_differ(page, note_dos):
                    # The next encounter, even inside one printout's pagination.
                    verdict = Verdict(
                        "new_document", "progress_note", "medium", verdict.score,
                        f"next encounter: {opens} on {page.dos_from}"
                        + (f" ({verdict.evidence})" if verdict.decided_by == "pagination" else ""),
                    )
                elif note_open and verdict.relation == "unknown":
                    verdict = Verdict(
                        "continue", "progress_note", "medium", verdict.score,
                        f"progress note open since page {first.page_number}"
                        + (f"; {verdict.evidence}" if verdict.evidence != "no signal" else ""),
                    )

        if verdict.relation != "continue":
            document += 1
            first = page
            note_document = note_open = page.page_type in rules.open_page_types
            note_dos = (page.dos_from, page.dos_to or page.dos_from)
        elif note_document and not note_dos[0] and page.dos_from:
            note_dos = (page.dos_from, page.dos_to or page.dos_from)

        rows.append(_row(page, verdict, document))
        if note_open and page.signed:
            note_open = False
        previous = page

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
        "score": verdict.score if verdict else None,
        "review_required": bool(verdict and verdict.relation == "unknown"),
        # Printed evidence links this page to the previous one: page numbering,
        # or a "continued" marker. Anything else that continues is weak.
        "link": (
            None if not verdict or verdict.relation != "continue"
            else "strong" if verdict.decided_by == "pagination" or "marked continued" in verdict.evidence
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
                {"page_type": p.page_type, "dos_from": p.dos_from, "dos_to": p.dos_to,
                 "page_number": p.page_number}
                for _, p in members
            ]
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
            row["final_page_type"] = carried["page_type"]
            row["final_dos_from"] = carried["dos_from"]
            row["final_dos_to"] = carried["dos_to"]
            if index == 0 and carried["dos_page_number"] not in (None, page.page_number):
                row["evidence"] += (
                    "; first page has no date of service, Final DOS from page "
                    f"{carried['dos_page_number']}"
                )


def document_finals(members: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The values a document's pages share, from its pages in order.

    Each member has ``page_type``, ``dos_from``, ``dos_to``, ``page_number``
    and, optionally, ``classification_category``. The page type and category
    are the first page's. The DOS is the first page's, or the first page in
    the document that has one (``dos_page_number`` says which page).
    """
    opener = members[0]
    dated = next((m for m in members if m.get("dos_from")), None)
    return {
        "page_type": opener.get("page_type") or None,
        "classification_category": opener.get("classification_category") or None,
        "dos_from": dated["dos_from"] if dated else None,
        "dos_to": (dated.get("dos_to") or dated["dos_from"]) if dated else None,
        "dos_page_number": dated.get("page_number") if dated else None,
    }
