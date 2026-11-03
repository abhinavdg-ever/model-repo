"""Read one member ID from the sentence inside a key's overlay box.

Acceptance (all member_id keys):
  - Value is never a pure alphabetic word.
  - Allowed shapes (from Data/examp/id.csv):
      * digits only: 462124, 279
      * letters + digits: HG758668, EMA20577880, G06330
      * digits/letters with hyphens: 134-26-2945, 000859374-1473, D24-58988
      * masked SSN only when last 4 digits exist: XXX-XX-0594 (reject xxx-xx-xxxx)
  - Reject phones as printed ((330) 376-7416, 330-376-7416), any number right after a
    phone / fax label, and slash-dates. A bare 10-digit number is an ID (MRN 3502262043).
  - A system code printed onto the ID is dropped: '3502776450(EV)' -> 3502776450.
  - A key that ends its line reads the ID wrapped to the start of the next line
    ('... MRN: 3502776450(EV)  FIN:' / '86013950877').

Chart is hash-required (Chart# / Chart # only). Similar OCR keys match via fuzzy matcher.
No keyless member-id extraction — only catalog key hits. An untrusted key counts only when
it reads an ID a trusted key already read on the page. A number glued into the key word
('(ID #718,', 'MRN:88234.0') is the value, and '(ID #…)' is trusted on its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..util.keys import KeyHit, extract_field_keys, linked
from ..util.geometry import Word, is_edge_region, region_priority
from ..util.model import predict

LABELS = ["ID", "identifier"]
# A phone number as printed: (330) 376-7416, 330-376-7416, 330.376.7416. A bare 10-digit number
# is an ID (MRN 3502262043, 0000284763) unless a phone label is printed right before it.
_PHONE = re.compile(r"^(?:\(\d{3}\)\s?\d{3}[-.\s]?\d{4}|\d{3}[-.\s]\d{3}[-.\s]?\d{4})$")
_PHONE_LABEL = re.compile(r"(?i)^(?:phone|ph|tel|telephone|fax|cell|mobile|pager)\b\W*")
_SLASH_DATE = re.compile(r"^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}$")
# Punctuation and OCR specks glued to the edge of an ID token ("48497'" is the ID 48497).
_EDGE_CHARS = "#.,;:|'\"`‘’“”"
_EDGE = re.compile(rf"^[{re.escape(_EDGE_CHARS)}()\[\]]+|[{re.escape(_EDGE_CHARS)}()\[\]]+$")
# Must contain at least one digit; letters and internal hyphens optional.
_ID_SHAPE = re.compile(r"^(?=.*\d)[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")
# MRNs printed as 88234.0 or 88234.0-1. A short decimal (3.14) is not an id.
_DECIMAL_ID = re.compile(r"^\d{4,}\.\d+(?:-[A-Za-z0-9]+)?$")
_ALPHA_ONLY = re.compile(r"^[A-Za-z]+$")
# Masked SSN-style: xxx-xx-1234 / XXX-XX-0594 — keep only when last 4 are digits.
_MASKED_SSN = re.compile(r"^[Xx]{2,}-[Xx]{2,}-(\d{4})$")
_MIN_LEN = 3


@dataclass
class IdHit:
    key: str
    region: str
    sentence: str
    ner_text: str = ""
    value: str = ""
    score: float = 0.0
    accepted: bool = False
    selected: bool = False
    source: str = "ner"
    key_hit: KeyHit | None = field(default=None, repr=False, compare=False)


def _normalize(text: str) -> str:
    return text.strip().strip(_EDGE_CHARS)


def is_member_id_value(text: str) -> bool:
    """True when text matches member-id value patterns (digit-bearing, not alpha-only)."""
    return bool(accept(text))


def accept(text: str) -> str:
    """Normalize and keep only ID-shaped tokens.

    Masked forms like xxx-xx-xxxx are rejected unless the last 4 digits are
    present (e.g. XXX-XX-0594 → keep).
    """
    cleaned = _normalize(text)
    if not cleaned or any(char.isspace() for char in cleaned):
        return ""
    if _ALPHA_ONLY.fullmatch(cleaned):
        return ""
    if _PHONE.fullmatch(text.strip()) or _SLASH_DATE.fullmatch(cleaned):
        return ""
    if _DECIMAL_ID.fullmatch(cleaned):
        return cleaned
    masked = _MASKED_SSN.fullmatch(cleaned)
    if masked:
        return cleaned  # last-4 digits available
    # Fully masked / placeholder with no real last-4 (xxx-xx-xxxx).
    compact = cleaned.casefold().replace("-", "")
    if compact and set(compact) <= {"x"}:
        return ""
    if not _ID_SHAPE.fullmatch(cleaned):
        return ""
    if len(cleaned.replace("-", "")) < _MIN_LEN:
        return ""
    return cleaned


def key_span(sentence: str, words, indexes: list[int]) -> tuple[int, int] | None:
    """Character span of the key inside the overlay sentence."""
    del sentence
    wanted = set(indexes)
    offset = 0
    start = end = None
    for word in words:
        if offset:
            offset += 1
        if word.index in wanted:
            if start is None:
                start = offset
            end = offset + len(word.content)
        offset += len(word.content)
    if start is None:
        return None
    return start, end


def _word_gap(sentence: str, key_at: tuple[int, int], token_at: tuple[int, int]) -> tuple[int, int, int]:
    key_start, key_end = key_at
    token_start, token_end = token_at
    if token_end <= key_start:
        middle = sentence[token_end:key_start]
        after = 1
        char_gap = key_start - token_end
    elif token_start >= key_end:
        middle = sentence[key_end:token_start]
        after = 0
        char_gap = token_start - key_end
    else:
        middle = ""
        after = 0
        char_gap = 0
    return len(middle.split()), after, char_gap


# The id sits in the key's own word: '(ID #718,' or 'MRN:88234.0'.
_GLUED_ID = re.compile(r"(?i)[#:]\s*([A-Za-z0-9]+(?:[.\-][A-Za-z0-9]+)*)")
# A system / facility code printed onto the id: '3502776450(EV)' is the MRN 3502776450.
_CODE_SUFFIX = re.compile(r"\([A-Za-z]{1,6}\)[^A-Za-z0-9]*$")


def _id_token(raw: str) -> str:
    token = accept(_EDGE.sub("", raw.strip()))
    if not token and _CODE_SUFFIX.search(raw):
        token = accept(_EDGE.sub("", _CODE_SUFFIX.sub("", raw.strip())))
    return token


def _id_pieces(part: str) -> list[tuple[str, int, int]]:
    """ID spans inside one whitespace-separated piece, as (value, start, end)."""
    whole = _id_token(part)
    if whole:
        return [(whole, 0, len(part))]
    found: list[tuple[str, int, int]] = []
    for match in _GLUED_ID.finditer(part):
        token = accept(match.group(1))
        if token:
            found.append((token, match.start(1), match.end(1)))
    return found


def geometry_confidence(word_gap: int) -> float:
    """A delivered geometry value is never 0. Adjacent to the key is 0.92."""
    return round(max(0.55, 0.92 - 0.12 * word_gap), 4)


def _owned_value(hit: KeyHit, hits: list[KeyHit]):
    """Which window positions this key may read: a value belongs to the label printed before it
    on its own line, so a key never takes another label's value whatever the distance.
      - on the key's line: no other label between them ('MRN: 3502262043(EV)  FIN: 84021092972'
        gives FIN only the second), and a value left of the key has no label before it;
      - on another line: no label before the value on its line ('SSN:' over 'MRN: MM0000005272',
        'MRN.' over 'BIRTH DATE. 12211999') and no label stacked between the key and the value
        ('MRN' / 'Birthday:' / value). A table row under its header has no label: unchanged.
    Takes (start, end) in the window sentence, like nearest()."""
    height = max(hit.box.height(), 1.0)
    slack = 0.5 * height
    others = [other.box for other in hits if other is not hit]
    spans: list[tuple[int, int, Word]] = []
    offset = 0
    for word in hit.value_words:
        spans.append((offset, offset + len(word.content), word))
        offset += len(word.content) + 1

    def same_line(a, b) -> bool:
        return abs(a.cy - b.cy) < 0.6 * height

    def allowed(start: int, end: int) -> bool:
        del end
        word = next((w for s, e, w in spans if s <= start < e), None)
        if word is None or not others:
            return True
        box, key = word.box, hit.box
        if same_line(box, key):
            line = [other for other in others if same_line(other, key)]
            if box.left >= key.right - slack:
                return not any(key.right - slack <= other.left and other.right <= box.left + slack for other in line)
            if box.right <= key.left + slack:
                return not any(other.right <= key.left + slack for other in line)
            return True
        if any(same_line(other, box) and other.right <= box.left + slack for other in others):
            return False
        low, high = sorted((key.cy, box.cy))
        return not any(
            low + 0.6 * height < other.cy < high - 0.6 * height
            and min(other.right, box.right, key.right) > max(other.left, min(box.left, key.left))
            for other in others
        )

    return allowed


def nearest(
    sentence: str,
    key_at: tuple[int, int] | None,
    raws: list[dict] | None = None,
    allowed=None,
) -> tuple[str, float]:
    """ID token closest to the key. A token after the key wins a tie. Leading zeros stay.
    `allowed(start, end)` drops tokens another label owns (_owned_value)."""
    text = sentence or ""
    if key_at is None:
        return "", 0.0
    found = []
    offset = 0
    previous = ""
    for part in text.split(" "):
        if offset:
            offset += 1
        # 'Phone: 5055493103' / 'Fax:3303767416' is a phone number, not an ID.
        if not (_PHONE_LABEL.fullmatch(previous) or _PHONE_LABEL.match(part)):
            for token, start, end in _id_pieces(part):
                if allowed is None or allowed(offset + start, offset + end):
                    found.append((token, offset + start, offset + end))
        offset += len(part)
        if part:
            previous = part
    if not found:
        return "", 0.0
    token, start, end = min(found, key=lambda item: _word_gap(text, key_at, (item[1], item[2])))
    word_gap = _word_gap(text, key_at, (start, end))[0]
    score = 0.0
    for raw in raws or []:
        span = str(raw.get("text") or "")
        if token.casefold() in span.casefold() or token.casefold() in _normalize(span).casefold():
            score = max(score, float(raw.get("score") or 0))
    if score <= 0:
        score = geometry_confidence(word_gap)
    return token, score


def _in_header_row(hit: KeyHit, hits: list[KeyHit]) -> bool:
    """Other keys share the key's line and the next word right of it is one of them (or
    nothing): 'Name  Patient ID  SSN', not the inline 'MRN: 123  DOB: ...'."""
    height = max(hit.box.height(), 1.0)
    own = set(hit.word_indexes)
    row_keys = {
        index
        for other in hits
        if other is not hit and abs(other.box.cy - hit.box.cy) < 0.6 * height
        for index in other.word_indexes
        if index not in own
    }
    if not row_keys:
        return False
    right = [
        word for word in hit.value_words
        if word.index not in own and abs(word.box.cy - hit.box.cy) < 0.6 * height and word.box.left >= hit.box.right - 1
    ]
    return not right or min(right, key=lambda word: word.box.left).index in row_keys


def _column_value(hit: KeyHit, words: list[Word]) -> str:
    """The ID under a table column header ('Patient ID' over 'E2694028'), else ''. A line read
    as one OCR word ('(330) 376-7416 EMA24002442') gives the token under the key's centre."""
    height = max(hit.box.height(), 1.0)
    below = [
        word for word in words
        if 0 <= word.box.top - hit.box.bottom + 0.3 * height < 2.5 * height
        and min(word.box.right, hit.box.right) - max(word.box.left, hit.box.left) > 0
    ]
    if not below:
        return ""
    top = min(word.box.top for word in below)
    found: list[tuple[float, str]] = []
    for word in below:
        if word.box.top - top > height:
            continue
        text = word.content
        per_char = word.box.width() / max(len(text), 1)
        for part in re.finditer(r"\S+", text):
            token = _id_token(part.group())
            if token:
                centre = word.box.left + per_char * (part.start() + part.end()) / 2
                found.append((abs(centre - hit.box.cx), token))
    return min(found)[1] if found else ""


def _wrapped_value(hit: KeyHit, hits: list[KeyHit], words: list[Word]) -> tuple[str, Word] | None:
    """The ID of a key that ends its line, printed at the start of the next line: a long header
    line wrapped ('Patient: Doe, Jane  MRN: 3502776450(EV)  FIN:' / '86013950877').

    Only when the key's line is not a row of bare labels (it holds a digit, or the key has its
    colon) and the ID stands alone at the next line's left margin (or a label follows it)."""
    own = set(hit.word_indexes)
    height = max(hit.box.height(), 1.0)
    line = [word for word in words if abs(word.box.cy - hit.box.cy) < 0.6 * height]
    if not line or any(word.index not in own and word.box.left >= hit.box.right - 1 for word in line):
        return None
    labelled = any(word.content.rstrip().endswith(":") for word in line if word.index in own)
    if not labelled and not any(ch.isdigit() for word in line for ch in word.content):
        return None
    below = [
        word for word in words
        if -0.3 * height < word.box.top - hit.box.bottom < 1.5 * height
        and abs(word.box.cy - hit.box.cy) >= 0.6 * height
    ]
    if not below:
        return None
    first = min(below, key=lambda word: word.box.left)
    if first.box.left > min(word.box.left for word in line) + 2.0 * height:
        return None
    rest = [
        word for word in below
        if word is not first and abs(word.box.cy - first.box.cy) < 0.6 * height and word.box.left > first.box.left
    ]
    if rest:
        after = min(rest, key=lambda word: word.box.left)
        if not any(after.index in other.word_indexes for other in hits):
            return None
    value = _id_token(first.content)
    return (value, first) if value else None


def extract_box(hit: KeyHit, hits: list[KeyHit] | None = None, words: list[Word] | None = None) -> IdHit:
    """One overlay box → one row. Accepted only when value matches ID patterns. A key in a
    table's header row reads its column: the value under it, never the next header. A key that
    ends its line reads the ID wrapped to the start of the next line."""
    if hits and _in_header_row(hit, hits):
        value = _column_value(hit, words or hit.value_words)
        if value:
            return IdHit(
                key=hit.key,
                region=hit.region,
                sentence=hit.value_text,
                ner_text=value,
                value=value,
                score=geometry_confidence(0),
                accepted=True,
                source="column",
            )
    wrapped = _wrapped_value(hit, hits, words) if hits and words else None
    if wrapped:
        value, word = wrapped
        row = IdHit(
            key=hit.key,
            region=hit.region,
            sentence=f"{hit.value_text} {word.content}".strip(),
            ner_text=value,
            value=value,
            score=geometry_confidence(0),
            accepted=True,
            source="wrapped",
        )
        row.value_at = [word]
        return row
    raws = predict(hit.value_text, LABELS)
    key_at = key_span(hit.value_text, hit.value_words, hit.word_indexes)
    value, score = nearest(hit.value_text, key_at, raws, _owned_value(hit, hits) if hits else None)
    if value and not is_member_id_value(value):
        value, score = "", 0.0
    best_text = ""
    best_score = -1.0
    for raw in raws:
        ner_text = str(raw.get("text") or "").strip()
        accepted = accept(ner_text)
        if not value or not accepted or value.casefold() not in accepted.casefold():
            continue
        raw_score = float(raw.get("score") or 0)
        if raw_score > best_score:
            best_text = ner_text
            best_score = raw_score
    if best_text and is_member_id_value(value):
        return IdHit(
            key=hit.key,
            region=hit.region,
            sentence=hit.value_text,
            ner_text=best_text,
            value=value,
            score=best_score,
            accepted=True,
            source="ner",
        )
    if value and is_member_id_value(value):
        return IdHit(
            key=hit.key,
            region=hit.region,
            sentence=hit.value_text,
            ner_text=value,
            value=value,
            score=score,
            accepted=True,
            source="geometry",
        )
    return IdHit(key=hit.key, region=hit.region, sentence=hit.value_text)


def select_per_key(rows: list[IdHit]) -> list[IdHit]:
    """Keep every key occurrence; select the best one per distinct key (header/footer first,
    then higher confidence), and only header/footer ones when the page has any."""
    best: dict[str, IdHit] = {}
    for row in rows:
        if row.accepted and row.value and not is_member_id_value(row.value):
            row.accepted = False
            row.value = ""
        current = best.get(row.key.casefold())
        if current is None or (
            row.accepted,
            region_priority(row.region),
            row.score,
        ) > (
            current.accepted,
            region_priority(current.region),
            current.score,
        ):
            best[row.key.casefold()] = row
    chosen = [row for row in best.values() if row.accepted and row.value and is_member_id_value(row.value)]
    chosen_ids = {id(row) for row in chosen}
    edge_chosen = any(is_edge_region(row.region) for row in chosen)
    for row in rows:
        row.selected = id(row) in chosen_ids and (is_edge_region(row.region) or not edge_chosen)
    return rows


def one_per_value(rows: list[IdHit]) -> list[IdHit]:
    """2026-11-03 (user): two keys that read the same ID ('Acct #' and 'Account No' both
    23-4567) are one ID: only the best of them stays selected (header / footer first, then
    the higher score). Every row is kept; only ``selected`` changes."""
    best: dict[str, IdHit] = {}
    for row in rows:
        if not (row.selected and row.value):
            continue
        value = re.sub(r"[\s\-./]", "", _normalize(row.value)).casefold()
        current = best.get(value)
        if current is None or (region_priority(row.region), float(row.score or 0)) > (
            region_priority(current.region), float(current.score or 0)
        ):
            if current is not None:
                current.selected = False
            best[value] = row
        else:
            row.selected = False
    return rows


def confirmed_repeats(hits: list[KeyHit], rows: list[IdHit], words: list[Word] | None) -> list[IdHit]:
    """Untrusted keys whose ID is one a trusted key already read on the page: a lone mid-page
    '(MRN 20766549)' repeats the header's MRN."""
    known = {_normalize(row.value).casefold() for row in rows if row.accepted and row.value}
    repeats = []
    for hit in hits:
        if hit.trusted or hit.field != "member_id" or not hit.value_text:
            continue
        row = extract_box(hit, hits, words)
        if row.accepted and _normalize(row.value).casefold() in known:
            repeats.append(linked(row, hit))
    return repeats


def extract_page(hits: list[KeyHit], words: list[Word] | None = None) -> list[IdHit]:
    rows = extract_field_keys(hits, "member_id", lambda hit: extract_box(hit, hits, words))
    return one_per_value(select_per_key(rows + confirmed_repeats(hits, rows, words)))
