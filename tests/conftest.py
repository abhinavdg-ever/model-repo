"""Shared test setup.

The pipeline modules are imported the way the services import them —
``core-pipeline`` on sys.path, with ``module`` for the ported libraries.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE = REPO_ROOT / "core-pipeline"
LIB = CORE / "modules"
REVIEW_BACKEND = REPO_ROOT / "review-ui" / "backend"

for path in (CORE, LIB, REVIEW_BACKEND):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


# training/ lives on the developer's machine only (removed from GitHub 2026-11-10, user). A test
# that reads it is skipped when it is not there, e.g. on a fresh clone.
TRAINING = REPO_ROOT / "training"


def pytest_collection_modifyitems(config, items):
    if TRAINING.is_dir():
        return
    import inspect

    import pytest

    skip = pytest.mark.skip(reason="training/ is not in this checkout (kept on the developer's machine)")
    for item in items:
        try:
            source = inspect.getsource(item.function)
            if item.cls is not None:
                source += inspect.getsource(item.cls)
        except (OSError, TypeError):
            continue
        if '"training"' in source or "TRAINER" in source:
            item.add_marker(skip)
