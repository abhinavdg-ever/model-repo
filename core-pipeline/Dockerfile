# core-pipeline — chart intake + the imaging stage chain.
#
# Deployed independently of review-ui. The two share a Postgres database and the
# data/folders volume (this service writes it, review-ui mounts it read-only);
# there is no HTTP call between them.
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# tesseract-ocr        — preliminary OCR (stage 2)
# tesseract-ocr-osd    — page orientation (stage 1). Without it OSD answers
#                        nothing and every page reports orientation 0.
# libgl1 / libglib2.0  — OpenCV runtime (rotation / quality)
# libgomp1             — ONNX Runtime (RapidOCR) and scikit-learn
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-osd \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Model packages (Docling, MiniLM, GLiNER, BERT page and blank/junk classifiers, the key/value ranker) are
# in the image. Weight files are not baked in. Copy them onto the host under
# ./models; compose mounts that folder.
COPY requirements-basic.txt requirements-models.txt ./
RUN pip install --no-cache-dir -r requirements-basic.txt \
 && pip install --no-cache-dir -r requirements-models.txt

COPY . /app/core-pipeline
WORKDIR /app/core-pipeline

# Fail the build if junk keywords / clinical short-circuit did not land in the
# image (same logic as local / Windows — stages/lib/keyword-canon/junk_keywords_canon.json + text_utils).
RUN python -c "\
import sys; \
sys.path.insert(0, 'stages/lib/blank_junk'); \
from classify import CODE_MAIN, classify_text; \
from kw import rules; \
assert rules().letter_fax and rules().invoice_keywords; \
text = ( \
    'Progress Note\\n' \
    'Reason for Appointment: 6 month follow up.\\n' \
    'History of Present Illness: Patient presents with SOB.\\n' \
    'Current Medications: Amlodipine, Apixaban.\\n' \
    'Vital Signs: BP 132/64. Assessment: Hypertension.\\n' \
    'CONFIDENTIALITY NOTICE: This transmission is intended only ' \
    'for the intended recipient and may contain confidential medical records.\\n' \
); \
code, reason = classify_text(text); \
assert code == CODE_MAIN and reason == 'clinical_content', (code, reason); \
print('junk clinical short-circuit ok')"

# Fail the build if stage 1 cannot judge orientation, or if the canons from
# this checkout did not land: OSD data, the reading vocabulary, the stored
# text-orientation convention, taxonomy 1.2 and the DOS range labels.
RUN tesseract --list-langs 2>&1 | grep -qx osd \
 && test -s stages/lib/image_preprocess/english_words.txt.gz \
 && python -c "\
import json; \
from stages.lib.image_preprocess.stage import text_orientation; \
assert [text_orientation(c) for c in (0, 90, 180, 270)] == [0, 270, 180, 90]; \
tax = json.load(open('stages/lib/keyword-canon/page_taxonomy.json')); \
assert tax['version'] == '1.2' and len(tax['page_types']) == 51, tax['version']; \
dos = json.load(open('stages/lib/keyword-canon/dos_canon.json')); \
assert 'certification period' in dos['labels']['encounter']; \
print('orientation + canons ok')"

# Engines live under stages/lib/image_preprocess/ and stages/lib/ocr/; weights are bind-mounted at runtime.
# Weight paths match .env: relative to core-pipeline/ (this image's WORKDIR).
ENV PYTHONPATH=/app/core-pipeline \
    HW_MODEL_PATH=models/hw/handwritten_printed_convnext_tiny.pth \
    BLANK_JUNK_MODEL_DIR=models/blank-junk \
    RAPID_MODELS_DIR=models/rapidocr \
    SECTION_HEADER_MINILM_PATH=models/semantic-model \
    MEMBER_NER_MODELS_PATH=models/ner \
    PAGE_FAMILY_MODEL_DIR=models/page-family/bert \
    EXTRACTION_MODELS_ROOT=models \
    API_HOST=0.0.0.0 \
    API_PORT=8001

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8001/health || exit 1

CMD ["python", "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8001"]
