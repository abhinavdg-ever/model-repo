from __future__ import annotations

import re

from .keys import key_sentences
from .model import predict_entities
from ..rule_based.name_common import is_ignore, is_non_name, name_matches, tokenize

PERSON_LABELS = ["person"]
_CLEAN = re.compile(r"\s+")
_GENERIC = frozenset(
    {
        "patient",
        "the patient",
        "this patient",
        "tne patient",
        "new patient",
        "pt",
        "name",
        "patient name",
        "member name",
        "subscriber name",
        "preferred name",
        "caller name",
        "female",
        "male",
        "adult",
        "physician",
        "doctor",
        "caregiver",
        "patient caregiver",
        "old female",
        "dob",
        "mrn",
    }
)


def _clean(text: str) -> str:
    return _CLEAN.sub(" ", text).strip(" #.,;:|")


def _trim_to_name(name: str) -> str:
    """Strip non-name words from the ends; drop the hit if one is left inside.

    Whatever survives here is treated as a member name and can mark the page as
    carrying a wrong member, which rejects the whole document, so the model
    saying "person" is not enough on its own.

    GLiNER labels plenty of things "person" that are not a member: "the
    patient", "my medical assistant", "patient or family". Trimming rescues a
    real name behind a label prefix without letting a stock phrase through.
    """
    tokens = tokenize(name)
    while tokens and is_non_name(tokens[0]):
        tokens.pop(0)
    while tokens and is_non_name(tokens[-1]):
        tokens.pop()
    if not tokens or any(is_non_name(token) for token in tokens):
        return ""
    return " ".join(tokens)


def _name_token_count(name: str) -> int:
    return sum(
        1
        for token in tokenize(name)
        if token.isalpha() and not is_ignore(token) and not is_non_name(token)
    )


def _is_full_name(name: str) -> bool:
    return 2 <= _name_token_count(name) <= 3


def _keep_hit(name: str) -> bool:
    if len(name) < 2 or not any(char.isalpha() for char in name):
        return False
    return name.casefold() not in _GENERIC


def _merge_person_hits(sentence: str, hits: list[dict]) -> list[tuple[float, str]]:
    spans: list[tuple[int, int, float, str]] = []
    for hit in hits:
        name = _clean(str(hit.get("text") or ""))
        if not _keep_hit(name):
            continue
        start = hit.get("start")
        end = hit.get("end")
        spans.append(
            (
                int(start) if start is not None else -1,
                int(end) if end is not None else -1,
                float(hit.get("score") or 0),
                name,
            )
        )
    spans.sort(key=lambda item: (item[0] if item[0] >= 0 else 10**9, item[1]))
    merged: list[tuple[int, int, float, str]] = []
    for start, end, score, name in spans:
        if not merged:
            merged.append((start, end, score, name))
            continue
        prev_start, prev_end, prev_score, prev_name = merged[-1]
        adjacent = start >= 0 and prev_end >= 0 and start <= prev_end + 3
        both_one = _name_token_count(prev_name) == 1 and _name_token_count(name) == 1
        if adjacent or (both_one and start >= 0 and prev_end >= 0 and start <= prev_end + 8):
            stop = max(prev_end, end)
            piece = _clean(sentence[prev_start:stop]) if prev_start >= 0 and stop > prev_start else ""
            joined = piece if _name_token_count(piece) >= 2 else _clean(f"{prev_name} {name}")
            merged[-1] = (prev_start, stop, max(prev_score, score), joined)
        else:
            merged.append((start, end, score, name))
    out: list[tuple[float, str]] = []
    for _start, _end, score, name in merged:
        trimmed = _trim_to_name(name)
        if trimmed and _is_full_name(trimmed):
            out.append((score, trimmed))
    return out


def name_candidates(ocr_text: str, model_id: str | None = None) -> list[tuple[float, str, str]]:
    """Run NER on each patient-name sentence; return (score, name, key).

    The sentence is real page text around the key ("Patient Name: Robert
    Smith"), not a rebuilt token window, so the model reads it the way it was
    written. Every person the model returns is reported, whether or not it is
    the expected member, so the caller can also spot a wrong member.
    """
    people: list[tuple[float, str, str]] = []
    for key, sentence in key_sentences(ocr_text, "patient_name"):
        for score, name in _merge_person_hits(
            sentence,
            predict_entities(sentence, PERSON_LABELS, model_id=model_id, value_source=key),
        ):
            people.append((score, name, key))
    people.sort(key=lambda item: item[0], reverse=True)
    return people


def pick_name(
    people: list[tuple[float, str, str]],
    first_name: str,
    last_name: str,
    middle_name: str = "",
    name_mode: str = "2",
) -> tuple[str, str]:
    """Best candidate: one that verifies as the member, else the top hit."""
    if not people:
        return "N/A", ""
    for _score, name, key in people:
        if name_matches(name, first_name, last_name, middle_name, name_mode):
            return name, key
    return people[0][1], people[0][2]


def extract_name_ner(
    ocr_text: str,
    first_name: str,
    last_name: str,
    middle_name: str = "",
    name_mode: str = "2",
    model_id: str | None = None,
) -> tuple[str, str]:
    """Return (detected_name, key_used)."""
    people = name_candidates(ocr_text, model_id)
    return pick_name(people, first_name, last_name, middle_name, name_mode)
