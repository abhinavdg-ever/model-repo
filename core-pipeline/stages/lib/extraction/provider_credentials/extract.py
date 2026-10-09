"""Provider credentials (MD, DO, PA-C, …) from the page text.

The list is ``keyword-canon/provider_credentials_canon.json``. A token counts
when it matches that list and sits after a name on the same line. A two-letter
form that is also a state (MD, PA) is kept only when it is not followed by a
ZIP code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from stages.lib.canon_store import CANON_DIR, CanonFile

from ..util.geometry import Box, Word, group_lines

_ZIP = re.compile(r"\d{5}(?:-\d{4})?$")
# These letters are also USPS state abbreviations.
_STATE_LIKE = frozenset({"md", "pa", "ma", "dc"})


def _norm(text: str) -> str:
    return re.sub(r"[\s.]+", "", text or "").casefold()


def _build(data: dict) -> dict[str, str]:
    """Normalized form -> display form. The first spelling in the file wins."""
    mapping: dict[str, str] = {}
    for item in data.get("credentials") or []:
        text = str(item).strip()
        if text:
            mapping.setdefault(_norm(text), text)
    return mapping


_CANON = CanonFile(CANON_DIR / "provider_credentials_canon.json", _build)


def load_credentials() -> dict[str, str]:
    return _CANON.get()


def _credential_text(token: str) -> bool:
    letters = [ch for ch in token if ch.isalpha()]
    return bool(letters) and any(ch.isupper() for ch in letters)


def _name_before(token: str) -> bool:
    letters = [ch for ch in token if ch.isalpha()]
    return len(letters) >= 2 and letters[0].isupper()


@dataclass
class CredentialHit:
    key: str
    region: str
    sentence: str
    value: str
    score: float = 0.0
    accepted: bool = False
    selected: bool = False
    source: str = ""
    box: Box | None = None


def extract_page(words: list[Word]) -> list[CredentialHit]:
    """Every credential after a name. The first of each spelling is selected."""
    catalog = load_credentials()
    if not catalog:
        return []
    found: list[CredentialHit] = []
    seen: set[str] = set()
    for line in group_lines(words):
        sentence = " ".join(word.content for word in line)
        for index, word in enumerate(line):
            key = _norm(word.content)
            display = catalog.get(key)
            if not display or not _credential_text(word.content):
                continue
            previous = line[index - 1].content if index else ""
            nxt = line[index + 1].content if index + 1 < len(line) else ""
            if not _name_before(previous):
                continue
            if key in _STATE_LIKE and _ZIP.match(nxt.strip(" ,")):
                continue
            selected = display.casefold() not in seen
            seen.add(display.casefold())
            found.append(
                CredentialHit(
                    key="Credential",
                    region="line",
                    sentence=sentence,
                    value=display,
                    score=0.9,
                    accepted=True,
                    selected=selected,
                    source="rules",
                    box=word.box,
                )
            )
    return found
