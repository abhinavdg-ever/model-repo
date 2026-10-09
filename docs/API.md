# How to run

Two services, started separately. They share Postgres and `review-ui/data/folders`. They do not call each other.

| Service | Command | URL |
|---|---|---|
| core-pipeline | uvicorn on port 8001 | http://localhost:8001/docs |
| review-ui API | uvicorn on port 8002 | http://localhost:8002/docs |
| review-ui web | `npm run dev` | http://localhost:5174 |

On Windows use `py -3.12`, `.venv\Scripts\Activate.ps1`, `curl.exe`, and `$env:NAME = "value"`. If activation is blocked: `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`.

---

## 1. Setting up the environment

Install Python 3.12 (not 3.13+), Tesseract, `psql`, and Node 20+. On Windows, set `TESSERACT_CMD` in `core-pipeline/.env` to the full path of `tesseract.exe`.

```bash
# core-pipeline
cd core-pipeline
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-basic.txt
pip install -r requirements-models.txt
cp .env.example .env
```

`requirements-basic.txt` is the API, database, and OCR fallback. `requirements-models.txt` is Docling, MiniLM, GLiNER, and the key/value ranker. Set `DATABASE_URL` in `.env`. Paths in that file are relative to `core-pipeline/`.

```bash
# review-ui
cd review-ui/backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp ../.env.example ../.env
```

Apply the schema once. `v1.sql` is required. `v2.sql` is optional. An existing database is recreated, not patched: dump it, then `clear_schema.sql`, then `v1.sql`.

```bash
psql "$DATABASE_URL" -f schema/v1.sql
psql "$DATABASE_URL" -c "SELECT stage_name, pass_no, seq FROM pipeline_stage ORDER BY seq;"
```

Expect 13 phase-1 rows. An empty `pipeline_stage` makes `/ready` return 503.

### Azure OpenAI: key or no key

Azure Blob, Document Intelligence, and OpenAI are optional. Leave them unset and the matching stage records that it ran without them (`GET /health` names the gap). `AZURE_OPENAI_AUTH` is `auto` (key if `AZURE_OPENAI_API_KEY` is set, otherwise Entra), `key`, or `entra`.

---

## 2. Downloading models

Weights are not installed by pip. They live under `core-pipeline/models/`. Run the commands from `core-pipeline` with the virtualenv active.

### Downloaded by command

```bash
cd core-pipeline && source .venv/bin/activate

# Section headers — sentence-transformers/all-MiniLM-L6-v2 → models/semantic-model
python -m stages.lib.ocr.section_header_match --download

# Heading detector → models/layout_heron. GLiNER is the member NER checkpoint.
python -m stages.lib.extraction.util.model_setup

# Member NER, also used by key/value extraction. Folder is
# models/ner/<catalog folder for MEMBER_NER_MODEL_ID>.
python -m stages.lib.member.extractors.ner_based.model_downloader

# Final OCR 1 — four RapidOCR files → models/rapidocr
mkdir -p models/rapidocr
BASE=https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2
curl -fL -o models/rapidocr/PP-OCRv6_det_small.pth "$BASE/torch/PP-OCRv6/det/PP-OCRv6_det_small.pth"
curl -fL -o models/rapidocr/PP-OCRv6_rec_small.pth "$BASE/torch/PP-OCRv6/rec/PP-OCRv6_rec_small.pth"
curl -fL -o models/rapidocr/ch_ptocr_mobile_v2.0_cls_mobile.pth "$BASE/torch/PP-OCRv4/cls/ch_ptocr_mobile_v2.0_cls_mobile.pth"
curl -fL -o models/rapidocr/ppocrv6_dict.txt "$BASE/paddle/PP-OCRv6/rec/PP-OCRv6_rec_small/ppocrv6_dict.txt"
```

The first Docling run also pulls Docling’s own layout weights into the Hugging Face cache. Final OCR 2 is Azure Document Intelligence, so that is an API key, not a file.

Set `MEMBER_NER_ENABLED=true` after `models/ner/` is in place. Without it, no chart can be rejected.

### Copied in by hand

These are not fetched by the commands above, and they are not baked into the Docker image. Copy the folders into `core-pipeline/models/` on the machine that runs the API. Compose mounts that directory (`MODELS_HOST_PATH`).

| Folder | Files | Used for |
|---|---|---|
| `models/hw/` | `handwritten_printed_convnext_tiny.pth`, `metadata.json`. Optional: `handwritten_printed_convnext_tiny_backup.pth`, `image_type_classification.pkl` | Handwriting and rotation. Missing `.pth` falls back to the `.pkl`. |
| `models/kv-extraction/` | `manifest.json`, `thresholds.json`, `features.json`, and the `ranker_*.txt` files sitting in this folder (version v002) | Key/value ranker. Member, DOS, and page number read its picks. |
| `models/blank-junk/` | `tfidf_flat.joblib`, `default.json` | Blank and junk. Missing file means regex rules only. |
| `models/page-family/` | `family.joblib`, `meta.json` | Page family. Missing file, or no XGBoost, means keywords name the family. |

---

## 3. Starting uvicorn

Three terminals.

```bash
# 1. Pipeline API — http://localhost:8001/docs
cd core-pipeline && source .venv/bin/activate
uvicorn api.main:app --host 0.0.0.0 --port 8001
```

```bash
# 2. Review API — http://localhost:8002/docs
cd review-ui/backend && source .venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8002 --reload
```

```bash
# 3. Review web — http://localhost:5174
cd review-ui/frontend && npm install && npm run dev
```

`python cli.py serve` is the same pipeline process as the first uvicorn command.

```bash
curl -fsS localhost:8001/health
curl -fsS localhost:8001/ready
```

`/ready` stays 503 until Postgres is reachable and `pipeline_stage` has rows. `/health` says which model folders are missing.

Run one local chart (the folder name is the chart name):

```bash
curl -X POST localhost:8001/api/charts/run -H 'Content-Type: application/json' \
  -d '{"input_type":"local","input_path":"/path/to/inbox","chart_name":"53688890"}'
```

That returns 202. Poll `GET /api/charts/{chart_id}`. `skip_ocr: true` reuses OCR already on disk. Stage 5 (Azure Document Intelligence) is billed per page.
