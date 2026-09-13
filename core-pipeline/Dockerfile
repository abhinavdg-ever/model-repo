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

# tesseract-ocr        — preliminary OCR (stage 1)
# libgl1 / libglib2.0  — OpenCV runtime, used by the rotation detector
# libgomp1             — ONNX Runtime (RapidOCR) and scikit-learn
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        libgl1 \
        libglib2.0-0 \
        libgomp1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# The NER layer (GLiNER + torch) roughly triples the image size, so it is
# opt-in:  docker build --build-arg WITH_NER=true .
ARG WITH_NER=false

COPY requirements.txt requirements-ner.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && if [ "$WITH_NER" = "true" ]; then \
        pip install --no-cache-dir -r requirements-ner.txt; \
    fi

COPY . /app/core-pipeline
WORKDIR /app/core-pipeline

# The handwriting classifier and the rotation detector are imported from the
# reference tree; the compose file mounts it at this path.
ENV PYTHONPATH=/app/core-pipeline \
    HW_MODEL_PATH=/app/Reference/advantmed_imaging/image_type_classification.pkl \
    API_HOST=0.0.0.0 \
    API_PORT=8001

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8001/health || exit 1

CMD ["python", "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8001"]
