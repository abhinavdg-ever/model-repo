# Weight files for core-pipeline. .env paths are relative to core-pipeline/
# (models/hw, models/rapidocr, models/semantic-model, models/blank-junk, models/ner).
# See docs/API.md. Everything here except blank-junk/ and this file is gitignored.
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
#   blank-junk/tfidf_flat.joblib + default.json
#       TF-IDF KEEP/BLANK/JUNK model (committed). Regex rules are the fallback.
#   blank-junk/bert_page/   optional DistilBERT (not committed). Copy from b_jnk/models/bert_page.
#       Used when that folder and torch+transformers are present; otherwise TF-IDF.
#
# Section-header MiniLM prefers models/semantic-model when present; otherwise
# falls back to the HuggingFace Hub id (SECTION_HEADER_MINILM_MODEL).
