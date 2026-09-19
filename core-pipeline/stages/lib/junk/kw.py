"""Junk keyword lists from ``stages/lib/keywords.json``."""

from __future__ import annotations

import json
from pathlib import Path

JUNK = json.loads(
    (Path(__file__).resolve().parents[1] / "keywords.json").read_text(encoding="utf-8")
)["junk"]
