"""Read one DOB value from the sentence inside a key's overlay box.

Improvements from Data/examp/dob.csv:
  - Extra keys / OCR spellings (DOB#, Date of Birthe, BirthDate, PATIENT DOB, …)
  - Dotted month dates (26.Nov.1953) via Util.dates
  - Value must normalize to a real calendar date (noise like names/page labels rejected)
  - Keyless DOB in header/footer (of the image or the text's extent) only for a date at least
    two years older than the page's other dates, so visit / print dates are not read as a DOB
  - Similar-key matching comes from Util.keys OCR fuzzy (all fields)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from math import hypot

from ..util.dates import canonical_date, find_dates, normalize_date
from ..util.keys import KeyHit
from ..util.geometry import (
    Box,
    Word,
    edge_band,
    group_lines,
    is_edge_region,
    region_priority,
    union_boxes,
)
from ..util.model import predict
from ..util.tokens import edit_distance, spaced

LABELS = ["date", "date of birth"]
_CLEAN = re.compile(r"\s+")
_HAS_DIGIT = re.compile(r"\d")
_TIME_AFTER = re.compile(r"\s+\d{1,2}:\d{2}\b")
_MIN_AGE_DAYS = 2 * 365
_LONE_MIN_AGE_DAYS = 10 * 365
_FUZZY_DOB = re.compile(r"(?i)d[o0][bg]")
# Patient details that sit beside a keyless birth date on its row.
_SEX = frozenset({"m", "f", "male", "female"})
_ID_LABELS = frozenset({"mrn", "acct", "account", "chart", "fin", "mr", "ssn"})
_ID_VALUE = re.compile(r"[A-Z]{0,5}\d{6,}")
_AGE = re.compile(r"(?i)\b\d{1,3}\s*(?:y|yo|y/o|y\.o\.?|yr|yrs|years?)\b|[(<]\s*\d{1,3}\s*\)|\bage\b")
_PHONE = re.compile(r"\(?\d{3}\)?[\s.-]*\d{3}-\d{4}")
_PERSON_MIN = 0.5
# Reject obvious non-DOB phrases that sometimes sit near dates.
_NOISE = re.compile(
    r"(?i)\b("
    r"page|pg|pago|of\s+\d+|visit|encounter|appointment|dos\b|printed"
    r")\b"
)


@dataclass
class DobHit:
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


def is_dob_value(text: str) -> bool:
    """True when text is (or contains) a normalizable calendar date, not page/name noise."""
    return bool(accept_dob_value(text))


def accept_dob_value(text: str) -> str:
    cleaned = _CLEAN.sub(" ", (text or "")).strip(" #.,;:|")
    if not cleaned or not _HAS_DIGIT.search(cleaned):
        return ""
    if _NOISE.search(cleaned) and not find_dates(cleaned):
        return ""
    matches = find_dates(cleaned)
    if not matches:
        return ""
    # Prefer the first date span; require normalize success.
    value = normalize_date(matches[0].group(1))
    return value


def _accept(text: str) -> str:
    """Legacy NER helper — keep digit-bearing spans that contain a date."""
    cleaned = _CLEAN.sub(" ", text).strip(" #.,;:|")
    if not cleaned or not _HAS_DIGIT.search(cleaned):
        return ""
    if accept_dob_value(cleaned):
        return cleaned
    return cleaned if find_dates(cleaned) else ""


def _key_span(sentence: str, key: str) -> tuple[int, int] | None:
    found = re.search(re.escape(key), sentence, flags=re.IGNORECASE)
    if found:
        return found.start(), found.end()
    compact = re.sub(r"[^a-z0-9]", "", key.casefold())
    if compact in {"dob", "dobage", "dobagesex"} or compact.startswith("dob"):
        fuzzy = _FUZZY_DOB.search(sentence)
        if fuzzy:
            return fuzzy.start(), fuzzy.end()
    return None


def _gap(sentence: str, key_at: tuple[int, int], date_at: tuple[int, int]) -> tuple[int, int, int]:
    """Word gap, then a penalty if a time follows the date, then character gap."""
    key_start, key_end = key_at
    date_start, date_end = date_at
    if date_end <= key_start:
        middle = sentence[date_end:key_start]
        char_gap = key_start - date_end
    elif date_start >= key_end:
        middle = sentence[key_end:date_start]
        char_gap = date_start - key_end
    else:
        middle = ""
        char_gap = 0
    followed_by_time = 1 if _TIME_AFTER.match(sentence[date_end:]) else 0
    return len(middle.split()), followed_by_time, char_gap


def nearest_date(sentence: str, key: str, raws: list[dict] | None = None) -> tuple[str, float]:
    """Date closest to the key (numeric or month-name). Time after the date loses a tie."""
    text = sentence or ""
    key_at = _key_span(text, key)
    dates = find_dates(text)
    if not dates or key_at is None:
        return "", 0.0
    chosen = min(dates, key=lambda match: _gap(text, key_at, (match.start(), match.end())))
    value = normalize_date(chosen.group(1))
    if not value or not is_dob_value(value):
        return "", 0.0
    score = 0.0
    for raw in raws or []:
        span = str(raw.get("text") or "")
        if value.casefold() in span.casefold() or chosen.group(0).casefold() in span.casefold():
            score = max(score, float(raw.get("score") or 0))
    if score <= 0:
        score = max(0.55, 0.92 - 0.12 * _gap(text, key_at, (chosen.start(), chosen.end()))[0])
    return value, round(score, 4)


def extract_box(hit: KeyHit) -> DobHit:
    """One overlay box produces one row. Accepted only for real date values."""
    raws = predict(hit.value_text, LABELS)
    value, score = nearest_date(hit.value_text, hit.key, raws)
    if value and not is_dob_value(value):
        value, score = "", 0.0
    best_text = ""
    best_score = -1.0
    for raw in raws:
        ner_text = str(raw.get("text") or "").strip()
        accepted = _accept(ner_text)
        if not value or not accepted:
            continue
        if value.casefold() not in accepted.casefold() and accepted.casefold() not in value.casefold():
            continue
        raw_score = float(raw.get("score") or 0)
        if raw_score > best_score:
            best_text = ner_text
            best_score = raw_score
    if best_text and is_dob_value(value):
        return DobHit(
            key=hit.key,
            region=hit.region,
            sentence=hit.value_text,
            ner_text=best_text,
            value=value,
            score=best_score,
            accepted=True,
            source="ner",
        )
    if value and is_dob_value(value):
        return DobHit(
            key=hit.key,
            region=hit.region,
            sentence=hit.value_text,
            ner_text=value,
            value=value,
            score=score,
            accepted=True,
            source="geometry",
        )
    return DobHit(key=hit.key, region=hit.region, sentence=hit.value_text)


def _band_sentence(words: list[Word], page_h: float, *, header: bool) -> tuple[str, str]:
    if page_h <= 0 or not words:
        return "", "mid"
    text = union_boxes([word.box for word in words])
    wanted = "header" if header else "footer"
    band = [word for word in words if edge_band(word.box, page_h, text) == wanted]
    band.sort(key=lambda item: (item.box.cy, item.box.left))
    sentence = " ".join(word.content for word in band).strip()
    region = "keyless_header" if header else "keyless_footer"
    return sentence, region


def _as_date(value: str) -> date | None:
    try:
        return date.fromisoformat(canonical_date(value))
    except ValueError:
        return None


def _old_enough(born: date, page_dates: list[date]) -> bool:
    """A keyless date is a birth date only when it is years before the page's own dates
    (visit, print, signature). With no other date on the page it must be a decade old: a
    scanned record can be years old, so its visit date is often years before today."""
    others = [day for day in page_dates if day != born]
    if others:
        return (max(others) - born).days >= _MIN_AGE_DAYS
    return (date.today() - born).days >= _LONE_MIN_AGE_DAYS


def page_dates(words: list[Word]) -> list[date]:
    """Every calendar date printed on the page."""
    text = " ".join(word.content for word in words)
    return [day for day in (_as_date(match.group(1)) for match in find_dates(text)) if day]


def is_birth_date(raw: str, dates: list[date]) -> bool:
    """A date that can be a birth date by age alone: years before the page's other dates."""
    born = _as_date(normalize_date(raw))
    return born is not None and _old_enough(born, dates)


def _is_birth_label(part: str) -> bool:
    """'dob', 'birth', 'birthday', 'dobsex', 'born' and their OCR misreads ('binth', 'oob', 'd0b')."""
    if part.startswith(("birth", "dob", "born")) or part in {"oob", "d0b", "bday"}:
        return True
    return len(part) >= 4 and edit_distance(part, "birth") <= 1


def _demographic(before: list[Word], after: list[Word]) -> int:
    """How strongly the words around a keyless date on its row mark it as the patient's birth
    date: 2 for a birth label printed before it (even misread: 'OOB:', 'Date of Binth'), 1 for
    other patient details (sex, age, phone, a chart/account label or number, a person name), 0
    for none. A problem-list or medication date ('(01/14/2014) Hyperlipidemia') has none."""
    if any(_is_birth_label(part) for word in before for part in re.findall(r"[a-z0-9]+", word.content.casefold())):
        return 2
    near = before + after
    text = " ".join(word.content for word in near)
    for word in near:
        for part in re.findall(r"[a-z0-9]+", word.content.casefold()):
            if part in _SEX or part.startswith("sex") or part in _ID_LABELS or _is_birth_label(part):
                return 1
        if _ID_VALUE.fullmatch(word.content.strip(",.;:()")):
            return 1
    if _AGE.search(text) or _PHONE.search(text):
        return 1
    return int(_person_touches(before, at_end=True) or _person_touches(after, at_end=False))


def _full_year(value: str) -> bool:
    """A birth date is printed with its four-digit year; '04/18/11' or '10/04/20' is some other date."""
    return bool(re.search(r"(?<!\d)\d{4}(?!\d)", value))


def _person_touches(words: list[Word], *, at_end: bool) -> bool:
    """A person name of two words or more that ends right before the date ('MariaAvila 08/13/1996')
    or starts right after it ('12/25/1980 Park, Charles'). A name further off on the row is
    someone else's ('Name: Diana. Marital Status: Married., 10/31/2022')."""
    text = spaced(" ".join(word.content for word in words))
    if not text:
        return False
    for raw in predict(text, ["person"]):
        start, end = raw.get("start"), raw.get("end")
        if float(raw.get("score") or 0) < _PERSON_MIN or not isinstance(start, int) or not isinstance(end, int):
            continue
        # Two printed words, each capitalized ('Weli-balanced' is one word, not a name).
        if len(re.findall(r"(?:^|[\s,])[A-Z][A-Za-z'.]+(?=$|[\s,])", text[start:end])) < 2:
            continue
        rest = text[end:] if at_end else text[:start]
        if not re.search(r"[A-Za-z0-9]", rest):
            return True
    return False


def extract_keyless_edge_dates(words: list[Word], page_h: float) -> list[DobHit]:
    """Keyless DOB: a header/footer date years older than the page's other dates, with patient
    details beside it on its row (see _demographic), a four-digit year, and no clock time after it
    (a visit or print stamp). A date after a birth label comes first, then the oldest, so that is
    the one selected."""
    found: list[tuple[int, date, DobHit]] = []
    seen: set[str] = set()
    dates_on_page = page_dates(words)
    text_box = union_boxes([word.box for word in words]) if words else None
    for header in (True, False):
        sentence, region = _band_sentence(words, page_h, header=header)
        wanted = "header" if header else "footer"
        band = [word for word in words if page_h > 0 and edge_band(word.box, page_h, text_box) == wanted]
        for row in group_lines(band):
            row_text, starts = "", []
            for word in row:
                starts.append(len(row_text) + (1 if row_text else 0))
                row_text = f"{row_text} {word.content}" if row_text else word.content
            for match in find_dates(row_text):
                value = normalize_date(match.group(1))
                if not value or not is_dob_value(value) or value.casefold() in seen or not _full_year(value):
                    continue
                born = _as_date(value)
                if born is None or not _old_enough(born, dates_on_page):
                    continue
                if _TIME_AFTER.match(row_text[match.end():]):
                    continue
                first = max(at for at, start in enumerate(starts) if start <= match.start())
                last = max(at for at, start in enumerate(starts) if start < match.end())
                strength = _demographic(row[max(0, first - 4) : first], row[last + 1 : last + 4])
                if not strength:
                    continue
                seen.add(value.casefold())
                found.append(
                    (
                        -strength,
                        born,
                        DobHit(
                            key="No Key Header" if header else "No Key Footer",
                            region=region,
                            sentence=sentence,
                            ner_text=match.group(0),
                            value=value,
                            score=0.7,
                            accepted=True,
                            source="keyless_edge",
                        ),
                    )
                )
    found.sort(key=lambda item: (item[0], item[1]))
    return [row for _, _, row in found]


_DATE_KEY_FIELDS = frozenset({"dob", "dos"})


def _box_gap(left: Box, right: Box) -> float:
    """Distance between the nearest edges of two boxes (0 when they overlap)."""
    dx = max(0.0, max(left.left, right.left) - min(left.right, right.right))
    dy = max(0.0, max(left.top, right.top) - min(left.bottom, right.bottom))
    return hypot(dx, dy)


def _reaches(key: Box, value: Box) -> bool:
    """The value sits where a key's value can be: further along the key's row, or on one of the
    next two rows (anywhere across). Never above the key."""
    line_h = max(key.height(), 1.0)
    dy = value.cy - key.cy
    if dy < -0.5 * line_h or dy > 2.5 * line_h:
        return False
    return not (abs(dy) <= 0.5 * line_h and value.left < key.right)


def _nearest_below(row: DobHit, hit: KeyHit, words: list[Word], hits: list[KeyHit]) -> None:
    """A DOB key whose box holds no date: its value printed further along its row, or on the next
    two rows wherever they start ('Age: 59 Years DOB:' over '07/24/1984 Sex: Female'). The
    nearest date wins, unless another date key (DOS, a second DOB) that can reach it is nearer."""
    dates_on_page = page_dates(words)
    others = [other.box for other in hits if other is not hit and other.field in _DATE_KEY_FIELDS]
    best: tuple[float, Word, str] | None = None
    for word in words:
        if word.index in hit.word_indexes or not _reaches(hit.box, word.box):
            continue
        match = next(iter(find_dates(word.content)), None)
        value = normalize_date(match.group(1)) if match else ""
        born = _as_date(value) if value else None
        if born is None or not is_dob_value(value) or not _full_year(value) or not _old_enough(born, dates_on_page):
            continue
        if _TIME_AFTER.match(word.content[match.end():]):
            continue
        distance = _box_gap(hit.box, word.box)
        if any(_reaches(box, word.box) and _box_gap(box, word.box) < distance for box in others):
            continue
        if best is None or distance < best[0]:
            best = (distance, word, value)
    if best is None:
        return
    _, word, value = best
    row.sentence = f"{row.sentence} | {word.content}".strip(" |")
    row.ner_text, row.value, row.score = word.content, value, 0.75
    row.accepted, row.source = True, "geometry_below"


def extract_page(hits: list[KeyHit], words: list[Word], page_w: float, page_h: float) -> list[DobHit]:
    """Keyed DOB at every DOB key, then keyless header/footer dates if no key gave one, then (no
    date yet) a DOB key's value printed outside its box (_nearest_below)."""
    del page_w
    from ..util.keys import extract_field_keys

    rows = extract_field_keys(hits, "dob", extract_box)
    if not any(row.accepted and row.value for row in rows):
        rows.extend(extract_keyless_edge_dates(words, page_h))
    if not any(row.accepted and row.value for row in rows):
        for row in rows:
            if not row.accepted and row.key_hit is not None:
                _nearest_below(row, row.key_hit, words, hits)
    mark_selected(rows)
    return rows


def mark_selected(rows: list[DobHit]) -> None:
    """Prefer header/footer hits; only then mid. Highest score breaks a tie."""
    accepted = [row for row in rows if row.accepted and row.value and is_dob_value(row.value)]
    if not accepted:
        return
    edge = [row for row in accepted if is_edge_region(row.region)]
    pool = edge if edge else accepted
    counts: dict[str, int] = {}
    for row in pool:
        counts[row.value.casefold()] = counts.get(row.value.casefold(), 0) + 1
    best_count = max(counts.values())
    tied = [row for row in pool if counts[row.value.casefold()] == best_count]
    winner = max(tied, key=lambda row: (region_priority(row.region), row.score, -len(row.value)))
    winner.selected = True
