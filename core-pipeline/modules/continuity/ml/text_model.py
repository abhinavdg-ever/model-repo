"""The page-pair text model in the pipeline (Brief 3 E3, Brief 2 Q3; 2026-11-22): one Bio_ClinicalBERT
cross-encoder, trained on Colab (training/continuity/text-model/train_pairs.py), run here through
ONNX on CPU. It reads the end of page A and the start of page B and gives two probabilities:

* ``p_break``   -- the document ends after page A (one more continuity clue, ``p_break_text``);
* ``p_follows`` -- page B comes right after page A (the sequencing repair's pair score).

The input is the trainer's: page A's non-empty lines joined, its last EDGE_CHARS characters, then its
last ``words_per_side`` words; page B's first EDGE_CHARS characters, then its first words
(``_tail`` / ``_head`` are copied from train_pairs.py unchanged; a test fails if the copies drift).

Files in ``TEXT_MODEL_DIR`` (default ``models/continuity/text_model``): ``model.int8.onnx`` (made by
``python -m modules.continuity.ml.text_model quantize``) or ``model.onnx``, ``tokenizer/``,
``label_space.json``. Missing folder or onnxruntime: ``status()`` says why and the clue is NaN.
"""
from __future__ import annotations

import json
import logging
import sys
import threading
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)

EDGE_CHARS = 900  # the training pairs kept the last / first 900 characters of a page's lines
_lock = threading.Lock()
_loaded: Optional[dict[str, Any]] = None
_failed: Optional[str] = None


# --- copied unchanged from training/continuity/text-model/train_pairs.py ---------------------------


def _tail(text: str, words: int) -> str:
    return " ".join((text or "").split()[-words:])


def _head(text: str, words: int) -> str:
    return " ".join((text or "").split()[:words])


# --- the pipeline side ---------------------------------------------------------------------------


def _joined(text: str) -> str:
    return "\n".join(line.strip() for line in (text or "").splitlines() if line.strip())


def model_dir() -> Path:
    from common.config import TEXT_MODEL_DIR

    return Path(TEXT_MODEL_DIR)


def _onnx_file(folder: Path) -> Optional[Path]:
    for name in ("model.int8.onnx", "model.onnx"):
        if (folder / name).is_file():
            return folder / name
    return None


def load(folder: Optional[Path] = None) -> Optional[dict[str, Any]]:
    global _loaded, _failed
    if folder is None and (_loaded is not None or _failed is not None):
        return _loaded
    with _lock:
        where = Path(folder) if folder else model_dir()
        onnx = _onnx_file(where)
        if onnx is None or not (where / "tokenizer").is_dir() or not (where / "label_space.json").is_file():
            _failed = f"no page-pair text model at {where}"
            return None
        if not (find_spec("onnxruntime") and find_spec("transformers")):
            _failed = "onnxruntime / transformers not installed"
            return None
        import onnxruntime as ort
        from transformers import AutoTokenizer

        try:
            options = ort.SessionOptions()
            options.intra_op_num_threads = 4
            loaded = {"session": ort.InferenceSession(str(onnx), options, providers=["CPUExecutionProvider"]),
                      "tokenizer": AutoTokenizer.from_pretrained(str(where / "tokenizer")),
                      "space": json.loads((where / "label_space.json").read_text(encoding="utf-8")),
                      "file": onnx.name}
        except Exception as exc:  # noqa: BLE001 - a bad model must not sink the chart
            _failed = f"text model at {where} failed to load: {str(exc).splitlines()[0][:200]}"
            logger.error("Text model: %s", _failed)
            return None
        if folder is None:
            _loaded = loaded
        return loaded


def status() -> dict[str, Any]:
    loaded = load()
    return {"ready": loaded is not None, "reason": None if loaded else _failed, "path": str(model_dir()),
            "file": (loaded or {}).get("file")}


def pair_input(text_a: str, text_b: str, words: int) -> tuple[str, str]:
    """(end of page A, start of page B) as the model was trained on them."""
    return _tail(_joined(text_a)[-EDGE_CHARS:], words), _head(_joined(text_b)[:EDGE_CHARS], words)


def score(pairs: Sequence[tuple[str, str]], loaded: Optional[dict[str, Any]] = None,
          batch: int = 16) -> Optional[list[tuple[float, float]]]:
    """(p_break, p_follows) for each (text of page A, text of page B); None without the model."""
    loaded = loaded or load()
    if loaded is None:
        return None
    import numpy as np

    space, tok, sess = loaded["space"], loaded["tokenizer"], loaded["session"]
    words, max_length = int(space["words_per_side"]), int(space["max_length"])
    out: list[tuple[float, float]] = []
    for start in range(0, len(pairs), batch):
        chunk = [pair_input(a, b, words) for a, b in pairs[start:start + batch]]
        enc = tok([a for a, _ in chunk], [b for _, b in chunk], truncation="longest_first",
                  max_length=max_length, padding=True, return_tensors="np")
        probs = sess.run(None, {"input_ids": enc["input_ids"].astype(np.int64),
                                "attention_mask": enc["attention_mask"].astype(np.int64)})[0]
        out += [(float(p[0]), float(p[1])) for p in probs]
    return out


def quantize(folder: Path) -> Path:
    """model.onnx -> model.int8.onnx (dynamic int8 weights, CPU), beside it."""
    from onnxruntime.quantization import QuantType, quantize_dynamic

    target = folder / "model.int8.onnx"
    quantize_dynamic(str(folder / "model.onnx"), str(target), weight_type=QuantType.QInt8)
    return target


def main(argv: Optional[list[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    q = sub.add_parser("quantize", help="write model.int8.onnx beside model.onnx")
    q.add_argument("--folder", type=Path)
    a = ap.parse_args(argv)
    folder = a.folder or model_dir()
    print(f"wrote {quantize(folder)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
