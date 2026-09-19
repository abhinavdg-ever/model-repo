# API & running

Two services, separate deploys — they never call each other. They share Postgres
and `data/folders`.

| Service | Local port | Swagger |
|---|---|---|
| core-pipeline | `8001` | http://localhost:8001/docs |
| review-ui API | `8002` | http://localhost:8002/docs |
| review-ui web | `5174` | http://localhost:5174 |

Algorithms: [LOGIC.md](LOGIC.md). Shape: [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Local: Environment set up

### 1. System tools

| Tool | macOS | Linux | Windows |
|---|---|---|---|
| **Python 3.12** (not 3.13+) | `brew install python@3.12` | `apt install python3.12 python3.12-venv` | `winget install Python.Python.3.12` |
| `tesseract` | `brew install tesseract` | `apt install tesseract-ocr` | [UB Mannheim](https://github.com/UB-Mannheim/tesseract/wiki) |
| `psql` | `brew install libpq` | `apt install postgresql-client` | PostgreSQL installer |
| Node 20+ (frontend) | `brew install node` | `apt install nodejs npm` | `winget install OpenJS.NodeJS.LTS` |

Set `TESSERACT_CMD` when `tesseract` is not on `PATH` (always on Windows).

### 2. Database schema

`schema/` has three files: `clear_schema.sql`, `v1.sql`, `v2.sql`.

```bash
psql "$DATABASE_URL" -f schema/v1.sql            # required
psql "$DATABASE_URL" -f schema/v2.sql            # optional — next phase
psql "$DATABASE_URL" -c "SELECT count(*) FROM pipeline_stage;"   # 8, or 12 with v2
```

Wipe and re-apply:

```bash
psql "$DATABASE_URL" -f schema/clear_schema.sql
psql "$DATABASE_URL" -f schema/v1.sql
psql "$DATABASE_URL" -f schema/v2.sql
# or: ./scripts/reset_db.sh --yes
```

PowerShell: `$env:DATABASE_URL`. Empty `pipeline_stage` ⇒ `/ready` returns 503.

### 3. Python packages + `.env`

```bash
# macOS / Linux
cd core-pipeline
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # set DATABASE_URL at minimum

cd ../review-ui/backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp ../.env.example ../.env    # or review-ui/.env.example
```

Windows: `py -3.12 -m venv .venv` then `.venv\Scripts\Activate.ps1`.

Optional pip extras:

| File | Enables |
|---|---|
| `requirements-docling.txt` | Docling + ConvNeXt HW **packages** (weights separate) |
| `requirements-ner.txt` + model downloader | GLiNER / document **Rejected** |

Key `.env` knobs (paths relative to `core-pipeline/`):

| Variable | Default / notes |
|---|---|
| `DATABASE_URL` | required |
| `DATA_ROOT` | `../review-ui/data/folders` |
| `STAGE_WORKERS` / `BATCH_WORKERS` | `4` / `4` — keep `workers × STAGE_WORKERS ≤ DB_POOL_MAX` |
| `SKIP_OCR` | `false` — reuse on-disk `ocr/` when present |
| `HW_MODEL_PATH` | `models/hw/handwritten_printed_convnext_tiny.pth` |
| `RAPID_MODELS_DIR` | `models/rapidocr` |
| `MEMBER_NER_ENABLED` | `false` until GLiNER is installed |

Azure Blob / DocIntel / OpenAI are optional — missing ones degrade a stage in a
way the run records (`GET /health` names the gap).

Blob auth modes (`AZURE_STORAGE_AUTH`):

| Mode | When |
|---|---|
| `entra` (default) | `DefaultAzureCredential` (MI → CLI → …). Set `AZURE_CLIENT_ID` for a **user-assigned** MI |
| `managed_identity` | VM / App Service MI only — no CLI, no browser. Same `AZURE_CLIENT_ID` |
| `entra_interactive` | MI → `az login` → browser (dev machines) |
| `key` | `AZURE_STORAGE_ACCOUNT_KEY` |

`AZURE_PRINCIPAL_ID` is the object id used when assigning **Storage Blob Data
Reader/Contributor**; it is not passed to the SDK. Final2 DI features default
to `languages,barcodes` (`AZURE_DI_FEATURES=off` to disable).

---

## Prerequisites (Models)

Weight files are **not** on PyPI. Place them under `core-pipeline/models/`
(gitignored):

```
models/hw/handwritten_printed_convnext_tiny.pth
models/rapidocr/
  PP-OCRv6_det_small.pth
  PP-OCRv6_rec_small.pth
  ch_ptocr_mobile_v2.0_cls_mobile.pth
  ppocrv6_dict.txt
models/ner/          # GLiNER via model_downloader
```

| Missing | Fallback |
|---|---|
| ConvNeXt `.pth` / torch | RandomForest `image_type_classification.pkl` |
| RapidOCR four files | **rapidocr-onnxruntime** (base `requirements.txt`) |
| GLiNER | rules-only member verify — **no chart can be Rejected** |

RapidOCR download (ModelScope v3.9.2):

```bash
cd core-pipeline && mkdir -p models/rapidocr
BASE=https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2
curl -L -o models/rapidocr/PP-OCRv6_det_small.pth "$BASE/torch/PP-OCRv6/det/PP-OCRv6_det_small.pth"
curl -L -o models/rapidocr/PP-OCRv6_rec_small.pth "$BASE/torch/PP-OCRv6/rec/PP-OCRv6_rec_small.pth"
curl -L -o models/rapidocr/ch_ptocr_mobile_v2.0_cls_mobile.pth \
  "$BASE/torch/PP-OCRv4/cls/ch_ptocr_mobile_v2.0_cls_mobile.pth"
curl -L -o models/rapidocr/ppocrv6_dict.txt \
  "$BASE/paddle/PP-OCRv6/rec/PP-OCRv6_rec_small/ppocrv6_dict.txt"
pip install -r requirements-docling.txt
```

GLiNER:

```bash
pip install -r requirements-ner.txt
python -m stages.lib.member.extractors.ner_based.model_downloader
export MEMBER_NER_ENABLED=true
```

Confirm: `curl -s localhost:8001/health | python -m json.tool` — check
`docling_final1.ready` / `member_ner.ready`.

---

## Uvicorn / server start

### core-pipeline (`:8001`)

```bash
cd core-pipeline && source .venv/bin/activate
python cli.py serve
# equivalent: uvicorn api.main:app --host 0.0.0.0 --port 8001
```

### review-ui

```bash
# API — :8002 locally (:3000 in Docker)
cd review-ui/backend && source .venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8002 --reload

# Web — :5174 locally (:3001 in Docker)
cd review-ui/frontend && npm install && npm run dev
```

Checks:

```bash
curl -fsS localhost:8001/health
curl -fsS localhost:8001/ready    # 503 until DB + pipeline_stage are good
```

`DATA_MODE=local` (review-ui default) reads `data/folders`.
`DATA_MODE=production` reads Postgres.

POC login (not a security control): `imaging-user` / `aipocpw2026` — override
with `VITE_LOGIN_*` and rebuild the frontend.

---

## APIs available

Mutating chart calls return **202** and run in the background. Poll
`GET /api/charts/{id}` or `GET /api/charts/by-name/{name}`.

Stage names: `ocr_quality`, `ocr_prelim`, `blank_junk`, `ocr_final1`,
`ocr_final2`, `member_verify`, `dos_extract`. Pass 2: `blank_junk:2`.
Unknown name → **400**.

### Shared optional stage selectors

Usable on `/run`, `/batch`, and `/rerun`:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `through` | string | omit | Run from the top, **stop after** this stage |
| `only` | string[] | omit | Run **just** these stages against existing outputs |
| `force` | bool | `false` | Reprocess completed pages (**final2 is billed**) |

### `POST /api/charts/run` — one chart

**Required (local):** `local_read_path` + `local_folder_name`  
**Required (blob):** `blob_container` + `blob_read_path` + `blob_read_folder_name`  
Do not mix blob and local. Folder name **is** the chart name.

| Field | Required? | Default | Notes |
|---|---|---|---|
| `local_read_path` / `local_folder_name` | local source | — | Server-side paths |
| `local_write_path` | optional | omit | Write results; omit to keep workspace only |
| `blob_container` / `blob_read_path` / `blob_read_folder_name` | blob source | — | |
| `blob_write_path` | optional | omit | Write under this prefix |
| `write_mode` | optional | `skip_orig_pages` | or `all_files` |
| `overwrite` | optional | `false` | Replace files at destination |
| `run_id` / `batch_id` | optional | inferred | From path (`Run1`→`R1`, `Batch1`→`B1`) |
| `through` / `only` / `force` | optional | — | See above |

```bash
# Minimal local
curl -X POST localhost:8001/api/charts/run -H 'Content-Type: application/json' \
  -d '{"local_read_path":"/data/inbox","local_folder_name":"52743839_44976074"}'

# Local + write + stop before Azure OCR
curl -X POST localhost:8001/api/charts/run -H 'Content-Type: application/json' \
  -d '{"local_read_path":"/data/inbox","local_folder_name":"52743839_44976074",
       "local_write_path":"/data/outbox","through":"ocr_final1"}'

# Blob
curl -X POST localhost:8001/api/charts/run -H 'Content-Type: application/json' \
  -d '{"blob_container":"imaging-pipeline",
       "blob_read_path":"run1/batch1",
       "blob_read_folder_name":"52743839_44976074",
       "blob_write_path":"Processed/Run1",
       "run_id":"R1","batch_id":"B1"}'
```

### `POST /api/charts/batch` — every chart folder under a path

Same vocabulary as `/run` **without** a folder name (each subfolder is a chart).

| Field | Required? | Default | Notes |
|---|---|---|---|
| `local_read_path` **or** blob pair | one source | — | |
| `local_write_path` / `blob_write_path` | optional | omit | |
| `write_mode` | optional | `skip_orig_pages` | |
| `overwrite` | optional | `false` | |
| `limit` | optional | all | First N charts (dry run) |
| `workers` | optional | `BATCH_WORKERS` (4) | Must fit DB pool |
| `run_id` / `batch_id` | optional | inferred | |
| `through` / `only` / `force` | optional | — | |

```bash
curl -X POST localhost:8001/api/charts/batch -H 'Content-Type: application/json' \
  -d '{"local_read_path":"/data/inbox","limit":1,"through":"ocr_prelim","workers":2}'
```

Progress: `progress.txt` in the batch parent folder (`processing N/X charts…`)
and under each chart’s `imaging/progress.txt` (`processing N/X files…`).

### `POST /api/charts/write` — export without reprocessing

| Field | Required? | Default |
|---|---|---|
| `chart_name` | **required** | — |
| `local_write_path` **or** `blob_container`+`blob_write_path` | one dest | — |
| `write_mode` | optional | `skip_orig_pages` |
| `overwrite` | optional | `false` |

### `POST /api/charts/{chart_id}/rerun`

| Field | Required? | Default |
|---|---|---|
| `through` / `only` / `force` | optional | resume incomplete pages |

```bash
curl -X POST localhost:8001/api/charts/7/rerun -H 'Content-Type: application/json' \
  -d '{"only":["dos_extract"]}'
```

### `POST /api/manifest/sweep`

| Field | Required? | Default |
|---|---|---|
| `local_path` **or** `blob_container`+`blob_prefix` | one source | — |
| `run_id` / `batch_id` | optional | parsed from filename (`metadata_R1_B1.csv`) |
| `mirror_local` | optional | — | copy blob manifests into `METADATA_ROOT` |

### Read / ops

| Method | Path | Notes |
|---|---|---|
| `GET` | `/health` | Liveness + optional feature readiness |
| `GET` | `/ready` | 503 unless DB up and `pipeline_stage` seeded |
| `GET` | `/api/stages` | Stage chain from DB |
| `GET` | `/api/charts/{id}` | Progress / status |
| `GET` | `/api/charts/by-name/{name}` | Same by chart name |
| `GET` | `/api/manifest/{record_id}` | Manifest rows for a chart |
| `GET` | `/api/jobs?chart_id=` | Job log (`chart_id` optional) |

### review-ui (all `GET`, read-only)

| Path | Returns |
|---|---|
| `/api/health` | status + `data_mode` |
| `/api/folders` | charts + badges |
| `/api/folders/{id}` | pages + OCR availability |
| `/api/folders/{id}/pages/{n}/image` | page image |
| `/api/folders/{id}/ocr?kind=preliminary\|final1\|final2` | OCR text |
| `/api/folders/{id}/imaging` | imaging results |
| `/imaging/export.csv` | CSV export |

### CLI (same options as the API)

```bash
cd core-pipeline
python cli.py serve
python cli.py stages
python cli.py run --local-read-path /data/inbox --folder-name 52743839_44976074
python cli.py batch --local-read-path /data/inbox --limit 1 --through ocr_prelim
python cli.py write 52743839_44976074 --local-write-path /data/outbox
python cli.py rerun 7 --only dos_extract
python cli.py manifest --local ../review-ui/data/metadata/metadata_R1_B1.csv
```

### Utilities — load a folder of `metadata_Rn_Bn` files

```bash
cd core-pipeline && source .venv/bin/activate
python ../utilities/load_metadata_manifests.py
python ../utilities/load_metadata_manifests.py /path/to/metadata/
```

See [`utilities/README.md`](../utilities/README.md).

---

## Docker

Each service has its own `docker-compose.yml`. Order does not matter.

```bash
cd core-pipeline && cp .env.example .env && docker compose up -d --build
curl -fsS localhost:8001/ready

cd ../review-ui && cp .env.example .env && docker compose up -d --build
# UI: http://localhost:3001   API: http://localhost:3000
```

Local paths in API bodies must be paths **inside the container** (mount the
host folder first).

NER in Docker:

```bash
docker compose build --build-arg WITH_NER=true
NER_MODELS_HOST_PATH=./models/ner MEMBER_NER_ENABLED=true docker compose up -d
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `/ready` → 503, empty `pipeline_stage` | `psql … -f schema/v1.sql` |
| Chart stuck at `ocr_prelim` | Install tesseract / set `TESSERACT_CMD` |
| `final2` empty | Set Azure DI endpoint + key |
| `manifest_missing` | Sweep manifest, then `rerun` with `only: ["member_verify"]` |
| No Rejected / `ner_disabled` | `GET /health` → `member_ner.reason` |
| Python 3.13 pip failure | Use 3.12 |
| PowerShell `curl` oddities | Use `curl.exe` |
