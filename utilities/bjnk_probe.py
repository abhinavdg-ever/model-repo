"""Blank/junk probe: the same page text through the b_jnk BERT, on any machine.

    python utilities/bjnk_probe.py <package_root> <bert_dir> <page.txt>

Examples:
    core-pipeline/.venv/bin/python utilities/bjnk_probe.py core-pipeline/modules/blank_junk/model core-pipeline/models/blank-junk/bert page.txt
    python bjnk_probe.py . models\\bert_page page.txt        (inside b_jnk_v3, Windows)

package_root: the folder holding src/ (b_jnk_v3, or core-pipeline/modules/blank_junk/model)
bert_dir:     the folder holding model.safetensors, tokenizer.json, bjnk_meta.json
page.txt:     one page's Tesseract text (UTF-8)

Prints versions, file fingerprints, token counts and the 8 probabilities. Never the text.
"""
import hashlib
import platform
import sys
from pathlib import Path

root, bert_dir, page = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
sys.path.insert(0, str(root))

import torch  # noqa: E402
import transformers  # noqa: E402
import tokenizers  # noqa: E402
from src.models.bert_classifier import BertPageClassifier, encode_head_tail  # noqa: E402
from src.preprocessing.tesseract_txt import clean_ocr_text  # noqa: E402


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


print("python", platform.python_version(), "| torch", torch.__version__,
      "| transformers", transformers.__version__, "| tokenizers", tokenizers.__version__)
for name in ("model.safetensors", "tokenizer.json", "tokenizer_config.json", "bjnk_meta.json", "config.json"):
    f = bert_dir / name
    print(f"{name:24s}", sha(f) if f.is_file() else "MISSING")

raw = page.read_text(encoding="utf-8")
text = clean_ocr_text(raw)
print("raw sha", hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16],
      "| cleaned sha", hashlib.sha256(text.encode("utf-8")).hexdigest()[:16], "| chars", len(text))

for device in ("cpu", "auto"):
    model = BertPageClassifier.load(bert_dir, device=device)
    ids = model.tokenizer([text], add_special_tokens=False, truncation=False)["input_ids"][0]
    enc = encode_head_tail(model.tokenizer, [text], model.max_length, model.head_share)
    print(f"device {model.device} | tokens {len(ids)} -> {int(enc['attention_mask'].sum())} "
          f"| ids sha {hashlib.sha256(str(enc['input_ids'].tolist()).encode()).hexdigest()[:16]}")
    probs = model.predict_proba([text])[0]
    print("  " + "  ".join(f"{lab}={p:.4f}" for lab, p in zip(model.labels, probs)))
