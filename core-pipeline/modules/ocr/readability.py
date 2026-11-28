"""Readability (2026-11-28, user; was Visibility): ``is_readable`` Yes / No per page.

No when the page-tag model calls the page not visible, or when all three hold:

* Page Quality is Low (the routing's Final Quality);
* the OCR match rate is under 50% (the three engines' when Doc Intel read the page, else
  Docling vs Tesseract);
* Tesseract's text is gibberish.

Gibberish (2026-11-28, user: both tests combined). Tesseract's tokens, numbers / dates /
punctuation left out, are scored twice:

* ``dictionary_share``: tokens in the English word list (pyspellchecker, offline) or in our
  own canon vocabulary (every word in ``resources/canon/*.json``: page keywords, section
  headers, credentials, lab and medication terms ...);
* ``shape_share``: tokens shaped like a word (letters only, a vowel, normal casing).

The page is gibberish when the average of the two is under GIBBERISH_BELOW (50%). Under
MIN_TOKENS tokens nothing is judged (None). Without pyspellchecker the canon words alone are
the dictionary.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any, Optional

GIBBERISH_BELOW = 0.5
MATCH_BELOW = 0.5
MIN_TOKENS = 20
_TOKEN = re.compile(r"[A-Za-z][A-Za-z'\-]*")


def _tokens(text: str) -> list[str]:
    out = []
    for raw in (text or "").split():
        token = raw.strip(".,;:!?()[]{}'\"|_*#")
        if not token or re.fullmatch(r"[\d/\-.:#$%,+]+", token):
            continue
        out.append(token)
    return out


def _shaped(token: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z][a-z]+|[A-Z]{2,}", token) and re.search(r"[aeiouyAEIOUY]", token))


@lru_cache(maxsize=1)
def _canon_words() -> frozenset[str]:
    from modules.canon_store import CANON_DIR

    words: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, str):
            words.update(w.lower() for w in _TOKEN.findall(value) if len(w) > 1)
        elif isinstance(value, dict):
            for key, item in value.items():
                walk(key)
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for path in sorted(CANON_DIR.glob("*.json")):
        try:
            walk(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return frozenset(words)


@lru_cache(maxsize=1)
def _english() -> Any:
    try:
        from spellchecker import SpellChecker

        return SpellChecker(language="en").word_frequency
    except Exception:  # noqa: BLE001 -- the canon words alone then
        return None


def _known(token: str) -> bool:
    word = token.lower().strip("'-")
    if not word:
        return False
    if word in _canon_words():
        return True
    english = _english()
    if english is not None and word in english:
        return True
    # Hyphenated words: every part known.
    parts = [p for p in word.split("-") if p]
    return len(parts) > 1 and all(_known(p) for p in parts)


def gibberish(text: Optional[str]) -> dict[str, Any]:
    """{"tokens", "dictionary_share", "shape_share", "score", "gibberish"}; shares and the verdict
    are None under MIN_TOKENS tokens."""
    tokens = _tokens(text or "")
    out: dict[str, Any] = {"tokens": len(tokens), "dictionary_share": None, "shape_share": None,
                           "score": None, "gibberish": None}
    if len(tokens) < MIN_TOKENS:
        return out
    dictionary = sum(1 for t in tokens if _known(t)) / len(tokens)
    shape = sum(1 for t in tokens if _shaped(t)) / len(tokens)
    score = (dictionary + shape) / 2
    out.update(dictionary_share=round(dictionary, 4), shape_share=round(shape, 4), score=round(score, 4),
               gibberish=score < GIBBERISH_BELOW)
    return out


def is_readable(*, is_visible: Optional[bool], final_quality: Optional[str], match_rate: Optional[float],
                tesseract_gibberish: Optional[bool]) -> tuple[bool, str]:
    """(is_readable, why not)."""
    if is_visible is False:
        return False, "not visible (page-tag model)"
    low = (final_quality or "").strip().lower() == "low"
    bad_match = match_rate is not None and float(match_rate) < MATCH_BELOW
    if low and bad_match and tesseract_gibberish:
        return False, f"Page Quality Low, OCR match {float(match_rate):.0%}, Tesseract gibberish"
    return True, ""


# --------------------------------------------------------------------------- v2

def readability_logic() -> str:
    """READABILITY_LOGIC (2026-11-28, user): v1 (today, the default) | v2 | shadow (v1 writes, v2 recorded)."""
    import os

    value = (os.environ.get("READABILITY_LOGIC") or "v1").strip().lower()
    return value if value in ("v1", "v2", "shadow") else "v1"


def _canon() -> dict[str, Any]:
    from modules.canon_store import CANON_DIR, CanonFile

    global _CANON_FILE
    if _CANON_FILE is None:
        _CANON_FILE = CanonFile(CANON_DIR / "readability_canon.json")
    try:
        return _CANON_FILE.get() or {}
    except Exception:  # noqa: BLE001 - the defaults below then
        return {}


_CANON_FILE = None


def azure_word_confidence(raw: Any) -> Optional[float]:
    """Mean Azure word confidence from a stored Final OCR 2 row (pagesMeta[].words[].confidence)."""
    if not raw:
        return None
    try:
        doc = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    values = [float(w["confidence"]) for page in (doc or {}).get("pagesMeta") or [] if isinstance(page, dict)
              for w in page.get("words") or [] if isinstance(w, dict) and isinstance(w.get("confidence"), (int, float))]
    return round(sum(values) / len(values), 4) if values else None


def image_measures(path: Any, height_px: int = 1600) -> dict[str, Optional[float]]:
    """ink_pct, sharpness (variance of the Laplacian on the inked area) and contrast (95th - 5th
    percentile of grey on the inked area) of the page image scaled to height_px; None when unreadable."""
    try:
        import numpy as np
        from PIL import Image

        with Image.open(path) as image:
            gray = image.convert("L")
            if gray.height and gray.height != height_px:
                gray = gray.resize((max(1, round(gray.width * height_px / gray.height)), height_px))
            a = np.asarray(gray, dtype=np.float32)
    except Exception:  # noqa: BLE001 - an unreadable image fails no image test
        return {"ink_pct": None, "sharpness": None, "contrast": None}
    ink = a < 160
    ink_pct = float(ink.mean() * 100)
    if not ink.any():
        return {"ink_pct": 0.0, "sharpness": None, "contrast": None}
    lap = (a[1:-1, :-2] + a[1:-1, 2:] + a[:-2, 1:-1] + a[2:, 1:-1] - 4 * a[1:-1, 1:-1])
    mask = ink[1:-1, 1:-1]
    rows, cols = np.where(ink)
    box = a[rows.min():rows.max() + 1, cols.min():cols.max() + 1]
    sharp = float(lap[mask].var()) if mask.any() else None
    contrast = float(np.percentile(box, 95) - np.percentile(box, 5))
    return {"ink_pct": round(ink_pct, 3), "sharpness": None if sharp is None else round(sharp, 2),
            "contrast": round(contrast, 2)}


def profile(*, texts: dict[str, Optional[str]], azure_raw: Any, is_visible: Optional[bool],
            visibility_conf: Optional[float], final_quality: Optional[str], match_rate: Optional[float],
            handwritten_pct: Optional[float], image_path: Any = None) -> dict[str, Any]:
    """The readability profile (section 3 of the design): numbers and shares only, no text."""
    from modules.ocr.routing import overlap

    cfg = _canon()
    order = cfg.get("best_engine_order") or ["azuredocintel", "docling", "tesseract"]
    best = next((e for e in order if (texts.get(e) or "").strip()), None)
    best_text = texts.get(best) or "" if best else ""
    g = gibberish(best_text)
    tokens = int(g["tokens"])
    azure_conf = azure_word_confidence(azure_raw) if best == "azuredocintel" and cfg.get("use_azure_word_conf", True) else None
    word_score = None
    if g["dictionary_share"] is not None:
        parts = [g["dictionary_share"], g["shape_share"]] + ([azure_conf] if azure_conf is not None else [])
        word_score = round(sum(parts) / len(parts), 4)
    # OCR agreement: the best text against the other engine closest to it (before the edge cap).
    agreement, pair = None, None
    others = [e for e in order if e != best and (texts.get(e) or "").strip()]
    if best == "azuredocintel":
        candidates = [(overlap(best_text, texts.get(e)), f"azuredocintel vs {e}") for e in others]
    elif best is not None and others:
        candidates = [(overlap(texts.get("docling"), texts.get("tesseract")), "docling vs tesseract")]
    else:
        candidates = []
    candidates = [(v, p) for v, p in candidates if v is not None]
    if candidates:
        agreement, pair = max(candidates)
        agreement = round(float(agreement), 4)
    weights = cfg.get("text_score_weights") or {"word_score": 2, "ocr_agreement": 1}
    if word_score is None:
        text_score = None
    elif agreement is None:
        text_score = word_score
    else:
        ww, wa = float(weights.get("word_score", 2)), float(weights.get("ocr_agreement", 1))
        text_score = round((ww * word_score + wa * agreement) / (ww + wa), 4)
    p_not_visible = None
    if is_visible is not None and visibility_conf is not None:
        p_not_visible = round(float(visibility_conf) if is_visible is False else 1.0 - float(visibility_conf), 4)
    out: dict[str, Any] = {
        "best_engine": best, "tokens": tokens, "dictionary_share": g["dictionary_share"], "shape_share": g["shape_share"],
        "azure_word_conf": azure_conf, "word_score": word_score, "ocr_agreement": agreement, "agreement_pair": pair,
        "text_score": text_score, "match_rate": None if match_rate is None else round(float(match_rate), 4),
        "visible": is_visible, "p_not_visible": p_not_visible, "final_quality": final_quality,
        "handwritten_pct": handwritten_pct,
        "tesseract_word_score": gibberish(texts.get("tesseract")).get("score"),
        "ink_pct": None, "sharpness": None, "contrast": None,
    }
    image = cfg.get("image") or {}
    if tokens < int(cfg.get("min_tokens", 20)) and image.get("enabled", True) and image_path is not None:
        out.update(image_measures(image_path, int(image.get("height_px", 1600))))
    return out


def _band(floor: float, span: float, margin: float) -> float:
    return round(floor + span * max(0.0, min(1.0, margin)), 4)


def decide_v2(p: dict[str, Any], *, blank: bool = False, blank_confidence: Optional[float] = None) -> dict[str, Any]:
    """R0-R5 of the design on a profile: {"is_readable": True / False / None (Not Applicable), "rule",
    "score", "confidence", "review", "reason", "rules": {id: no / fired / not measured}}."""
    cfg = _canon()
    c = cfg.get("confidence") or {}
    tr, ir, fb = c.get("text_rules") or {}, c.get("image_rule") or {}, c.get("fallback") or {}
    t_floor, t_span = float(tr.get("floor", 0.70)), float(tr.get("span", 0.29))
    min_tokens = int(cfg.get("min_tokens", 20))
    good, poor = float(cfg.get("good_text_score", 0.60)), float(cfg.get("poor_text_score", 0.30))
    nv_text, nv_at = float(cfg.get("not_visible_text_score", 0.35)), float(cfg.get("not_visible_at", 0.90))
    agr = cfg.get("agreement") or {}
    disagree_below, low_below = float(agr.get("disagree_below", 0.30)), float(agr.get("low_below", 0.30))
    img = cfg.get("image") or {}
    sharp_below, contrast_below = float(img.get("sharpness_below", 30)), float(img.get("contrast_below", 30))
    full_at = float(img.get("score_full_at", 120))
    ts, tokens, agreement = p.get("text_score"), int(p.get("tokens") or 0), p.get("ocr_agreement")
    sharp, contrast, ink = p.get("sharpness"), p.get("contrast"), p.get("ink_pct")
    rules: dict[str, str] = {}
    enough = tokens >= min_tokens and ts is not None
    score = ts if enough else (None if sharp is None or contrast is None else
                               round(min(1.0, sharp / full_at, contrast / full_at), 4))

    def result(value: Optional[bool], rule: str, confidence: float, reason: str, review: bool) -> dict[str, Any]:
        rules[rule] = "fired"
        review = review or value is False or confidence < float(c.get("review_below", 0.70))
        return {"is_readable": value, "rule": rule, "score": score, "confidence": round(confidence, 4),
                "review": bool(review), "reason": f"{rule}: {reason}", "rules": rules}

    if blank:
        return result(None, "R0", float(blank_confidence if blank_confidence is not None else 1.0),
                      "blank page: Not Applicable", False)
    rules["R0"] = "no"
    engines_disagree = agreement is not None and agreement < disagree_below
    model_disagrees = p.get("visible") is False
    words = f"words {p.get('word_score')}" + (f", Azure {p.get('azure_word_conf')}" if p.get("azure_word_conf") is not None else "")
    agree_txt = "" if agreement is None else f", agreement {agreement:.0%}"
    if not enough:
        rules["R1"] = "not measured"
    elif ts >= good:
        conf_ = _band(t_floor, t_span, (ts - good) / float(tr.get("margin_range", 0.30)))
        if model_disagrees or engines_disagree:
            conf_ = min(conf_, float(c.get("disagree_cap", 0.70)))
        notes = "; model said not visible" if model_disagrees else ""
        notes += "; engines disagree" if engines_disagree else ""
        return result(True, "R1", conf_, f"text score {ts:.2f} ({words}{agree_txt}){notes}",
                      model_disagrees or engines_disagree)
    else:
        rules["R1"] = "no"
    pnv = p.get("p_not_visible")
    if pnv is None:
        rules["R2"] = "not measured"
    elif pnv >= nv_at and (tokens < min_tokens or (ts is not None and ts < nv_text)):
        text_margin = 1.0 if tokens < min_tokens or ts is None else min(1.0, (nv_text - ts) / nv_text)
        margin = (min(1.0, (pnv - nv_at) / max(1e-6, 1 - nv_at)) + text_margin) / 2
        return result(False, "R2", _band(t_floor, t_span, margin),
                      f"visibility model not visible ({pnv:.0%}) and the text is poor", True)
    else:
        rules["R2"] = "no"
    low = (p.get("final_quality") or "").strip().lower() == "low"
    if not enough:
        rules["R3"] = "not measured"
    elif ts < poor and (low or (agreement is not None and agreement < low_below)):
        return result(False, "R3", _band(t_floor, t_span, (poor - ts) / poor),
                      f"text score {ts:.2f} ({words}{agree_txt})", True)
    else:
        rules["R3"] = "no"
    if tokens >= min_tokens:
        rules["R4"] = "no"
    elif sharp is None or contrast is None or not ink:
        rules["R4"] = "not measured"
    elif sharp < sharp_below or contrast < contrast_below:
        margin = max((sharp_below - sharp) / sharp_below, (contrast_below - contrast) / contrast_below)
        return result(False, "R4", _band(float(ir.get("floor", 0.55)), float(ir.get("span", 0.25)), margin),
                      f"{tokens} tokens, sharpness {sharp:.0f}, contrast {contrast:.0f}", True)
    else:
        rules["R4"] = "no"
    # R5: Yes, the review band.
    f_floor, f_span = float(fb.get("floor", 0.50)), float(fb.get("span", 0.20))
    if enough:
        conf_ = _band(f_floor, f_span, (ts - poor) / poor)
    elif sharp is not None and contrast is not None:
        conf_ = _band(f_floor, f_span, (min(sharp, contrast) - sharp_below) / 60)
    else:
        conf_ = float(fb.get("nothing_measured", 0.50))
    mr = p.get("match_rate")
    review = (enough and poor <= ts < good) or engines_disagree or (
        low and mr is not None and mr < float(cfg.get("match_below", 0.50)))
    why = f"text score {ts:.2f}" if enough else f"{tokens} tokens" + (
        f", sharpness {sharp:.0f}" if sharp is not None else "")
    if engines_disagree:
        why += f", engines disagree (agreement {agreement:.0%})"
    return result(True, "R5", conf_, why, review)
