# core-pipeline — chart intake + the imaging stage chain.
#
# Deployed independently of review-ui. The two share a Postgres database and the
# outputs/charts volume (this service writes it, review-ui mounts it read-only);
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

# Fail the build if the blank/junk keyword canon did not land in the image: a clinical page
# with a fax confidentiality footer must score clinical over junk (modules/blank_junk/method.py,
# keyword-canon). The regex classify_text this used to call was removed on 2026-11-01.
RUN python -c "\
import modules.blank_junk.stage; \
from modules.blank_junk.method import canon, score_text; \
assert canon().groups, 'blank/junk canon empty'; \
text = ( \
    'Progress Note\\n' \
    'Reason for Appointment: 6 month follow up.\\n' \
    'History of Present Illness: Patient presents with SOB.\\n' \
    'Current Medications: Amlodipine, Apixaban.\\n' \
    'Vital Signs: BP 132/64. Assessment: Hypertension.\\n' \
    'CONFIDENTIALITY NOTICE: This transmission is intended only ' \
    'for the intended recipient and may contain confidential medical records.\\n' \
); \
s = score_text(text); \
assert s.clinical > s.junk, (s.clinical, s.junk); \
print('blank/junk canon ok')"

# Fail the build if b_jnk v3's PageClassifierService (blank/junk R5 since 2026-11-03) or
# its decision settings did not land. No weights needed: the modules import and the
# config builds a DecisionConfig.
RUN cd modules/blank_junk/model && python -c "\
import json; \
from src.inference.predict import PageClassifierService; \
from src.models.decision import DecisionConfig; \
cfg = json.load(open('configs/default.json', encoding='utf-8')); \
DecisionConfig(**cfg['decision']); \
assert cfg['routing']['min_dictionary_words'] >= 0; \
print('blank/junk PageClassifierService ok')"

# Fail the build if stage 1 cannot judge orientation, or if the canons from
# this checkout did not land: OSD data, the reading vocabulary, the stored
# text-orientation convention, the page taxonomy's label space (any version) and the
# DOS range labels.
RUN tesseract --list-langs 2>&1 | grep -qx osd \
 && test -s modules/image_preprocess/english_words.txt.gz \
 && python -c "\
import json; \
from modules.image_preprocess.stage import text_orientation; \
orient = [text_orientation(c) for c in (0, 90, 180, 270)]; \
assert orient == [0, 90, 180, 270], ('text_orientation', orient); \
tax = json.load(open('resources/canon/page_taxonomy.json')); \
assert tax['label_space']['L1'] and tax['label_space']['L2'], tax.get('version'); \
dos = json.load(open('resources/canon/dos_canon.json')); \
assert 'certification period' in dos['labels']['encounter'], 'dos_canon labels'; \
print('orientation + canons ok')"

# Fail the build, not the server, when a code package did not land in the checkout
# (a working copy missing modules/extraction/training crashed uvicorn on import).
# The continuity engine also loads its canon: weights naming an unknown signal, or an
# inverted abstain band, fail here.
RUN python -c "import modules.extraction, modules.dos.dos_logic; \
from modules.continuity.engine import load_rules; load_rules(); print('packages ok')"

# Engines live under modules/image_preprocess/ and modules/ocr/; weights are bind-mounted at runtime.
# Weight paths match .env: relative to core-pipeline/ (this image's WORKDIR).
ENV PYTHONPATH=/app/core-pipeline \
    HW_MODEL_PATH=models/hw/handwritten_printed_convnext_tiny.pth \
    BLANK_JUNK_MODEL_DIR=models/blank-junk \
    RAPID_MODELS_DIR=models/rapidocr \
    SECTION_HEADER_MINILM_PATH=models/semantic-model \
    MEMBER_NER_MODELS_PATH=models/ner \
    PAGE_FAMILY_MODEL_DIR=models/page-family/bert \
    PAGE_AXES_MODEL_DIR=models/page-family/page-axes \
    PAGE_LEAF_MODEL_DIR=models/page-family/page-leaf \
    PAGE_CLASSIFIER=leaf \
    ENCOUNTER_MODEL_DIR=models/encounter \
    EXTRACTION_MODELS_ROOT=models \
    API_HOST=0.0.0.0 \
    API_PORT=8001

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8001/health || exit 1

CMD ["python", "-m", "uvicorn", "apps.orchestrator_api.main:app", "--host", "0.0.0.0", "--port", "8001"]
