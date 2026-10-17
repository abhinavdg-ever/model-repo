"""Date of service from DOS keys, admit/discharge pairs, or header/footer dates.

A date printed with spaced slashes ('05 / 20 / 2025') is one plain word by now
(geometry.merge_spaced_dates) and is read like any other.

Rules from Data/examp/dos.csv (keys and tiers live in DOS/keys.json):
  - Tiers per page: service keys > admit/discharge > received > weak keys (Date,
    Report Date, ...) > printed. The best tier with a valid date wins; weak keys are only a
    fallback. A lab's Collection date ranks with the service keys and wins among them, so a
    lab page is Collected > Received > Printed.
  - Value: single date (6/28/2024, 9/24/24, 2024-12-02, 08-19-2024, Jul 1, 2024,
    November 6, 2024; a trailing time is dropped) or a range "date - date"
    (-, –, to, thru, through). Admit + discharge on one page are both selected and
    become a range in the record summary.
  - Keyed value: first date after the key on the key's line (weak keys: right after
    the key; received/printed too), else the column-aligned date just below the key (table
    headers).
  - Year must be 2000..next year; dates equal to the page's DOB are never used.
  - Keyless: header/footer only, only when no keyed DOS; dates followed by a clock
    time (fax/print stamps), fax/signature/print lines, prose lines and page labels
    are skipped.
  - One date read by two keys is one pair (drop_shared_values); a note-type label of
    another field followed by a date on its line ('Progress Note: <provider> 07/24/2024')
    gives that date.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from ..member_dob.extract import nearest_date as dob_nearest_date
from ..util.dates import (
    canonical_date,
    date_year,
    find_date_ranges,
    find_dates,
    normalize_date,
)
from ..util.geometry import FOOTER_FRAC, HEADER_FRAC, Word, is_edge_region, median_height
from ..util.keys import KeyHit, linked, load_key_tiers, trusted_field_hits
from ..util.tokens import Token, key_token_span, ordered_tokens, same_line

TIER_RANK = {
    "service": 0, "collected": 0, "admit": 1, "discharge": 1,
    "received": 2, "weak": 3, "printed": 4, "keyless": 5,
}
# Tiers whose date must sit right after the key ('Received: 10/02/2024'), as weak keys.
_NEAR_TIERS = frozenset({"weak", "received", "printed"})
_MIN_YEAR = 2000
_MAX_GAP_WORDS = 6
_WEAK_GAP_WORDS = 1
_BELOW_LINES = 4.0
# Keyless header/footer dates followed by a clock time are fax/print stamps.
_TIME_AFTER = re.compile(r"^[\s,]*\d{1,2}:\d{2}")
_KEYLESS_MAX_TOKENS = 8
_PAGE_LABEL = re.compile(r"(?i)\b(?:page|pago|pg|pq)\b\.?\s*#?:?\s*\d")
# A date introduced by one of these right before it, on the key's own line.
_INTRODUCED = re.compile(r"(?i)\b(?:at|on|dated)\s*$")
# Note-type labels of other fields ('Progress Note:', 'Office Visit', 'Consult Note').
_NOTE_LABEL = re.compile(r"(?i)\b(?:note|visit|consult(?:ation)?)\b")
_KEYLESS_NOISE = frozenset(
    {"from", "fax", "faxed", "sent", "signed", "electronically", "printed", "received"}
)
_MONTH_NAME = re.compile(
    r"(?i)\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b"
)


@dataclass
class DosHit:
    key: str
    region: str
    sentence: str
    tier: str
    value: str = ""
    dos_from: str = ""
    dos_to: str = ""
    score: float = 0.0
    accepted: bool = False
    selected: bool = False
    source: str = ""
    key_hit: KeyHit | None = field(default=None, repr=False, compare=False)


@dataclass
class _Span:
    start: int
    end: int
    value: str
    dos_from: str
    dos_to: str


def _plausible(raw: str) -> bool:
    year = date_year(raw)
    return year is not None and _MIN_YEAR <= year <= date.today().year + 1


def date_spans(text: str, blocked: set[str]) -> list[_Span]:
    """Ranges first, then single dates outside them; implausible / DOB dates dropped."""
    spans: list[_Span] = []
    for match in find_date_ranges(text):
        left, right = match.group(1), match.group(2)
        if not (_plausible(left) and _plausible(right)):
            continue
        if canonical_date(left) in blocked or canonical_date(right) in blocked:
            continue
        low, high = normalize_date(left), normalize_date(right)
        spans.append(_Span(match.start(), match.end(), f"{low} - {high}", low, high))
    for match in find_dates(text):
        if any(match.start() < span.end and match.end() > span.start for span in spans):
            continue
        raw = match.group(1)
        if not _plausible(raw) or canonical_date(raw) in blocked:
            continue
        value = normalize_date(raw)
        spans.append(_Span(match.start(1), match.end(1), value, value, value))
    return sorted(spans, key=lambda span: span.start)


def _span_tokens(tokens: list[Token], span: _Span) -> list[Token]:
    return [token for token in tokens if token.start < span.end and token.end > span.start]


def _confidence(word_gap: int) -> float:
    return round(max(0.55, 0.92 - 0.12 * word_gap), 4)


def _value_origin(tokens: list[Token], text: str, key_first: int, key_last: int) -> int:
    """Where the value may start. A date glued onto the key token ('DATE OF SERVICE:02/12/2024')
    starts inside that token, after the key words, not after the token."""
    start = tokens[key_first].start
    end = tokens[key_last].end
    key_text = text[start:end]
    match = re.match(r"(?i)^(?:[A-Za-z][A-Za-z.]*[\s.:/#-]*)+", key_text)
    if match and match.end() < len(key_text) and re.search(r"\d", key_text[match.end():]):
        return start + match.end()
    return end


def _pick(
    tokens: list[Token],
    text: str,
    key_first: int,
    key_last: int,
    tier: str,
    blocked: set[str],
    header_row: bool = False,
) -> tuple[_Span, float, str] | None:
    key_start, key_end = tokens[key_first], tokens[key_last]
    origin = _value_origin(tokens, text, key_first, key_last)
    line_h = median_height([token.word for token in tokens])
    spans = [span for span in date_spans(text, blocked) if span.start >= origin]
    for span in spans:
        covered = _span_tokens(tokens, span)
        if not covered:
            continue
        head = covered[0]
        on_line = (
            abs(head.word.box.cy - key_end.word.box.cy) <= 0.6 * line_h
            and head.word.box.left >= key_end.word.box.left
        )
        if not on_line:
            continue
        gap = len(text[key_end.end : span.start].split())
        if gap <= (_WEAK_GAP_WORDS if tier in _NEAR_TIERS else _MAX_GAP_WORDS):
            return span, _confidence(gap), "same_line"
        # 'Encounter Note by Davis, Alfred H III, PA at 06/12/2024': introduced on the key's line.
        if tier == "service" and _INTRODUCED.search(text[key_end.end : span.start]):
            return span, 0.6, "same_line_introduced"
        break
    # 'Collection Date -' ends its line and the date starts the next one (a header row's last
    # key has no connector: the next line is its table's values).
    between = text[key_end.end : spans[0].start] if spans else ""
    if spans and not between.strip(" -–:") and re.search(r"[-–:]", between):
        covered = _span_tokens(tokens, spans[0])
        if covered and 0 < covered[0].word.box.top - key_end.word.box.bottom + 0.3 * line_h < 2.0 * line_h:
            return spans[0], 0.85, "next_line"
    if tier in _NEAR_TIERS:
        return _column_below(tokens, spans, key_start, key_end, line_h) if header_row else None
    key_left = key_start.word.box.left
    key_right = key_end.word.box.right
    key_cx = (key_left + key_right) / 2.0
    reach = max(key_right - key_left, 6.0 * line_h)
    below: list[tuple[float, float, _Span]] = []
    for span in spans:
        covered = _span_tokens(tokens, span)
        if not covered:
            continue
        top = min(token.word.box.top for token in covered)
        if top <= key_end.word.box.bottom - 0.3 * line_h:
            continue
        if top - key_end.word.box.bottom > _BELOW_LINES * line_h:
            continue
        cx = (covered[0].word.box.left + covered[-1].word.box.right) / 2.0
        if abs(cx - key_cx) > reach:
            continue
        below.append((round((top - key_end.word.box.bottom) / line_h), abs(cx - key_cx), span))
    if not below:
        return None
    _, _, span = min(below, key=lambda item: (item[0], item[1]))
    return span, 0.8, "below"


def _column_below(
    tokens: list[Token], spans: list[_Span], key_start: Token, key_end: Token, line_h: float
) -> tuple[_Span, float, str] | None:
    """The date directly under a table column header ('Date  Location  PCP' over '12/19/2024 ...'):
    on the next line and overlapping the key horizontally."""
    for span in spans:
        covered = _span_tokens(tokens, span)
        if not covered:
            continue
        top = min(token.word.box.top for token in covered)
        gap = top - key_end.word.box.bottom
        if not -0.3 * line_h < gap < 1.5 * line_h:
            continue
        left, right = covered[0].word.box.left, covered[-1].word.box.right
        if min(right, key_end.word.box.right) > max(left, key_start.word.box.left):
            return span, 0.8, "column"
    return None


def _header_row(hit: KeyHit, words: list[Word]) -> bool:
    """The key's line is a row of labels: several words and no digits at all (a table header)."""
    key_words = [word for word in words if word.index in set(hit.word_indexes)]
    if not key_words:
        return False
    first = key_words[0]
    height = max(first.box.height(), 1.0)
    line = [word for word in words if abs(word.box.cy - first.box.cy) < 0.6 * height]
    others = [word for word in line if word.index not in set(hit.word_indexes)]
    return len(others) >= 2 and not any(ch.isdigit() for word in line for ch in word.content)


def _reach(hit: KeyHit, words: list[Word]) -> list[Word]:
    """The key's window plus the rest of its line and the words that follow it: the window
    scales with the key, so a short key ('Order Date') misses a tabbed or wrapped date."""
    key_words = [word for word in words if word.index in set(hit.word_indexes)]
    if not key_words:
        return hit.value_words
    last = max(key_words, key=lambda word: word.index)
    height = max(last.box.height(), 1.0)
    line = [
        word for word in words
        if abs(word.box.cy - last.box.cy) < 0.6 * height and word.box.left >= last.box.left
    ]
    following = [word for word in words if last.index < word.index <= last.index + 3]
    chosen = {word.index: word for word in hit.value_words + line + following}
    return list(chosen.values())


def extract_box(hit: KeyHit, tier: str, blocked: set[str], words: list[Word] | None = None) -> DosHit:
    tokens, text = ordered_tokens(_reach(hit, words) if words else hit.value_words)
    blank = DosHit(key=hit.key, region=hit.region, sentence=text, tier=tier)
    positions = key_token_span(tokens, hit.word_indexes, hit.key)
    if positions is None:
        return blank
    header_row = tier in _NEAR_TIERS and bool(words) and _header_row(hit, words)
    picked = _pick(tokens, text, positions[0], positions[1], tier, blocked, header_row)
    if picked is None:
        return blank
    span, score, source = picked
    row = DosHit(
        key=hit.key,
        region=hit.region,
        sentence=text,
        tier=tier,
        value=span.value,
        dos_from=span.dos_from,
        dos_to=span.dos_to,
        score=score,
        accepted=True,
        source=source,
    )
    # The same date is often printed twice on a line ('(Order Date - 12/13/2023) (Collection Date - 12/13/2023)').
    row.value_at = [token.word for token in _span_tokens(tokens, span)]
    return row


def _band_words(words: list[Word], page_h: float) -> dict[str, list[Word]]:
    bands: dict[str, list[Word]] = {"header": [], "footer": []}
    if page_h <= 0:
        return bands
    for word in words:
        frac = word.box.cy / page_h
        if frac <= HEADER_FRAC:
            bands["header"].append(word)
        elif frac >= 1.0 - FOOTER_FRAC:
            bands["footer"].append(word)
    return bands


def _segments(tokens: list[Token], line_h: float) -> list[list[Token]]:
    runs: list[list[Token]] = []
    for token in tokens:
        if runs and same_line(runs[-1][-1], token, line_h):
            runs[-1].append(token)
        else:
            runs.append([token])
    return runs


def extract_keyless(words: list[Word], page_h: float, blocked: set[str]) -> list[DosHit]:
    """Clear dates in the header/footer bands (one row per distinct date per band)."""
    line_h = median_height(words)
    rows: list[DosHit] = []
    for band, band_words in _band_words(words, page_h).items():
        tokens, text = ordered_tokens(band_words)
        seen: set[str] = set()
        for run in _segments(tokens, line_h):
            if any(token.text.strip(":,.").casefold() in _KEYLESS_NOISE for token in run):
                continue
            start, end = run[0].start, run[-1].end
            segment = text[start:end]
            # A month-name date in a banner ('Tuesday, September 24, 2024') is longer than
            # the usual header stamp, so the short-line cap does not apply to it.
            if len(run) > (24 if _MONTH_NAME.search(segment) else _KEYLESS_MAX_TOKENS):
                continue
            for span in date_spans(segment, blocked):
                if _TIME_AFTER.match(segment[span.end :]):
                    continue
                window = segment[max(0, span.start - 14) : span.end + 14]
                if _PAGE_LABEL.search(window):
                    continue
                marker = canonical_date(span.dos_from) + "|" + canonical_date(span.dos_to)
                if marker in seen:
                    continue
                seen.add(marker)
                row = DosHit(
                    key="No Key Header" if band == "header" else "No Key Footer",
                    region=f"keyless_{band}",
                    sentence=segment,
                    tier="keyless",
                    value=span.value,
                    dos_from=span.dos_from,
                    dos_to=span.dos_to,
                    score=0.7,
                    accepted=True,
                    source="keyless_edge",
                )
                row.value_at = [
                    token.word for token in run
                    if token.start < start + span.end and token.end > start + span.start
                ]
                rows.append(row)
    return rows


def _dob_values(hits: list[KeyHit]) -> set[str]:
    values: set[str] = set()
    for hit in hits:
        if hit.field != "dob" or not hit.trusted or not hit.value_text:
            continue
        value, _ = dob_nearest_date(hit.value_text, hit.key)
        if value:
            values.add(canonical_date(value))
    return values


def _best(rows: list[DosHit]) -> DosHit:
    counts: dict[str, int] = {}
    for row in rows:
        marker = canonical_date(row.dos_from)
        counts[marker] = counts.get(marker, 0) + 1
    return max(
        rows,
        key=lambda row: (counts[canonical_date(row.dos_from)], is_edge_region(row.region), row.score),
    )


def mark_selected(rows: list[DosHit]) -> list[DosHit]:
    """Pick one DOS per page: best tier first; with both an admit and a discharge date, both
    are selected (the record summary makes them a range). A lab's Collection date wins over
    the other service keys (Order Date, Date of Service) on its page."""
    accepted = [row for row in rows if row.accepted and row.value]
    if not accepted:
        return rows
    top = min(TIER_RANK.get(row.tier, 9) for row in accepted)
    pool = [row for row in accepted if TIER_RANK.get(row.tier, 9) == top]
    pool = [row for row in pool if row.tier == "collected"] or pool
    admits = [row for row in pool if row.tier == "admit"]
    discharges = [row for row in pool if row.tier == "discharge"]
    if admits and discharges:
        _best(admits).selected = True
        _best(discharges).selected = True
        return rows
    _best(admits or pool).selected = True
    return rows


def _note_label_rows(hits: list[KeyHit], words: list[Word], blocked: set[str]) -> list[DosHit]:
    """A note-type label of another field followed on its line by a date: the date of that
    note ('Progress Note: Bradley R Sellers, MD 07/24/2024'). Only same-line dates count."""
    rows = []
    for hit in hits:
        if hit.field == "dos" or not hit.trusted or not _NOTE_LABEL.search(hit.key):
            continue
        row = extract_box(hit, "service", blocked, words)
        if row.accepted and row.source.startswith("same_line"):
            row.key = hit.key
            rows.append(linked(row, hit))
    return rows


def _value_indexes(row: DosHit) -> tuple[int, ...]:
    return tuple(sorted(word.index for word in getattr(row, "value_at", []) or []))


def drop_shared_values(rows: list[DosHit]) -> list[DosHit]:
    """One date read by two keys is one pair. A key on the date's own line owns it over one
    reading it from the line below ('Admit Date/Time:' over 'Disch: 9/25/2024'); keys chained
    on one line are one label ('Admit Date/Date of Service: 06/12/2024'), kept at its start."""
    claims: dict[tuple[int, ...], list[DosHit]] = {}
    for row in rows:
        at = _value_indexes(row)
        if row.accepted and at:
            claims.setdefault(at, []).append(row)
    for claimed in claims.values():
        if len(claimed) < 2:
            continue

        def rank(row: DosHit) -> tuple[int, float]:
            hit = getattr(row, "key_hit", None)
            return (not row.source.startswith("same_line"), hit.box.left if hit is not None else 0.0)

        keep = min(claimed, key=rank)
        for row in claimed:
            if row is not keep:
                row.accepted = False
                row.selected = False
                row.source = f"{row.source}_shared"
    return rows


def extract_page(hits: list[KeyHit], words: list[Word], page_h: float) -> list[DosHit]:
    """Keyed DOS rows (all tiers), keyless header/footer dates only if none accepted."""
    tiers = load_key_tiers("dos")
    blocked = _dob_values(hits)
    rows = [
        linked(extract_box(hit, tiers.get(hit.key.strip().casefold(), "weak"), blocked, words), hit)
        for hit in trusted_field_hits(hits, "dos")
    ]
    for row in rows:
        if row.tier == "printed" and row.accepted:
            # 'Printed: 03/01/2024' is when the page was printed, never a DOS. The key keeps
            # its date so no other key or the keyless rule reads it as one.
            row.accepted, row.score, row.source = False, 0.0, "printed_not_dos"
    rows = drop_shared_values(rows + _note_label_rows(hits, words, blocked))
    if not any(row.accepted for row in rows):
        rows.extend(extract_keyless(words, page_h, blocked | printed_dates(rows)))
    return mark_selected(rows)


def printed_dates(rows: list[DosHit]) -> set[str]:
    """Dates a print key read: never a keyless DOS either."""
    return {canonical_date(row.dos_from) for row in rows if row.tier == "printed" and row.value}

