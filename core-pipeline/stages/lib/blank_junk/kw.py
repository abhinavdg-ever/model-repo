"""Junk keyword lists from ``stages/lib/keyword-canon/junk_keywords_canon.json``.

Compiled once per file version and reloaded when the file changes — edit the
JSON and the next page uses it. Detectors call :func:`rules` at call time;
nothing is captured at import.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from stages.lib.canon_store import CANON_DIR, CanonFile

CANON_PATH = CANON_DIR / "junk_keywords_canon.json"


@dataclass(frozen=True)
class JunkRules:
    raw: dict[str, Any]
    blank_phrase: re.Pattern[str]
    cover_phrases: tuple[re.Pattern[str], ...]
    cover_single_words: frozenset[str]
    instructions: tuple[re.Pattern[str], ...]
    invoice_keywords: tuple[str, ...]
    invoice_patterns: tuple[re.Pattern[str], ...]
    letter_fax: tuple[re.Pattern[str], ...]
    others_toc: tuple[re.Pattern[str], ...]
    others_emr_toc_title: re.Pattern[str]
    others_emr_toc_markers: tuple[re.Pattern[str], ...]
    record_request_phrases: tuple[str, ...]
    record_request_patterns: tuple[re.Pattern[str], ...]
    signature: re.Pattern[str]


def _icase(patterns: list[str]) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p, re.IGNORECASE) for p in patterns)


def _word_patterns(phrases: list[str]) -> tuple[re.Pattern[str], ...]:
    # Matched against lowercased text, as the ported detectors did.
    return tuple(re.compile(rf"\b{re.escape(kw.lower())}\b") for kw in phrases)


def _build(data: dict[str, Any]) -> JunkRules:
    return JunkRules(
        raw=data,
        blank_phrase=re.compile(data["blank_phrase"], re.IGNORECASE),
        cover_phrases=_icase(data["cover_phrases"]),
        cover_single_words=frozenset(data["cover_single_words"]),
        instructions=_icase(data["instructions"]),
        invoice_keywords=tuple(data["invoice"]),
        invoice_patterns=_word_patterns(data["invoice"]),
        letter_fax=_icase(data["letter_fax"]),
        others_toc=_icase(data["others_toc"]),
        others_emr_toc_title=re.compile(data["others_emr_toc_title"], re.IGNORECASE),
        others_emr_toc_markers=_icase(data["others_emr_toc_markers"]),
        record_request_phrases=tuple(data["record_request"]),
        record_request_patterns=_word_patterns(data["record_request"]),
        signature=re.compile(
            r"\b(" + "|".join(data["signature"]) + r")\b", re.IGNORECASE
        ),
    )


_CANON: CanonFile[JunkRules] = CanonFile(CANON_PATH, _build)


def rules() -> JunkRules:
    return _CANON.get()


def reload() -> None:
    """Force a re-read on next use (tests)."""
    _CANON.reset()
