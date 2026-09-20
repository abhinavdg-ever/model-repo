# API & running

Two services, separate deploys — they never call each other. They share Postgres
and `data/folders`.

| Service | Local port | Swagger |
|---|---|---|
| core-pipeline | `8001` | http://localhost:8001/docs |
| review-ui API | `8002` | http://localhost:8002/docs |
| review-ui web | `5174` | http://localhost:5174 |

Algorithms: [LOGIC.md](LOGIC.md). Shape: [ARCHITECTURE.md](ARCHITECTURE.md).

**Windows tips (every command below):** use `curl.exe` (not `curl`),
`py -3.12` (not plain `python` if 3.13 is default),
`.venv\Scripts\Activate.ps1`, and `$env:NAME = "value"` for env vars.
If `Activate.ps1` is blocked:
`Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`.

---

## Local: Environment set up

### 1. System tools

| Tool | macOS | Linux | Windows |
|---|---|---|---|
| **Python 3.12** (not 3.13+) | `brew install python@3.12` | `apt install python3.12 python3.12-venv` | `winget install Python.Python.3.12` |
| `tesseract` | `brew install tesseract` | `apt install tesseract-ocr` | [UB Mannheim](https://github.com/UB-Mannheim/tesseract/wiki) |
| `psql` | `brew install libpq` | `apt install postgresql-client` | PostgreSQL installer |
| Node 20+ (frontend) | `brew install node` | `apt install nodejs npm` | `winget install OpenJS.NodeJS.LTS` |
| Git LFS (HW `.pth`) | `brew install git-lfs` | `apt install git-lfs` | `winget install GitHub.GitLFS` |

Set `TESSERACT_CMD` when `tesseract` is not on `PATH` (always on Windows), e.g.
`TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe`.

### 2. Database schema

`schema/` has three files: `clear_schema.sql`, `v1.sql`, `v2.sql`.

Existing databases that already applied an older `v1.sql` can take the additive
patch instead of recreating:

```bash
psql "$DATABASE_URL" -f schema/patch_output_path.sql
```

That adds `chart_list.output_path` and remaps any legacy `status='rejected'` →
`completed`. When a write path is passed on run/batch-run it is stored **as-is**
(chart folder appended if missing). Otherwise ingest derives e.g.
`Raw_Input/Run1/Batch1/DEID_Images/<chart>` →
`Processed/Run1/Batch1/<chart>`.

```bash
# macOS / Linux
psql "$DATABASE_URL" -f schema/v1.sql            # required
psql "$DATABASE_URL" -f schema/v2.sql            # optional — next phase
psql "$DATABASE_URL" -c "SELECT count(*) FROM pipeline_stage;"   # 8, or 12 with v2
```

```powershell
# Windows
psql $env:DATABASE_URL -f schema/v1.sql
psql $env:DATABASE_URL -f schema/v2.sql
psql $env:DATABASE_URL -c "SELECT count(*) FROM pipeline_stage;"
```

Wipe and re-apply (full — drops roster too):

```bash
# macOS / Linux
psql "$DATABASE_URL" -f schema/clear_schema.sql
psql "$DATABASE_URL" -f schema/v1.sql
psql "$DATABASE_URL" -f schema/v2.sql
# or: ./scripts/reset_db.sh --yes
```

```powershell
# Windows
psql $env:DATABASE_URL -f schema/clear_schema.sql
psql $env:DATABASE_URL -f schema/v1.sql
psql $env:DATABASE_URL -f schema/v2.sql
# or: .\scripts\reset_db.ps1 -Yes   (if present)
```

Empty chart/OCR/imaging rows but **keep the manifest roster** (and
`pipeline_stage`):

```bash
psql "$DATABASE_URL" -f schema/clear_results_keep_manifest.sql
```

```powershell
psql $env:DATABASE_URL -f schema/clear_results_keep_manifest.sql
```

Empty `pipeline_stage` ⇒ `/ready` returns 503.

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

```powershell
# Windows
cd core-pipeline
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env   # set DATABASE_URL at minimum

cd ..\review-ui\backend
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item ..\.env.example ..\.env
```

Optional pip extras (local venv). **Docker always installs
`requirements-docling.txt`**; GLiNER remains a build-arg.

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
| `SECTION_HEADER_MINILM_PATH` | `models/semantic-model` — local MiniLM (preferred) |
| `SECTION_HEADER_SEMANTIC_ENABLED` | `true` — filter Final1 `section_headers` ≥90% |
| `DOCLING_TABLE_CELL_MATCHING` | `true` — fill table cells (dense forms) on the primary Final1 pass. On timeout/sparse, Final1 re-runs Docling with matching off for `section_headers` and takes body text from RapidOCR-onnx (`engine=docling-headers+rapidocr-onnx`). |
| `DOCLING_IMAGES_SCALE` | `1.0` — keep at 1 for page images (`2` halves overlay boxes) |
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

Weight files are **not** on PyPI and **`core-pipeline/models/` is not in git**.
Download them onto each machine under `core-pipeline/models/`:

```
models/hw/handwritten_printed_convnext_tiny.pth
models/hw/image_type_classification.pkl          # RF fallback
models/rapidocr/
  PP-OCRv6_det_small.pth
  PP-OCRv6_rec_small.pth
  ch_ptocr_mobile_v2.0_cls_mobile.pth
  ppocrv6_dict.txt
models/ner/               # GLiNER via model_downloader
models/semantic-model/    # MiniLM — section_header_match --download
```

**Handwritten / printed (ConvNeXt)** — copy the `.pth` into `models/hw/`, then:

```bash
# macOS / Linux
cd core-pipeline && source .venv/bin/activate
pip install -r requirements-docling.txt   # torch + torchvision + MiniLM
```

```powershell
# Windows
cd core-pipeline; .venv\Scripts\Activate.ps1
pip install -r requirements-docling.txt
```

| Missing | Fallback |
|---|---|
| ConvNeXt `.pth` / torch | RandomForest `models/hw/image_type_classification.pkl` |
| RapidOCR four files | **rapidocr-onnxruntime** (base `requirements.txt`) |
| GLiNER | rules-only member verify — **no chart can be Rejected** |
| MiniLM / sentence-transformers | lexical header match (same 0.90 threshold) |

### Section-header MiniLM

Used to filter Final1 `section_headers` in `*_final1.json` (≥90% match to
`stages/lib/imaging/section_header_canon.json`).

**Recommended: keep weights under `models/semantic-model/`** (gitignored):

```bash
# macOS / Linux
cd core-pipeline && source .venv/bin/activate
pip install -r requirements-docling.txt          # sentence-transformers + hub
python -m stages.lib.imaging.section_header_match --download
python -m stages.lib.imaging.section_header_match --check
```

```powershell
cd core-pipeline; .venv\Scripts\Activate.ps1
pip install -r requirements-docling.txt
python -m stages.lib.imaging.section_header_match --download
python -m stages.lib.imaging.section_header_match --check
```

Equivalent Hub CLI (same destination):

```bash
huggingface-cli download sentence-transformers/all-MiniLM-L6-v2 \
  --local-dir models/semantic-model
```

Runtime load order: `SECTION_HEADER_MINILM_PATH` (default `models/semantic-model`)
if present → else Hub id `SECTION_HEADER_MINILM_MODEL`. Disable with
`SECTION_HEADER_SEMANTIC_ENABLED=false`.

### RapidOCR download (ModelScope v3.9.2)

**macOS / Linux**

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

**Windows (PowerShell)** — `BASE=...` is bash-only; use `$base` and `curl.exe`:

```powershell
cd core-pipeline
New-Item -ItemType Directory -Force -Path models\rapidocr | Out-Null
$base = "https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2"
curl.exe -L -o models\rapidocr\PP-OCRv6_det_small.pth "$base/torch/PP-OCRv6/det/PP-OCRv6_det_small.pth"
curl.exe -L -o models\rapidocr\PP-OCRv6_rec_small.pth "$base/torch/PP-OCRv6/rec/PP-OCRv6_rec_small.pth"
curl.exe -L -o models\rapidocr\ch_ptocr_mobile_v2.0_cls_mobile.pth "$base/torch/PP-OCRv4/cls/ch_ptocr_mobile_v2.0_cls_mobile.pth"
curl.exe -L -o models\rapidocr\ppocrv6_dict.txt "$base/paddle/PP-OCRv6/rec/PP-OCRv6_rec_small/ppocrv6_dict.txt"
pip install -r requirements-docling.txt
```

### GLiNER

Ids: `gliner_low` (smallest) · `gliner_medium` (default) · `gliner_large`.

Prefer setting these in `core-pipeline/.env` (not a shell `export`) so Docker
Compose cannot pick up a different value from the host environment.

```bash
# macOS / Linux
pip install -r requirements-ner.txt
# in .env: MEMBER_NER_MODEL_ID=gliner_medium
python -m stages.lib.member.extractors.ner_based.model_downloader
# in .env: MEMBER_NER_ENABLED=true
```

```powershell
# Windows
pip install -r requirements-ner.txt
# in .env: MEMBER_NER_MODEL_ID=gliner_medium
python -m stages.lib.member.extractors.ner_based.model_downloader
# in .env: MEMBER_NER_ENABLED=true
```
Confirm:

```bash
# macOS / Linux
curl -s localhost:8001/health | python -m json.tool
```

```powershell
# Windows
curl.exe -s localhost:8001/health | python -m json.tool
```

Check `docling_final1.ready` / `member_ner.ready`.

---

## Uvicorn / server start

### core-pipeline (`:8001`)

```bash
# macOS / Linux
cd core-pipeline && source .venv/bin/activate
python cli.py serve
# equivalent: uvicorn api.main:app --host 0.0.0.0 --port 8001
```

```powershell
# Windows
cd core-pipeline
.venv\Scripts\Activate.ps1
python cli.py serve
```

### review-ui

```bash
# macOS / Linux
# API — :8002 locally (:3000 in Docker)
cd review-ui/backend && source .venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8002 --reload

# Web — :5174 locally (:3001 in Docker)
cd review-ui/frontend && npm install && npm run dev
```

```powershell
# Windows
cd review-ui\backend
.venv\Scripts\Activate.ps1
uvicorn app.main:app --host 127.0.0.1 --port 8002 --reload

# separate terminal
cd review-ui\frontend
npm install
npm run dev
```

Checks:

```bash
# macOS / Linux
curl -fsS localhost:8001/health
curl -fsS localhost:8001/ready    # 503 until DB + pipeline_stage are good
```

```powershell
# Windows
curl.exe -fsS localhost:8001/health
curl.exe -fsS localhost:8001/ready
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

Usable on `/run` and `/batch-run`:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `through` | string | omit | Run from the top, **stop after** this stage |
| `only` | string[] | omit | Run **just** these stages against existing outputs |
| `force` | bool | `true` | Reprocess completed pages (**final2 is billed**). Set `false` to resume. |
| `skip_ocr` | bool | omit | Per-request override for `SKIP_OCR`. `true` = reuse OCR from the chart **output folder** (or workspace `ocr/`) if present, else materialize the three `ocr/` files from `ocr_results`; skip prelim/final1/final2. `false` = always run OCR. omit = honour env. Ignored when `force=true` |

### `POST /api/charts/run` — one chart

**New chart (local):** `local_read_path` + `local_folder_name`  
**New chart (blob):** `blob_container` + `blob_read_path` + `blob_read_folder_name`  
**Resume** (replaces `/rerun`): `chart_id` or `chart_name` with no read path  
Do not mix blob and local. Folder name **is** the chart name. Local paths accept
Windows `\` or `/` (normalized to `/` before use). JSON still needs escaped
backslashes: `"C:\\\\data\\\\inbox"` or prefer `"C:/data/inbox"`.

Write is part of this call — pass `local_write_path` or `blob_write_path`. Default is a **sync**: missing destination files are written, existing ones skipped. `overwrite=true` replaces all.

| Field | Required? | Default | Notes |
|---|---|---|---|
| `local_read_path` / `local_folder_name` | local source | — | Server-side paths |
| `local_write_path` | optional | omit | Write results; omit to keep workspace only |
| `blob_container` / `blob_read_path` / `blob_read_folder_name` | blob source | — | |
| `blob_write_path` | optional | omit | Write under this prefix |
| `chart_id` / `chart_name` | resume | — | No read path; pipeline (+ write if set) |
| `write_mode` | optional | `skip_orig_pages` | or `all_files` |
| `overwrite` | optional | `false` | Sync by default; true = replace destination files |
| `run_id` / `batch_id` | optional | inferred | From path (`Run1`→`R1`, `Batch1`→`B1`) |
| `through` / `only` / `force` / `skip_ocr` | optional | — | See above |

```bash
# macOS / Linux — minimal local
curl -X POST localhost:8001/api/charts/run -H 'Content-Type: application/json' \
  -d '{"local_read_path":"/data/inbox","local_folder_name":"52743839_44976074"}'

# Local + write + stop before Azure OCR
curl -X POST localhost:8001/api/charts/run -H 'Content-Type: application/json' \
  -d '{"local_read_path":"/data/inbox","local_folder_name":"52743839_44976074",
       "local_write_path":"/data/outbox","through":"ocr_final1"}'

# Resume an existing chart (and write missing files)
curl -X POST localhost:8001/api/charts/run -H 'Content-Type: application/json' \
  -d '{"chart_id":7,"local_write_path":"/data/outbox","only":["dos_extract"]}'

# Blob
curl -X POST localhost:8001/api/charts/run -H 'Content-Type: application/json' \
  -d '{"blob_container":"imaging-pipeline",
       "blob_read_path":"run1/batch1",
       "blob_read_folder_name":"52743839_44976074",
       "blob_write_path":"Processed/Run1",
       "run_id":"R1","batch_id":"B1"}'
```

```powershell
# Windows — minimal local (use a Windows path inside the JSON)
curl.exe -X POST localhost:8001/api/charts/run -H "Content-Type: application/json" `
  -d "{\"local_read_path\":\"C:/data/inbox\",\"local_folder_name\":\"52743839_44976074\"}"

# Local + write + stop before Azure OCR
curl.exe -X POST localhost:8001/api/charts/run -H "Content-Type: application/json" `
  -d "{\"local_read_path\":\"C:/data/inbox\",\"local_folder_name\":\"52743839_44976074\",\"local_write_path\":\"C:/data/outbox\",\"through\":\"ocr_final1\"}"

# Resume
curl.exe -X POST localhost:8001/api/charts/run -H "Content-Type: application/json" `
  -d "{\"chart_id\":7,\"local_write_path\":\"C:/data/outbox\",\"only\":[\"dos_extract\"]}"

# Blob
curl.exe -X POST localhost:8001/api/charts/run -H "Content-Type: application/json" `
  -d "{\"blob_container\":\"imaging-pipeline\",\"blob_read_path\":\"run1/batch1\",\"blob_read_folder_name\":\"52743839_44976074\",\"blob_write_path\":\"Processed/Run1\",\"run_id\":\"R1\",\"batch_id\":\"B1\"}"
```

### `POST /api/charts/batch-run` — every chart folder under a path

Canonical path: `/batch-run`. `/batch` is a deprecated alias.

Same vocabulary as `/run` **without** a folder name (each subfolder is a chart). Write is part of this call when a write path is set (same sync behaviour).

| Field | Required? | Default | Notes |
|---|---|---|---|
| `local_read_path` **or** blob pair | one source | — | |
| `local_write_path` / `blob_write_path` | optional | omit | |
| `write_mode` | optional | `skip_orig_pages` | |
| `overwrite` | optional | `false` | Sync by default |
| `sample` | optional | all | Smoke test: first N chart folders (sorted). Prefer over `limit` |
| `limit` | optional | all | Same as `sample` (compat); `sample` wins if both set |
| `workers` | optional | `BATCH_WORKERS` (4) | Must fit DB pool. Charts with ≥`LARGE_CHART_MIN_PAGES` (default 100) pages run at most one-at-a-time while any smaller chart is still pending; when only large charts remain, workers parallelize them. |
| `run_id` / `batch_id` | optional | inferred | |
| `through` / `only` / `force` / `skip_ocr` | optional | — | |

```bash
# macOS / Linux — smoke-test the first 3 charts under the drop
curl -X POST localhost:8001/api/charts/batch-run -H 'Content-Type: application/json' \
  -d '{"local_read_path":"/data/inbox","sample":3,"through":"ocr_prelim","workers":2}'
```

```powershell
# Windows
curl.exe -X POST localhost:8001/api/charts/batch-run -H "Content-Type: application/json" `
  -d "{\"local_read_path\":\"C:/data/inbox\",\"sample\":3,\"through\":\"ocr_prelim\",\"workers\":2}"
```

`202` response includes `charts_found`, `charts_queued` (after `sample`/`limit`), and echoes `sample`.

Progress: `progress.txt` in the batch parent folder (`processing N/X charts…`)
and under each chart’s `imaging/progress.txt` (`processing N/X files…`).

### Removed endpoints

| Path | Status | Use instead |
|---|---|---|
| `POST /api/charts/write` | **410** | Pass `local_write_path` / `blob_write_path` on `/run` or `/batch-run` |
| `POST /api/charts/{id}/rerun` | **410** | `POST /api/charts/run` with `{"chart_id":…}` |

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
| `GET` | `/api/manifest/{record_id}` | Manifest rows: Postgres first, else scan `METADATA_ROOT` |
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
# macOS / Linux
cd core-pipeline
python cli.py serve
python cli.py stages
python cli.py run --local-read-path /data/inbox --folder-name 52743839_44976074
python cli.py batch --local-read-path /data/inbox --limit 1 --through ocr_prelim
python cli.py write 52743839_44976074 --local-write-path /data/outbox
python cli.py rerun 7 --only dos_extract
python cli.py manifest --local ../review-ui/data/metadata/metadata_R1_B1.csv
```

```powershell
# Windows
cd core-pipeline
python cli.py serve
python cli.py stages
python cli.py run --local-read-path C:\data\inbox --folder-name 52743839_44976074
python cli.py batch --local-read-path C:\data\inbox --limit 1 --through ocr_prelim
python cli.py write 52743839_44976074 --local-write-path C:\data\outbox
python cli.py rerun 7 --only dos_extract
python cli.py manifest --local ..\review-ui\data\metadata\metadata_R1_B1.csv
```

### Utilities — load a folder of `metadata_Rn_Bn` files

```bash
# macOS / Linux
cd core-pipeline && source .venv/bin/activate
python ../utilities/load_metadata_manifests.py
python ../utilities/load_metadata_manifests.py /path/to/metadata/
```

```powershell
# Windows
cd core-pipeline
.venv\Scripts\Activate.ps1
python ..\utilities\load_metadata_manifests.py
python ..\utilities\load_metadata_manifests.py C:\path\to\metadata\
```

See [`utilities/README.md`](../utilities/README.md).

---

## Docker

Each service has its own `docker-compose.yml`. Order does not matter.

```bash
# macOS / Linux
cd core-pipeline && cp .env.example .env && docker compose up -d --build
curl -fsS localhost:8001/ready

cd ../review-ui && cp .env.example .env && docker compose up -d --build
# UI: http://localhost:3001   API: http://localhost:3000
```

```powershell
# Windows
cd core-pipeline
Copy-Item .env.example .env
docker compose up -d --build
curl.exe -fsS localhost:8001/ready

cd ..\review-ui
Copy-Item .env.example .env
docker compose up -d --build
# UI: http://localhost:3001   API: http://localhost:3000
```

Local paths in API bodies must be paths **inside the container** (mount the
host folder first).

**Models:** scp weights onto the host under `core-pipeline/models/` (or set
`MODELS_HOST_PATH`). Compose mounts that tree at `/app/core-pipeline/models`.
Docling packages are in the image already — rebuild once after pull:

```bash
docker compose up -d --build
curl -s localhost:8001/health | python -m json.tool   # docling_final1.ready
```

NER (rejection) still needs the GLiNER runtime at build + checkpoints under
`models/ner/`:

```bash
# macOS / Linux
docker compose build --build-arg WITH_NER=true
MEMBER_NER_ENABLED=true docker compose up -d
```

```powershell
# Windows
docker compose build --build-arg WITH_NER=true
$env:MEMBER_NER_ENABLED = "true"
docker compose up -d
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
| Python 3.13 pip failure | Use 3.12 (`py -3.12` on Windows) |
| PowerShell `curl` oddities | Use `curl.exe` |
| `BASE is not recognized` | Bash-only; use `$base = "..."` in PowerShell |
| `Activate.ps1` blocked | `Set-ExecutionPolicy RemoteSigned -Scope CurrentUser` |
| HW `.pth` is ~1 KB after clone | `git lfs install` then `git lfs pull` |

**Pipeline file logs** (Docker): `core-pipeline/logs/core-pipeline.log`, rotated
at midnight to `core-pipeline.log.YYYY-MM-DD` (keep `LOG_BACKUP_DAYS`, default
30). Host path is `LOGS_HOST_PATH` (default `./logs`). Still also on stdout:
`docker compose logs -f api`. Log lines include a worker tag (`[batch-2]`,
`[page-0]`); set `LOG_COLOR=true` (Docker default) to colour those tags in the
terminal.
