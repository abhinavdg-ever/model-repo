"""The continuity clue model in the pipeline (Brief 3 section 3A): one decision per meeting point.

  1. Removed pages: blank / junk / duplicate pages are out; ``gap`` counts them.
  2. Hard rules (fixed, ``decided_by = decisive``): "page N of N" then "page 1 of M" -> break; a
     visit header on page n+1 with a date of service that differs -> break; "page k of N" then
     "page k+1 of N" with the same running header -> continue.
  3. Visual page rule (``visual_continues``): a visual page n+1 with no hard start -> continue.
  4-6. Clues (``clues.py``) -> LightGBM -> isotonic calibration -> P(break).
  7. The whole-chart pass (``viterbi.py``) over every point, the fixed points held.
  8. Output per point: break, ``p_break``, ``score = 2 ln(p / (1 - p))``, ``decided_by``
     (decisive | visual_continues | model), ``evidence`` = the three clues that pushed most.

The trained files are in ``CONTINUITY_MODEL_DIR`` (default ``models/continuity/clue_model``:
model.txt, calibration.json, settings.json, run_card.json; written by
training/continuity/clue-model/train.py). Missing folder or lightgbm: ``status()`` says why and
the stage keeps the rules engine's answer.
"""
from __future__ import annotations

import json
import logging
import math
import threading
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Optional, Sequence

from modules.continuity.ml import clues, viterbi
from modules.continuity.ml.viterbi import DECISIVE, VISUAL, fixed_decision

logger = logging.getLogger(__name__)

FILES = ("model.txt", "calibration.json", "settings.json")
MODEL = "model"
_lock = threading.Lock()
_loaded: Optional[dict[str, Any]] = None
_failed: Optional[str] = None

# Evidence words per clue: (word when it points to a break, word when it points to continue).
WORDS = {
    "sig_tail_a": "signed near end of text",
    "sig_before_next_section": "signature then another section",
    "addl_doc_head_b": "next page opens Additional Documentation",
    "pageof_seq": "page k of N follows",
    "pageof_a_last": "last page of N",
    "pageof_b_first": "page 1 of N next",
    "enc_head_b": "new visit header",
    "dos_conflict": "different dates of service",
    "dos_overlap": "same date of service",
    "header_jacc": "same running header",
    "footer_jacc": "same footer",
    "body_jacc": "similar text",
    "same_L1": "same page type",
    "type_change_weak": "type change, other type in top 5",
    "title_head_b": "title at the top of the next page",
    "cont_head_b": "continued at the top",
    "visual_b": "next page is visual",
    "p_break_text": "the text reads as a new document",
    "id_conflict": "different visit / account ids",
    "id_overlap": "same visit / account ids",
}


def model_dir() -> Path:
    from common.config import CONTINUITY_MODEL_DIR

    return Path(CONTINUITY_MODEL_DIR)


def _load() -> Optional[dict[str, Any]]:
    global _loaded, _failed
    if _loaded is not None or _failed is not None:
        return _loaded
    with _lock:
        if _loaded is not None or _failed is not None:
            return _loaded
        folder = model_dir()
        missing = [f for f in FILES if not (folder / f).is_file()]
        if missing:
            _failed = f"no continuity clue model at {folder} (missing {', '.join(missing)})"
            return None
        if not find_spec("lightgbm"):
            _failed = "lightgbm not installed"
            return None
        import lightgbm as lgb

        try:
            booster = lgb.Booster(model_file=str(folder / "model.txt"))
            calib = json.loads((folder / "calibration.json").read_text(encoding="utf-8"))
            settings = json.loads((folder / "settings.json").read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - a bad model must not sink the chart
            _failed = f"continuity clue model at {folder} failed to load: {str(exc).splitlines()[0][:200]}"
            logger.error("Continuity ML: %s", _failed)
            return None
        _loaded = {"booster": booster, "calib": calib, "settings": settings}
        return _loaded


def status() -> dict[str, Any]:
    loaded = _load()
    if loaded is None:
        return {"ready": False, "reason": _failed, "path": str(model_dir())}
    s = loaded["settings"]
    return {"ready": True, "path": str(model_dir()), "threshold": s.get("threshold"), "features": len(s["features"])}


def calibrate(raw: Sequence[float], calib: dict[str, list[float]]) -> list[float]:
    """The isotonic step function (x / y thresholds), linear between them, clipped at the ends."""
    xs, ys = calib["x"], calib["y"]
    out = []
    for r in raw:
        if r <= xs[0]:
            out.append(ys[0])
        elif r >= xs[-1]:
            out.append(ys[-1])
        else:
            hi = next(i for i, x in enumerate(xs) if x >= r)
            lo = hi - 1
            span = xs[hi] - xs[lo]
            out.append(ys[lo] + (ys[hi] - ys[lo]) * ((r - xs[lo]) / span if span else 0.0))
    return out


def _value(v: Any) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return math.nan
    return f


def decide(pages: Sequence[dict[str, Any]], gaps: Optional[dict[int, int]] = None) -> Optional[list[dict[str, Any]]]:
    """One decision per meeting point of a chart's content pages (page records as clues.py takes
    them, in order); None when the model is not ready."""
    loaded = _load()
    if loaded is None or len(pages) < 2:
        return None if loaded is None else []
    settings = loaded["settings"]
    feats = settings["features"]
    rows = clues.chart_rows(pages, gaps)
    if "p_break_text" in feats:
        # The page-pair text model (Brief 3 E3): one more clue; NaN when it is not installed.
        from modules.continuity.ml import text_model

        scored = text_model.score([(pages[i].get("text") or "", pages[i + 1].get("text") or "")
                                   for i in range(len(rows))])
        for row, s in zip(rows, scored or [None] * len(rows)):
            row["p_break_text"] = s[0] if s else math.nan
    X = [[_value(r.get(f)) for f in feats] for r in rows]
    booster = loaded["booster"]
    raw = list(booster.predict(X))
    contrib = booster.predict(X, pred_contrib=True)
    p = calibrate(raw, loaded["calib"])
    fixed = [fixed_decision(r) for r in rows]
    breaks = viterbi.decode(p, float(settings["threshold"]), [f[0] if f else None for f in fixed],
                            float(settings.get("prior", 0.0)))
    out = []
    for i, (row, brk) in enumerate(zip(rows, breaks)):
        pi = min(1 - 1e-6, max(1e-6, float(p[i])))
        top = sorted(((feats[j], float(c)) for j, c in enumerate(contrib[i][:-1]) if c), key=lambda kv: -abs(kv[1]))[:3]
        words = "; ".join(f"{WORDS.get(name, name)} ({'+' if c > 0 else '-'})" for name, c in top)
        out.append({
            "break": bool(brk), "p_break": round(pi, 4), "score": round(2 * math.log(pi / (1 - pi)), 3),
            "decided_by": fixed[i][1] if fixed[i] else MODEL,
            "evidence": fixed[i][2] if fixed[i] else words,
        })
    return out
