# How to run

Two services, started separately. They share only Postgres and Azure Blob, and they do not call each other. `core-pipeline/outputs/charts` is core-pipeline's scratch space; review-ui never reads it. Running it all on the Mac: [§4](#4-running-on-this-mac).

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

Apply the schema once. `schema/schema.sql` is the whole database (Part 1 implemented, Part 2 not yet). An existing database is upgraded with `schema/migrate.sql` (safe to re-run); to start over instead, dump it, then `schema/tools/clear_schema.sql`, then `schema.sql`.

```bash
psql "$DATABASE_URL" -f schema/schema.sql
psql "$DATABASE_URL" -c "SELECT stage_name, pass_no, seq FROM pipeline_stage ORDER BY seq;"
```

Expect 15 phase-1 rows (ocr_quality … imaging_final). An empty `pipeline_stage` makes `/ready` return 503.

### Optional Azure services

Azure Blob and Document Intelligence are optional. Leave them unset and the matching stage records that it ran without them (`GET /health` names the gap). The DOS stage uses no LLM.

---

## 2. Downloading models

Weights are not installed by pip. They live under `core-pipeline/models/`. Run the commands from `core-pipeline` with the virtualenv active.

### Downloaded by command

```bash
cd core-pipeline && source .venv/bin/activate

# Section headers — sentence-transformers/all-MiniLM-L6-v2 → models/semantic-model
python -m modules.ocr.section_header_match --download

# Heading detector → models/layout_heron. GLiNER is the member NER checkpoint.
python -m modules.extraction.util.model_setup

# Member NER, also used by key/value extraction. Folder is
# models/ner/<catalog folder for MEMBER_NER_MODEL_ID>.
python -m modules.member.extractors.ner_based.model_downloader

# Final OCR 1 — four RapidOCR files → models/rapidocr
mkdir -p models/rapidocr
BASE=https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.9.2
curl -fL -o models/rapidocr/PP-OCRv6_det_small.pth "$BASE/torch/PP-OCRv6/det/PP-OCRv6_det_small.pth"
curl -fL -o models/rapidocr/PP-OCRv6_rec_small.pth "$BASE/torch/PP-OCRv6/rec/PP-OCRv6_rec_small.pth"
curl -fL -o models/rapidocr/ch_ptocr_mobile_v2.0_cls_mobile.pth "$BASE/torch/PP-OCRv4/cls/ch_ptocr_mobile_v2.0_cls_mobile.pth"
curl -fL -o models/rapidocr/ppocrv6_dict.txt "$BASE/paddle/PP-OCRv6/rec/PP-OCRv6_rec_small/ppocrv6_dict.txt"
```

The first Docling run also pulls Docling’s own layout weights into the Hugging Face cache. Final OCR 2 is Azure Document Intelligence, so that is an API key, not a file.

`MEMBER_NER_ENABLED` defaults to `true`; put the checkpoints in `models/ner/`. Without them (or with it set to `false`) the member stage runs rules-only and no chart can be rejected.

### Copied in by hand

These are not fetched by the commands above, and they are not baked into the Docker image. Copy the folders into `core-pipeline/models/` on the machine that runs the API. Compose mounts that directory (`MODELS_HOST_PATH`).

| Folder | Files | Used for |
|---|---|---|
| `models/hw/` | `handwritten_printed_convnext_tiny.pth`, `metadata.json`. Optional: `handwritten_printed_convnext_tiny_backup.pth`, `image_type_classification.pkl` | Handwriting and rotation. Missing `.pth` falls back to the `.pkl`. |
| `models/kv-extraction/` | `manifest.json`, `thresholds.json`, `features.json`, and the `ranker_*.txt` files sitting in this folder (version v002) | Key/value ranker. Member, DOS, and page number read its picks. |
| `models/blank-junk/` | `bert/` (DistilBERT), `default.json` | Blank and junk: DistilBERT is one of the two voters. Missing folder, or no torch / transformers, means the vote runs on keywords alone (`/health` says `engine: keywords`). |
| `models/page-family/page-axes/` | `encoder/`, `heads.pt`, `label_space.json` from `training/bert-training/train_axes.py` | Page axes (L1 / L2 / L3 top 3, specialty). `label_space.json` `taxonomy_version` must equal `page_arbitration.json` → `axes.taxonomy_version` (v29.2). Missing folder, another version, or no transformers / torch means the keyword canon (`page_canon.json`) decides (`/health` says so). |

### From Blob instead of by hand (phase 8)

Models (and, optionally, the keyword canon) can be published once to the Blob container
`imaging-models` and fetched by every host. Layout: `manifest.json` (live version + SHA-256
and size of every file), `models/<name>/<version>/…`, `canon/<version>/*.json`. Same
storage account and credentials as `AZURE_STORAGE_*`. Code: `core-pipeline/common/resources.py`.

| Setting (`core-pipeline/.env`, section 15) | Default | Meaning |
|---|---|---|
| `RESOURCES_FROM_BLOB` | `false` | `true`: at API start (`serve`) and before CLI `run` / `batch-run`, download every file that is missing or differs from `manifest.json` into `core-pipeline/models/<name>/` |
| `RESOURCES_CONTAINER` | `imaging-models` | Container holding the manifest |
| `RESOURCES_MODELS` | blank = all | Comma list of model names to fetch on this host |
| `RESOURCES_CANON_FROM_BLOB` | `false` | `true`: also overwrite `core-pipeline/resources/canon/` with the published canon. `false` keeps the git copy |

Each file is downloaded to a temporary file, checked against the manifest's SHA-256 and size,
and only then moved into place; a mismatch is refused and the old file kept. Unchanged files
are not downloaded again (hashes are cached in `models/.resources-state.json`). If Blob cannot
be reached the API starts with what is on disk and `/health` → `resources` says
`reachable: false`. A new model version takes effect on the next restart. The first start on an
empty host downloads everything (about 3 GB) before the API answers.

Publish (from the Mac or the Windows laptop, never from a worker). `._*`, `.DS_Store` and
`__pycache__` are skipped; the manifest write is conditional on its etag, so two publishers
never overwrite each other:

```bash
# macOS / Ubuntu VM
cd core-pipeline && source .venv/bin/activate
python ../utilities/publish_resources.py --model sequencing --version v2 --dry-run   # list only
python ../utilities/publish_resources.py --model sequencing --version v2
python ../utilities/publish_resources.py --model page-family --version v4 --from /data/page-family
python ../utilities/publish_resources.py --canon --version 2026-11-08a
python ../utilities/publish_resources.py --list
python -m common.resources sync --model sequencing          # pull now, into models/
python -m common.resources sync --dest /tmp/check           # pull into another folder
```

```powershell
# Windows (PowerShell)
cd core-pipeline; .venv\Scripts\Activate.ps1
python ..\utilities\publish_resources.py --model sequencing --version v2 --dry-run
python ..\utilities\publish_resources.py --model sequencing --version v2
python ..\utilities\publish_resources.py --model page-family --version v4 --from C:\data\page-family
python ..\utilities\publish_resources.py --canon --version 2026-11-08a
python ..\utilities\publish_resources.py --list
python -m common.resources sync --model sequencing
python -m common.resources sync --dest C:\temp\check
```

Rolling back: publish the old folder again (any version name); the manifest points at the
last one published.

---

## 3. Starting uvicorn

Three terminals.

```bash
# 1. Pipeline API — http://localhost:8001/docs
cd core-pipeline && source .venv/bin/activate
uvicorn apps.orchestrator_api.main:app --host 0.0.0.0 --port 8001
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

---

## 4. Running on this Mac

The one-time setup in §1–§2 is already done on this Mac: Python 3.12 venvs, Tesseract 5.5 and `psql` from Homebrew, models under `core-pipeline/models/`, and `.env` files pointing at the shared Postgres. Every day, start the three processes, each in its own terminal:

```bash
REPO="/Volumes/Extreme SSD/Advantmed/advantmed-imaging-pipeline"   # set this in each terminal

# Terminal 1 — pipeline API, http://localhost:8001/docs (logs: core-pipeline/logs/core-pipeline.log)
cd "$REPO/core-pipeline" && source .venv/bin/activate && uvicorn apps.orchestrator_api.main:app --host 0.0.0.0 --port 8001

# Terminal 2 — review API, http://localhost:8002/docs
cd "$REPO/review-ui/backend" && source .venv/bin/activate && uvicorn app.main:app --host 127.0.0.1 --port 8002 --reload

# Terminal 3 — review web, http://localhost:5174
cd "$REPO/review-ui/frontend" && npm run dev
```

Check that they are up: `curl -fsS localhost:8001/ready` should answer without an error, then open http://localhost:5174. Stop a process with Ctrl+C in its terminal. Docker is not needed on the Mac.

### Run charts from a folder on the Mac

The folder name is the chart name. Pages are copied in, never moved. Results go to the database, and the working files to `core-pipeline/outputs/charts/<chart>`, which is deleted after 7 days without a run.

```bash
# one chart
curl -X POST localhost:8001/api/charts/run -H 'Content-Type: application/json' \
  -d '{"input_type":"local","input_path":"/Volumes/Extreme SSD/Advantmed/Training/Test Charts","chart_name":"22217239_25539465"}'

# every chart in the folder
curl -X POST localhost:8001/api/charts/batch-run -H 'Content-Type: application/json' \
  -d '{"input_type":"local","input_path":"/Volumes/Extreme SSD/Advantmed/Training/Test Charts"}'
```

### Run charts through Blob (as the VM does)

This needs `AZURE_STORAGE_ACCOUNT_NAME` in `core-pipeline/.env`. With `AZURE_STORAGE_AUTH=entra`, the first call opens a browser to sign in.

```bash
cd "$REPO/core-pipeline" && source .venv/bin/activate

# 1. upload: <folder>/<chart>/*.jpg -> raw_input/Batch1/Run1/<chart>/ (add --dry-run to only list)
python ../utilities/upload_charts_to_blob.py "/Volumes/Extreme SSD/Advantmed/Training/Test Charts" \
  --prefix raw_input/Batch1/Run1

# 2. run: results to Processed/Batch1/Run1/<chart>/
curl -X POST localhost:8001/api/charts/batch-run -H 'Content-Type: application/json' \
  -d '{"input_type":"blob","container_name":"imaging-pipeline","input_path":"raw_input/Batch1/Run1",
       "output_path":"Processed/Batch1/Run1"}'
```

Blob paths are case-sensitive: `raw_input` and `Raw_Input` are different folders.

### Watch and re-run

```bash
curl -s localhost:8001/api/charts/by-name/22217239_25539465 | python3 -m json.tool   # progress
tail -f "$REPO/core-pipeline/logs/core-pipeline.log"
cd "$REPO/core-pipeline" && source .venv/bin/activate && python cli.py usage --days 7
```

To run again without paying for OCR again, add `"skip_ocr": true`. To run one stage only, add `"only": ["dos_extract"]`.


---

## 5. Queue mode and the API key (phase 4)

Two settings in `core-pipeline/.env` (section 14) change how `/api/charts/run` and
`/api/charts/batch-run` do the work. Both default to today's behaviour.

| Setting | Default | Meaning |
|---|---|---|
| `RUN_MODE` | `inline` | `inline`: the API runs the chart itself, in the background. `queue`: the API puts one message per chart on the Service Bus queue and the workers run it |
| `SERVICEBUS_CONNECTION_STRING`, `SERVICEBUS_QUEUE` | —, `chart-runs` | The queue (sessions on). Never logged: logs and `/health` show only `sb://<namespace>/ (key ***)` |
| `WORKER_LOCK_RENEW_HOURS` | `6` | How long a worker keeps renewing the 5-minute session lock for one chart |
| `API_KEY_REQUIRED` | `false` | `true` **and** `ORCHESTRATOR_API_KEY` set: every POST needs `X-API-Key: <key>`, else 401. GETs, `/health`, `/ready`, `/docs` stay open. review-ui's Playground does not send the key yet, so keep `false` while it is used |

**How a queued run works.**

- `POST /api/charts/run` registers the chart, creates a `pipeline_jobs` row (`stage_name`
  `queued_run`, status `queued`) and sends one message with `session_id` = the chart name.
  It answers 202 with `run_id` (the job id) and `poll: /api/runs/{run_id}`.
- `POST /api/charts/batch-run` lists the charts with the same filters as inline (`chart_list`,
  `skip_completed` / `only_completed` / `min_run_date`, `sample`) and sends one message per
  chart. A local folder is listed at once (the reply has `charts_queued` and every `run_id`);
  a blob prefix is listed in the background (the reply says so; `GET /api/jobs` shows the runs).
- The message body: `{"kind":"run","run_id":123,"trigger_id":"T…","request":{<the run body>},"min_run_date":null}`.
- A worker (`python -m apps.worker.queue_worker`) takes the next free session, runs the chart
  exactly as inline mode does, then completes the message and marks the job `completed`.
  A failed run is abandoned and marked `failed`; Service Bus delivers it again (to any worker)
  and moves it to the dead-letter queue after 5 deliveries. Two runs of one chart never overlap
  (same session). SIGTERM lets the current chart finish first.
- `GET /api/runs/{run_id}` returns the job row and the chart's status; `?wait=60` (max 60)
  waits for `completed` / `failed`.
- `/health` shows `run_mode`, `api_key_required` and, in queue mode, `queue.reachable`
  (checked once at API start; restart the API to check again).

Start a worker without Docker (one terminal per worker; each loads its own models):

```bash
# macOS / Ubuntu VM
cd core-pipeline && source .venv/bin/activate
python -m apps.worker.queue_worker
```

```powershell
# Windows (PowerShell)
cd core-pipeline; .venv\Scripts\Activate.ps1
python -m apps.worker.queue_worker
```

With Docker: `docker compose up -d --build --scale worker=3` (in `core-pipeline/`).

Run a chart and wait for it, with the key:

```bash
# macOS / Ubuntu VM
KEY=$(grep '^ORCHESTRATOR_API_KEY=' core-pipeline/.env | cut -d= -f2-)
RUN=$(curl -s -X POST localhost:8001/api/charts/run -H 'Content-Type: application/json' \
  -H "X-API-Key: $KEY" \
  -d '{"input_type":"blob","container_name":"imaging-pipeline",
       "input_path":"Raw_Input/Run1/Batch1/DEID_PNGs","chart_name":"52743839_44976074",
       "output_path":"Processed/Run1"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')
curl -s "localhost:8001/api/runs/$RUN?wait=60" | python3 -m json.tool
```

```powershell
# Windows (PowerShell), API on the VM
$key = ((Get-Content core-pipeline\.env | Select-String '^ORCHESTRATOR_API_KEY=') -replace '^ORCHESTRATOR_API_KEY=','')
$body = @{ input_type='blob'; container_name='imaging-pipeline'
           input_path='Raw_Input/Run1/Batch1/DEID_PNGs'; chart_name='52743839_44976074'
           output_path='Processed/Run1' } | ConvertTo-Json
$run = Invoke-RestMethod -Method Post -Uri http://172.20.4.170:8001/api/charts/run `
       -Headers @{ 'X-API-Key' = $key } -ContentType 'application/json' -Body $body
Invoke-RestMethod "http://172.20.4.170:8001/api/runs/$($run.run_id)?wait=60"
```

Without `API_KEY_REQUIRED=true`, leave the header out. A queued run that fails five times is in
the queue's dead-letter sub-queue (Azure portal → Service Bus → `chart-runs` → Dead-letter).

## 6. Step-by-step chart APIs (phase 5)

New routes under `/v1` (the `/api/...` routes are unchanged). Each names a **target**; the
call runs only the target's stages that are not yet done, then stops.

| Endpoint | Target | Stages (in chain order) | `result` |
|---|---|---|---|
| `POST /v1/charts/run` | `full` | all 15 | chart status, page types, member verdict, dates |
| `POST /v1/charts/{chart_name}/ocr` | `ocr` | ocr_quality, ocr_prelim, blank_junk:1, ocr_final1, ocr_final2, section_headers | per page: `engine` (best of azuredocintel / docling / tesseract), `chars`, `chars_by_engine`, `quality` |
| `POST /v1/charts/{chart_name}/page-type` | `page-type` | ocr + kv_extract, blank_junk:2, page_subtype | per page: `page_type`, `page_subtype`, `blank_junk` |
| `POST /v1/charts/{chart_name}/member-verify` | `member-verify` | ocr + kv_extract, blank_junk:2, member_verify | `summary` (member_verification_summary) + `wrong_member_pages` |
| `POST /v1/charts/{chart_name}/dos` | `dos` | page-type + dos_extract | per page dates + `documents` (consecutive pages sharing document-level dates) |
| `GET /v1/runs/{run_id}?wait=N` | — | — | `/api/runs/{run_id}` plus `target` and, when completed, `result` |

The map lives in `core-pipeline/apps/worker/targets.py`.

**Body** (all optional on `/v1/charts/{chart_name}/...`; `chart_name` required on `/v1/charts/run`):
`input_type`, `container_name`, `input_path`, `output_path`, `chart_name`, `skip_ocr`, `rerun`.
A chart already registered needs no body: blob charts are re-read from their stored
`blob_path`, local charts run on their workspace (in `RUN_MODE=queue` a local chart needs
`input_type` + `input_path`). An unregistered chart without a source is a 404.

**What runs.** For each stage of the target, in order, `page_stage_status` is read
(`pages_needing_stage`). The run starts at the first stage not `completed`/`skipped` on every
page and takes every later stage of the target too (they read its output); `rerun=true` takes
all of them. Nothing missing → **200 at once** with the stored `result` (a second call is
instant, no run is created). Otherwise a run is created (`pipeline_jobs`, `stage_name`
`v1_<target>`) and handed to the same path as `/api/charts/run` with `only` = those stages:
a thread in the API (`inline`) or one Service Bus message (`queue`, the body also carries
`target`). The answer is **202** with `run_id` and `stages`; `?wait=N` (max 60) waits and
answers 200 with `result` when it finishes in time. A second call while the same target's
run is queued/running returns that run (`already_running: true`). The stages that run
reprocess every page (as `/api/charts/run` does); `skip_ocr: true` reuses OCR and avoids the
stage-5 bill. With `API_KEY_REQUIRED=true` every `/v1` POST needs `X-API-Key`.

Not built yet: the plan's `version` / `input_hash` staleness — a canon or model change does
not by itself make a completed stage count as missing; pass `rerun: true`.

```bash
# macOS / Ubuntu VM — DOS for a chart already registered, wait up to 60 s
curl -s -X POST "localhost:8001/v1/charts/52743839_44976074/dos?wait=60" \
  -H "X-API-Key: $KEY" | python3 -m json.tool

# a new chart from Blob: OCR only, then poll
RUN=$(curl -s -X POST localhost:8001/v1/charts/52743839_44976074/ocr -H 'Content-Type: application/json' \
  -H "X-API-Key: $KEY" \
  -d '{"input_type":"blob","container_name":"imaging-pipeline","input_path":"Raw_Input/Run1/Batch1/DEID_PNGs"}' \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("run_id",""))')
curl -s "localhost:8001/v1/runs/$RUN?wait=60" | python3 -m json.tool
```

```powershell
# Windows (PowerShell), API on the VM
$h = @{ 'X-API-Key' = $key }
Invoke-RestMethod -Method Post -Headers $h "http://172.20.4.170:8001/v1/charts/52743839_44976074/dos?wait=60"

$body = @{ input_type='blob'; container_name='imaging-pipeline'
           input_path='Raw_Input/Run1/Batch1/DEID_PNGs' } | ConvertTo-Json
$run = Invoke-RestMethod -Method Post -Headers $h -ContentType 'application/json' -Body $body `
       -Uri http://172.20.4.170:8001/v1/charts/52743839_44976074/member-verify
Invoke-RestMethod "http://172.20.4.170:8001/v1/runs/$($run.run_id)?wait=60"

# or curl.exe (not curl)
curl.exe -s -X POST -H "X-API-Key: $key" "http://172.20.4.170:8001/v1/charts/52743839_44976074/page-type?wait=60"
```

Without `API_KEY_REQUIRED=true`, leave the header out.
