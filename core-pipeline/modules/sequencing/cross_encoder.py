"""Cross-encoder pair scoring — optional ONNX; unavailable ⇒ marker/header fallback."""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from .config import CROSS_ENCODER_ONNX, CROSS_ENCODER_TOKENIZER

logger = logging.getLogger(__name__)

_session = None
_tokenizer = None
_failed = False
_lock = threading.Lock()

MAX_BERT_SEQ_LEN = 512
MAX_PAIR_TEXT_CHARS = 512


def available() -> bool:
    return _load_backend() is not None


def _clip(text: str, max_chars: int) -> str:
    text = text or ""
    return text if len(text) <= max_chars else text[:max_chars]


def _load_backend():
    global _session, _tokenizer, _failed
    if _session is not None:
        return _session
    if _failed:
        return None
    with _lock:
        if _session is not None:
            return _session
        if _failed:
            return None
        if not CROSS_ENCODER_ONNX.is_file() or not CROSS_ENCODER_TOKENIZER.is_file():
            logger.info(
                "Sequencing cross-encoder OFF (missing %s); using markers/headers/order",
                CROSS_ENCODER_ONNX.name,
            )
            _failed = True
            return None
        try:
            import numpy as np  # noqa: F401
            import onnxruntime as ort
            from tokenizers import Tokenizer

            opts = ort.SessionOptions()
            opts.log_severity_level = 3
            opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            opts.intra_op_num_threads = 2
            opts.inter_op_num_threads = 1
            _session = ort.InferenceSession(
                str(CROSS_ENCODER_ONNX), opts, providers=["CPUExecutionProvider"]
            )
            _tokenizer = Tokenizer.from_file(str(CROSS_ENCODER_TOKENIZER))
            _tokenizer.enable_truncation(max_length=MAX_BERT_SEQ_LEN)
        except Exception as exc:
            logger.warning("Sequencing cross-encoder load failed (%s)", exc)
            _failed = True
            return None
    return _session


def score_pairs(pairs: list[tuple[str, str]], batch_size: int = 32) -> list[float]:
    if not pairs:
        return []
    if not _load_backend():
        return [0.0] * len(pairs)

    import numpy as np

    scores: list[float] = []
    for start in range(0, len(pairs), batch_size):
        batch = pairs[start : start + batch_size]
        clipped = [
            (_clip(a, MAX_PAIR_TEXT_CHARS), _clip(b, MAX_PAIR_TEXT_CHARS))
            for a, b in batch
        ]
        try:
            enc = _tokenizer.encode_batch([[a, b] for a, b in clipped])
            # Minimal batching without shared onnx helpers
            max_len = MAX_BERT_SEQ_LEN
            ids = [e.ids[:max_len] for e in enc]
            masks = [[1] * len(i) + [0] * (max_len - len(i)) for i in ids]
            type_ids = [e.type_ids[:max_len] for e in enc]
            for i in range(len(ids)):
                pad = max_len - len(ids[i])
                if pad:
                    ids[i] = ids[i] + [0] * pad
                    type_ids[i] = type_ids[i] + [0] * pad
            feeds = {
                "input_ids": np.asarray(ids, dtype=np.int64),
                "attention_mask": np.asarray(masks, dtype=np.int64),
                "token_type_ids": np.asarray(type_ids, dtype=np.int64),
            }
            input_meta = {inp.name: inp for inp in _session.get_inputs()}
            run_feeds = {k: v for k, v in feeds.items() if k in input_meta}
            outputs = _session.run(None, run_feeds)
            logits = outputs[0]
            if logits.ndim == 2 and logits.shape[1] == 1:
                raw = logits[:, 0]
            elif logits.ndim == 2:
                raw = logits[:, -1]
            else:
                raw = logits
            batch_scores = 1.0 / (1.0 + np.exp(-raw))
            scores.extend(float(s) for s in batch_scores)
        except Exception as exc:
            logger.warning("Cross-encoder batch failed (%d pairs): %s", len(batch), exc)
            scores.extend([0.0] * len(batch))
    return scores
