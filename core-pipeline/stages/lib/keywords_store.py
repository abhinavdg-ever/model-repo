"""Load shared keyword lists from ``keywords.json`` (next to this module)."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

KEYWORDS_PATH = Path(__file__).resolve().with_name("keywords.json")


@lru_cache(maxsize=1)
def load() -> dict[str, Any]:
    return json.loads(KEYWORDS_PATH.read_text(encoding="utf-8"))


def section(*keys: str) -> Any:
    node: Any = load()
    for key in keys:
        node = node[key]
    return node
