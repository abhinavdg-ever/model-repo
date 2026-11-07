"""Words that are never part of a person's name, shared by the member name and the signer name.

The list lives in Util/not_name_words.txt (one word per line) so it can grow without code changes.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_PATH = Path(__file__).with_name("not_name_words.txt")


@lru_cache(maxsize=1)
def not_name_words() -> frozenset[str]:
    if not _PATH.is_file():
        return frozenset()
    words = set()
    for raw in _PATH.read_text(encoding="utf-8").splitlines():
        word = raw.split("#", 1)[0].strip().casefold()
        if word:
            words.add(word)
    return frozenset(words)


def is_not_name(text: str) -> bool:
    """A printed word ('Provider', 'ATTENDING', 'Physician:') that is never part of a name."""
    return text.strip(" ,;:.()[]").casefold() in not_name_words()
