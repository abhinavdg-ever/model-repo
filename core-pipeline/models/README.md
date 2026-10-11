# Weight files for core-pipeline. .env paths are relative to core-pipeline/
# (models/hw, models/rapidocr, models/semantic-model, models/blank-junk, models/ner).
# Full list of what must be here, how to get each, and what happens without it:
# docs/HOW_TO_RUN.md § "Models". Large checkpoints stay local. Committed: blank-junk/,
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
#   blank-junk/tfidf_flat.joblib + default.json
#       TF-IDF fallback when bert/ or torch is missing (committed). Regex is the
#       last fallback. /health -> blank_junk_model.engine says which one ran.
#   page-family/family.joblib
#       TF-IDF + XGBoost page family. Keywords choose the subtype; no hit
#       leaves the subtype equal to the family. Missing file ⇒ keywords choose
#       the family too. Needs xgboost (requirements-models.txt).
#
# Section-header MiniLM prefers models/semantic-model when present; otherwise
# falls back to the HuggingFace Hub id (SECTION_HEADER_MINILM_MODEL).
