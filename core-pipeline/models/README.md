# Place downloaded weight files here. See docs/API.md.
# This directory is gitignored — do not commit checkpoints.
#
#   hw/handwritten_printed_convnext_tiny.pth   # ConvNeXt (preferred)
#   hw/image_type_classification.pkl           # RandomForest backup
#   rapidocr/PP-OCRv6_det_small.pth
#   rapidocr/PP-OCRv6_rec_small.pth
#   rapidocr/ch_ptocr_mobile_v2.0_cls_mobile.pth
#   rapidocr/ppocrv6_dict.txt
#   ner/   (GLiNER — via model_downloader)
#   semantic-model/   (MiniLM — section_header_match --download)
#
# Section-header MiniLM prefers models/semantic-model when present; otherwise
# falls back to the HuggingFace Hub id (SECTION_HEADER_MINILM_MODEL).
