from __future__ import annotations

import json
import re
from pathlib import Path

from ..rule_based.name_common import is_ignore, is_label, tokenize

HERE = Path(__file__).resolve().parent
# Shared keyword file: stages/lib/keywords.json
KEYWORDS_PATH = HERE.parents[3] / "keywords.json"

_GROUPS: dict[str, list[str]] | None = None


def load_key_groups() -> dict[str, list[str]]:
    global _GROUPS
    if _GROUPS is None:
        data = json.loads(KEYWORDS_PATH.read_text(encoding="utf-8"))
        groups = (data.get("member") or {}).get("key_groups") or {}
        _GROUPS = {name: list(group.get("keys") or []) for name, group in groups.items()}
    return _GROUPS


def keys_for(group: str) -> list[str]:
    return load_key_groups().get(group, [])


# Rebuilding text from tokens strips every punctuation mark and splits dates
# into digits ("Patient Name Robert Smith DOB 1 15 1980"). NER is much weaker
# on that than on the real line, so a sentence is cut out of the original text
# instead and the model reads the field the way it was written.

# What ends a sentence in chart text: a newline, a run of spaces (OCR uses
# those as a column separator when several fields share one line), a full stop
# or a pipe.
_SENTENCE_BREAK = re.compile(r"[\n\r]|[ \t]{2,}|(?<=[a-z0-9])\.\s|[;|]")
_WHITESPACE = re.compile(r"\s+")
_MAX_LEFT_CHARS = 60
_MAX_RIGHT_CHARS = 160
_MIN_VALUE_WORDS = 2
_MAX_REACHES = 2

_KEY_PATTERNS: dict[str, list[tuple[str, "re.Pattern[str]"]]] = {}


def _key_pattern(key: str) -> "re.Pattern[str]":
    """Match the key allowing OCR punctuation/spacing between its words."""
    words = [re.escape(word) for word in tokenize(key.replace(".", " "))]
    if not words:
        return re.compile(r"(?!)")
    joined = r"[^A-Za-z0-9]{0,4}".join(words)
    return re.compile(rf"(?<![A-Za-z0-9]){joined}(?![A-Za-z0-9])", re.IGNORECASE)


def key_patterns(group: str) -> list[tuple[str, "re.Pattern[str]"]]:
    if group not in _KEY_PATTERNS:
        _KEY_PATTERNS[group] = [(key, _key_pattern(key)) for key in keys_for(group)]
    return _KEY_PATTERNS[group]


def _line_bounds(text: str, start: int, end: int) -> tuple[int, int]:
    left = start
    for match in _SENTENCE_BREAK.finditer(text, 0, start):
        left = match.end()
    right = len(text)
    match = _SENTENCE_BREAK.search(text, end)
    if match is not None:
        right = match.start()
    return left, right


def _value_words(text: str) -> int:
    """Count name-looking words in a key's value (labels and initials aside)."""
    return sum(
        1
        for token in tokenize(text)
        if token.isalpha() and len(token) > 1 and not is_label(token) and not is_ignore(token)
    )


def key_sentences(ocr_text: str, group: str) -> list[tuple[str, str]]:
    """Return (key, sentence) for each key of the group found in the page.

    The sentence is real page text -- punctuation kept -- bounded by the line
    the key sits on. A run of spaces normally ends it, because OCR uses that as
    a column separator, but a value too short to be a name reaches across the
    gap: that covers both a header with the value on the next line and a name
    written with wide gaps between its parts ("Robert  Smith").
    """
    text = ocr_text or ""
    if not text.strip():
        return []
    out: list[tuple[str, str]] = []
    seen: set[tuple[int, int]] = set()
    for key, pattern in key_patterns(group):
        for match in pattern.finditer(text):
            left, right = _line_bounds(text, match.start(), match.end())
            # Reach across a gap while the value is too short to be a name: a
            # header with the value on the next line ("PATIENT NAME\nRobert
            # Smith"), or a name split by wide gaps ("Robert  Smith").
            for _ in range(_MAX_REACHES):
                if _value_words(text[match.end() : right]) >= _MIN_VALUE_WORDS:
                    break
                probe = right
                while probe < len(text) and text[probe].isspace():
                    probe += 1
                if probe >= len(text):
                    right = len(text)
                    break
                nxt = _SENTENCE_BREAK.search(text, probe)
                right = nxt.start() if nxt is not None else len(text)
            left = max(left, match.start() - _MAX_LEFT_CHARS)
            right = min(right, match.end() + _MAX_RIGHT_CHARS)
            span = (left, right)
            if span in seen:
                continue
            seen.add(span)
            sentence = _WHITESPACE.sub(" ", text[left:right]).strip(" \t.,;:|-_")
            if sentence:
                out.append((key, sentence))
    return _drop_contained(out)


def _drop_contained(found: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Keep the widest sentence per site so NER runs once, not once per key.

    "patient name", "patient" and "name" all match the same line, and their
    sentences nest; only the longest carries any extra text.
    """
    kept: list[tuple[str, str]] = []
    for key, sentence in sorted(found, key=lambda item: len(item[1]), reverse=True):
        if any(sentence in other for _key, other in kept):
            continue
        kept.append((key, sentence))
    return kept
