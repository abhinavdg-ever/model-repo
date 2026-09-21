"""Bundled sequencing constants (ported from document-processing)."""
from pathlib import Path

_ARTIFACTS = Path(__file__).resolve().parent / "artifacts"

CROSS_ENCODER_ONNX = _ARTIFACTS / "cross_encoder_mini_lm.onnx"
CROSS_ENCODER_TOKENIZER = _ARTIFACTS / "cross_encoder_tokenizer.json"
MAX_PAIR_SCORES_PER_JOB = 2500
HIGH_CONFIDENCE_THRESHOLD = 0.50
