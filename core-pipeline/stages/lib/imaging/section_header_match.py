"""Semantic filter for OCR ``section_headers`` (Final1 / Final2 JSON).

Used by the standalone ``section_headers`` stage (and best-effort during OCR)
to keep candidates ≥ threshold vs ``section_header_canon.json``. Docling (or
Azure lines) propose heading candidates; only candidates whose text is ≥
``SECTION_HEADER_SEMANTIC_THRESHOLD`` similar to a known clinical section
header are kept — primarily by normalized lexical match against
``section_header_canon.json``. MiniLM embeddings are an optional assist for
near-paraphrases, gated so unrelated bold labels cannot pass on cosine alone.

The catalog reloads when the JSON file's mtime changes (edit the list → next
filter call picks it up). Re-run ``only=["section_headers"]`` to rewrite
``*_final1.json`` / ``*_final2.json`` without OCR.

Each kept header gains:
  ``match_score``       — similarity in [0, 1]
  ``matched_canonical`` — the catalog phrase it matched
"""
from __future__ import annotations

import json
import logging
import re
import threading
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_CANON_PATH = Path(__file__).with_name("section_header_canon.json")

# Reentrant: _get_model() holds this while calling _ensure_catalog(), which
# takes it again. With a plain Lock that thread deadlocked holding the lock,
# so every later Docling page blocked in _ensure_catalog and hit the 90s
# timeout for the life of the process.
_lock = threading.RLock()
_model: Any = None
_model_tried = False
_model_reason: Optional[str] = None
_canon: list[str] = []
_canon_norm: list[str] = []
_canon_mtime: float | None = None
_canon_embeddings: Any = None  # np.ndarray | None
_canon_embeddings_len: int = 0


def _normalize(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"[:*\-–—|/]+$", "", t)
    t = re.sub(r"[^\w\s/+]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _token_jaccard(a: str, b: str) -> float:
    ta = {t for t in a.split() if t}
    tb = {t for t in b.split() if t}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def load_canonical_headers(path: Path | None = None) -> list[str]:
    """Load the editable catalog of clinical section-header phrases."""
    p = path or _CANON_PATH
    raw = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"canonical headers must be a JSON list: {p}")
    out = [str(x).strip() for x in raw if str(x).strip()]
    # Dedupe preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for h in out:
        key = _normalize(h)
        if not key or key in seen:
            continue
        seen.add(key)
        uniq.append(h)
    return uniq


def canon_path() -> Path:
    return _CANON_PATH


def _ensure_catalog(*, force: bool = False) -> None:
    """Load / reload ``section_header_canon.json`` when missing or mtime changes."""
    global _canon, _canon_norm, _canon_mtime, _canon_embeddings, _canon_embeddings_len
    try:
        mtime = _CANON_PATH.stat().st_mtime
    except OSError:
        mtime = None
    with _lock:
        if (
            not force
            and _canon
            and mtime is not None
            and _canon_mtime is not None
            and mtime == _canon_mtime
        ):
            return
        prev_len = len(_canon)
        _canon = load_canonical_headers()
        _canon_norm = [_normalize(h) for h in _canon]
        changed = (
            force
            or _canon_mtime is None
            or mtime != _canon_mtime
            or len(_canon) != prev_len
        )
        _canon_mtime = mtime
        if changed:
            # Force MiniLM vectors to rebuild against the new phrase list.
            _canon_embeddings = None
            _canon_embeddings_len = 0
            logger.info(
                "Section-header catalog loaded (%d phrases) from %s",
                len(_canon),
                _CANON_PATH.name,
            )


def reload_catalog() -> list[str]:
    """Force-reload the JSON catalog (tests / admin). Returns the phrases."""
    _ensure_catalog(force=True)
    return list(_canon)


def _local_model_dir() -> Path | None:
    """Return ``models/semantic-model`` when it looks like a usable checkout."""
    from config import SECTION_HEADER_MINILM_PATH

    root = Path(SECTION_HEADER_MINILM_PATH)
    if not root.is_dir():
        return None
    # sentence-transformers layouts vary; any of these means "downloaded".
    markers = (
        "config.json",
        "modules.json",
        "sentence_bert_config.json",
        "pytorch_model.bin",
        "model.safetensors",
    )
    if any((root / name).is_file() for name in markers):
        return root
    # Nested Hub snapshot layout: semantic-model/snapshots/<hash>/
    snapshots = root / "snapshots"
    if snapshots.is_dir():
        for child in sorted(snapshots.iterdir()):
            if child.is_dir() and any((child / name).is_file() for name in markers):
                return child
    return None


def resolve_minilm_source() -> str:
    """Local path string if present, else Hub model id."""
    local = _local_model_dir()
    if local is not None:
        return str(local)
    from config import SECTION_HEADER_MINILM_MODEL

    return SECTION_HEADER_MINILM_MODEL


def download_minilm(
    *,
    dest: Path | None = None,
    model_id: str | None = None,
    force: bool = False,
) -> Path:
    """Download MiniLM into ``models/semantic-model`` (gitignored).

    Uses ``huggingface_hub.snapshot_download`` so the folder is offline-usable.
    """
    from config import SECTION_HEADER_MINILM_MODEL, SECTION_HEADER_MINILM_PATH

    target = Path(dest) if dest is not None else Path(SECTION_HEADER_MINILM_PATH)
    hub_id = (model_id or SECTION_HEADER_MINILM_MODEL).strip()
    if not force:
        existing = _local_model_dir()
        if existing is not None:
            logger.info("MiniLM already present at %s", existing)
            return existing

    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise RuntimeError(
            "huggingface_hub is required to download MiniLM. "
            "Install: pip install -r requirements-docling.txt"
        ) from exc

    target.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading %s → %s", hub_id, target)
    snapshot_download(
        repo_id=hub_id,
        local_dir=str(target),
        local_dir_use_symlinks=False,
    )
    # Clear any cached "missing" probe so the next load sees the new files.
    global _model_tried
    _model_tried = False
    ready = _local_model_dir()
    if ready is None:
        raise RuntimeError(
            f"Download finished but no MiniLM files found under {target}"
        )
    logger.info("MiniLM ready at %s", ready)
    return ready


def _get_model() -> Any:
    """Lazy-load MiniLM once. Returns None when unavailable."""
    global _model, _model_tried, _model_reason
    if _model_tried:
        return _model
    with _lock:
        if _model_tried:
            return _model
        _model_tried = True
        _ensure_catalog()
        # Prefer a local checkout; do not block OCR on a Hub download.
        if _local_model_dir() is None:
            _model = None
            _model_reason = "local MiniLM missing (run section_header_match --download)"
            logger.warning(
                "Section-header MiniLM unavailable (%s); using lexical fallback",
                _model_reason,
            )
            return _model
        try:
            from sentence_transformers import SentenceTransformer

            source = resolve_minilm_source()
            logger.info("Loading section-header MiniLM from %s", source)
            _model = SentenceTransformer(source)
            _model_reason = None
            logger.info("Section-header MiniLM ready")
        except Exception as exc:
            _model = None
            _model_reason = f"{type(exc).__name__}: {exc}"
            logger.warning(
                "Section-header MiniLM unavailable (%s); using lexical fallback",
                _model_reason,
            )
        return _model


def _get_canon_embeddings() -> Any:
    """Encode (or re-encode) the current catalog; None when MiniLM is off."""
    global _canon_embeddings, _canon_embeddings_len
    _ensure_catalog()
    model = _get_model()
    if model is None:
        return None
    with _lock:
        if (
            _canon_embeddings is not None
            and _canon_embeddings_len == len(_canon)
            and len(_canon) > 0
        ):
            return _canon_embeddings
        if not _canon:
            _canon_embeddings = None
            _canon_embeddings_len = 0
            return None
        _canon_embeddings = model.encode(
            _canon,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        _canon_embeddings_len = len(_canon)
        return _canon_embeddings


def minilm_ready() -> bool:
    return _get_model() is not None


def minilm_reason() -> Optional[str]:
    _get_model()
    return _model_reason


def _lexical_score(a: str, b: str) -> float:
    """Raw similarity of two normalized phrases (no containment boost)."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _best_lexical(text_norm: str) -> tuple[float, str]:
    """Best catalog match for normalized OCR ``text_norm``.

    Rules:
      - OCR must be at least as long as the catalog phrase (blocks
        ``Note`` / ``Notes`` → ``ED Note``).
      - If the catalog tokens are a subset of the OCR tokens and the
        catalog phrase is a substantial fraction of the OCR text, boost
        (keeps ``QB Problem List`` → ``Problem List``).
    """
    _ensure_catalog()
    best_score = 0.0
    best_label = ""
    ocr_toks = {t for t in text_norm.split() if t}
    for label, label_norm in zip(_canon, _canon_norm):
        # OCR text must not be shorter than the catalog phrase it matches.
        if len(text_norm) < len(label_norm):
            continue
        score = _lexical_score(text_norm, label_norm)
        lab_toks = {t for t in label_norm.split() if t}
        if (
            lab_toks
            and ocr_toks
            and lab_toks <= ocr_toks
            and len(label_norm) >= 4
            and len(label_norm) / max(len(text_norm), 1) >= 0.55
        ):
            score = max(score, 0.93)
        if score > best_score:
            best_score = score
            best_label = label
    return best_score, best_label


def _best_minilm(text: str) -> tuple[float, str]:
    import numpy as np

    vectors = _get_canon_embeddings()
    model = _get_model()
    if model is None or vectors is None:
        return _best_lexical(_normalize(text))
    emb = model.encode([text], normalize_embeddings=True, show_progress_bar=False)
    # Cosine with L2-normalized vectors = dot product
    scores = np.asarray(vectors) @ np.asarray(emb[0])
    idx = int(np.argmax(scores))
    return float(scores[idx]), _canon[idx]


def best_header_match(
    text: str,
    *,
    use_minilm: bool = True,
) -> tuple[float, str]:
    """Return (score, canonical_label) for ``text`` against the catalog.

    Score is the better of:
      - lexical similarity to a list phrase, or
      - MiniLM cosine to a list phrase (only when the top match shares
        enough tokens — stops unrelated bold labels from passing on
        embedding cosine alone).

    Callers keep a candidate when ``score >= threshold`` (default 0.90).
    Pass ``use_minilm=False`` for a fast lexical-only pass (review-ui live
    re-filter; unit tests).
    """
    raw = (text or "").strip()
    if not raw:
        return 0.0, ""
    norm = _normalize(raw)
    if not norm:
        return 0.0, ""
    _ensure_catalog()
    lex_score, lex_label = _best_lexical(norm)
    best_score, best_label = lex_score, lex_label
    if use_minilm and _get_model() is not None:
        sem_score, sem_label = _best_minilm(raw)
        sem_norm = _normalize(sem_label)
        # Same length guard as lexical: OCR must not be shorter than the label.
        if sem_norm and len(norm) >= len(sem_norm):
            jac = _token_jaccard(norm, sem_norm)
            # Semantic counts only when it is clearly about the same list phrase.
            if jac >= 0.40 and sem_score > best_score:
                best_score, best_label = sem_score, sem_label
    return best_score, best_label


def filter_section_headers(
    candidates: list[dict[str, Any]],
    *,
    threshold: float | None = None,
    enabled: bool | None = None,
    use_minilm: bool = True,
) -> list[dict[str, Any]]:
    """Keep only candidates with ≥ ``threshold`` lexical/semantic list match.

    Default threshold is ``SECTION_HEADER_SEMANTIC_THRESHOLD`` (0.90).
    When filtering is disabled, candidates are returned unchanged (no scores).
    When enabled, each kept item is annotated with ``match_score`` and
    ``matched_canonical``. Reloads the catalog if the JSON file changed.
    """
    from config import (
        SECTION_HEADER_SEMANTIC_ENABLED,
        SECTION_HEADER_SEMANTIC_THRESHOLD,
    )

    if enabled is None:
        enabled = SECTION_HEADER_SEMANTIC_ENABLED
    if not enabled:
        return list(candidates)
    if threshold is None:
        threshold = SECTION_HEADER_SEMANTIC_THRESHOLD
    threshold = float(threshold)

    _ensure_catalog()  # pick up list edits before scoring

    kept: list[dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        score, canonical = best_header_match(text, use_minilm=use_minilm)
        if score < threshold:
            continue
        enriched = dict(item)
        enriched["match_score"] = round(float(score), 4)
        enriched["matched_canonical"] = canonical
        kept.append(enriched)
    return kept


def main(argv: list[str] | None = None) -> int:
    """CLI: download or check the local MiniLM checkout."""
    import argparse
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(
        description="Download / check MiniLM under models/semantic-model"
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="Download all-MiniLM-L6-v2 into models/semantic-model",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download even if the folder already looks complete",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Print whether a local checkout is present (exit 0/1)",
    )
    args = parser.parse_args(argv)

    if args.download:
        path = download_minilm(force=args.force)
        print(f"ready: {path}")
        return 0
    if args.check:
        local = _local_model_dir()
        if local is None:
            print("missing: models/semantic-model (run --download)")
            return 1
        print(f"ready: {local}")
        return 0
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
