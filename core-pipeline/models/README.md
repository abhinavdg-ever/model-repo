# core-pipeline/models

What the code loads (2026-11-10 audit). Nothing here is in git except this README: each host
keeps its own `core-pipeline/models/`, copied by hand (scp / robocopy) or from Blob
(`RESOURCES_FROM_BLOB=true`). The last column is the big files; every folder is needed whole.

| Folder | Stage | Needed | Big files to copy onto a host |
|---|---|---|---|
| `hw/` | 1 rotation / handwriting | yes | `handwritten_printed_convnext_tiny.pth` (106 MB); `_backup.pth` only if the main one is missing |
| `rapidocr/` | 4 Final OCR 1 (text) | yes | the whole folder (34 MB) |
| `docling/` (`docling-layout-heron`, `docling-models` tableformer) | 4 Final OCR 1 (layout, tables) | yes | `*.safetensors` (layout 164 MB, tableformer 139 + 203 MB) |
| `blank-junk/` | 3, 8 blank / junk | yes | `bert/model.safetensors` (255 MB) |
| `layout_heron/` | 7 KV extraction (headings) | yes | `model.safetensors` (164 MB) |
| `kv-extraction/` | 7 KV extraction | yes | the whole folder (14 MB) |
| `ner/gliner_low/` or `ner/gliner_medium-v2.1/` | 9 member NER (`MEMBER_NER_MODEL_ID`) | the one in use | `pytorch_model.bin` (582 / 745 MB) |
| `page-family/page-leaf/`, `page-axes/` | 10 page type | yes | `encoder/model.safetensors` (413 MB each) |
| `encounter/` | 13 encounter type | yes | `encoder/model.safetensors` (413 MB) |
| `semantic-model/` | 6 section headers (and review-ui's local run) | yes | `model.safetensors` (87 MB); review-ui local run also `onnx/model.onnx` |

**Not used by any code** (deleted from the Mac 2026-11-10): `continuity/` (old GBM experiments),
`sequencing/` (old GBM), the top-level `gliner_low/` (a copy of `ner/gliner_low/`),
`docling/docling-project--docling-layout-heron-onnx/`, and in `semantic-model/` the other
formats (`pytorch_model.bin`, `tf_model.h5`, `rust_model.ot`, `openvino/`, the other `onnx/` files).

---

# Weight files for core-pipeline. .env paths are relative to core-pipeline/
# (models/hw, models/rapidocr, models/semantic-model, models/blank-junk, models/ner).
# Large checkpoints stay local. Committed: blank-junk/,
# page-family/, kv-extraction/, and hw/image_type_classification.pkl.
#
#   hw/handwritten_printed_convnext_tiny.pth         # ConvNeXt (preferred)
#   hw/handwritten_printed_convnext_tiny_backup.pth  # prior ConvNeXt fallback
#   hw/image_type_classification.pkl           # RandomForest backup
#   rapidocr/PP-OCRv6_det_small.pth
#   rapidocr/PP-OCRv6_rec_small.pth
#   rapidocr/ch_ptocr_mobile_v2.0_cls_mobile.pth
#   rapidocr/ppocrv6_dict.txt
#   ner/   (GLiNER — via model_downloader)
#   semantic-model/   (MiniLM — section_header_match --download)
#   blank-junk/bert/   PRIMARY blank/junk model: DistilBERT, 8 labels (KEEP, BLANK,
#       six JUNK sub-types), b_jnk v0.3.1 (not committed; copy b_jnk/models/bert_page here as bert/).
#       Needs torch + transformers. Decision rules: default.json -> decision.
#   blank-junk/default.json   model version. Without bert/ or torch the vote runs on keywords alone.
#       /health -> blank_junk_model.engine says bert or keywords.
#   page-family/page-axes/   page-axes model (PAGE_AXES_MODEL_DIR): decides page_type /
#       page_subtype. encoder/, heads.pt, label_space.json from
#       training/bert-training/train_axes.py. Weights (*.safetensors, heads.pt) stay
#       local. Missing ⇒ the keyword canon (page_canon.json) decides alone.
#   page-family/family.joblib
#       TF-IDF + XGBoost page family. Keywords choose the subtype; no hit
#       leaves the subtype equal to the family. Missing file ⇒ keywords choose
#       the family too. Needs xgboost (requirements-models.txt).
#
# Section-header MiniLM prefers models/semantic-model when present; otherwise
# falls back to the HuggingFace Hub id (SECTION_HEADER_MINILM_MODEL).
