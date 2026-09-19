"""Shared test setup.

The pipeline modules are imported the way the services import them —
``core-pipeline`` on sys.path, with ``stages/lib`` for the ported libraries.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE = REPO_ROOT / "core-pipeline"
LIB = CORE / "stages" / "lib"
REVIEW_BACKEND = REPO_ROOT / "review-ui" / "backend"

for path in (CORE, LIB, REVIEW_BACKEND):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
