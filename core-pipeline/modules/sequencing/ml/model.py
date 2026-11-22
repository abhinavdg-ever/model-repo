"""The sequencing pair model in the pipeline (Brief 2 Q2): log P(page B comes right after page A)
from the order clues (``clues.py``), LightGBM + isotonic calibration, for ``repair.py``.

The trained files are in ``SEQUENCING_MODEL_DIR`` (default ``models/sequencing/pair_model``: model.txt,
calibration.json, settings.json; written by training/sequencing/clue-model/train.py). Missing folder
or lightgbm: ``status()`` says why and the stage keeps today's rules.
"""
from __future__ import annotations

import json
import math
import threading
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from modules.continuity.ml.model import calibrate
from modules.sequencing.ml import clues as order_clues
from modules.sequencing.ml.repair import PageInfo

FILES = ("model.txt", "calibration.json", "settings.json")
_lock = threading.Lock()
_loaded: Optional[dict[str, Any]] = None
_failed: Optional[str] = None


def model_dir() -> Path:
    from common.config import SEQUENCING_MODEL_DIR

    return Path(SEQUENCING_MODEL_DIR)


def load(folder: Optional[Path] = None) -> Optional[dict[str, Any]]:
    global _loaded, _failed
    if folder is None and (_loaded is not None or _failed is not None):
        return _loaded
    with _lock:
        where = Path(folder) if folder else model_dir()
        missing = [f for f in FILES if not (where / f).is_file()]
        if missing or not find_spec("lightgbm"):
            _failed = f"no sequencing pair model at {where} (missing {', '.join(missing)})" if missing \
                else "lightgbm not installed"
            return None
        import lightgbm as lgb

        loaded = {"booster": lgb.Booster(model_file=str(where / "model.txt")),
                  "calib": json.loads((where / "calibration.json").read_text(encoding="utf-8")),
                  "settings": json.loads((where / "settings.json").read_text(encoding="utf-8"))}
        if folder is None:
            _loaded = loaded
        return loaded


def status() -> dict[str, Any]:
    loaded = load()
    return {"ready": loaded is not None, "reason": None if loaded else _failed, "path": str(model_dir())}


def page_info(text: str, dos: str = "") -> PageInfo:
    return PageInfo(number=order_clues.page_no(text), fax=order_clues.fax_no(text), dos=dos or "")


def scorer(texts: Sequence[str], loaded: Optional[dict[str, Any]] = None,
           extra: Optional[Callable[[int, int], Optional[float]]] = None) -> Callable[[int, int], float]:
    """log P(page j comes right after page i) for the pages' texts (scan order), cached. ``extra``:
    a text-model score to use in place of the clue model where it has one (Q3, later)."""
    loaded = loaded or load()
    if loaded is None:
        raise RuntimeError(_failed or "no sequencing pair model")
    feats = loaded["settings"]["features"]
    pages = [order_clues.opage({"text": t}) for t in texts]
    records = [{"text": t} for t in texts]
    cache: dict[tuple[int, int], float] = {}

    def score(i: int, j: int) -> float:
        key = (i, j)
        if key not in cache:
            p = extra(i, j) if extra else None
            if p is None:
                row = order_clues.order_feats(records[i], records[j], pages[i], pages[j])
                raw = loaded["booster"].predict([[float(row[f]) for f in feats]])[0]
                p = calibrate([raw], loaded["calib"])[0]
            cache[key] = math.log(min(1 - 1e-6, max(1e-6, float(p))))
        return cache[key]

    return score
