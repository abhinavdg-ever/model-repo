# API & running

How to run each service and call its endpoints.
Architecture is in [ARCHITECTURE.md](ARCHITECTURE.md), the algorithms in
[LOGIC.md](LOGIC.md).

**The two services deploy separately.** Each has its own `docker-compose.yml`.
They never call each other — they share a Postgres database and the
`data/folders` volume.

---

## Contents

- [Prerequisites](#prerequisites)
- [Installing dependencies](#installing-dependencies)
- [Running core-pipeline](#running-core-pipeline)
- [Starting the APIs](#starting-the-apis)
- [Running review-ui](#running-review-ui)
- [core-pipeline API reference](#core-pipeline-api-reference)
- [review-ui API reference](#review-ui-api-reference)
- [CLI](#cli)
- [Typical sessions](#typical-sessions)
- [Troubleshooting](#troubleshooting)

---

## Prerequisites

**Database — do this once, before either service starts.**

```bash
psql "$DATABASE_URL" -f schema/v1.sql   # required — what is implemented
psql "$DATABASE_URL" -f schema/v2.sql   # optional — next phase, nothing uses it yet
```

Two files, no migrations directory. **`v1.sql` is everything that is actually
implemented** — 12 tables and 2 views, every one written or read by running
code — and it is required. **`v2.sql` is the next phase**: 14 more tables that
nothing reads or writes yet. Applying it is optional, and V1 never references
it, so you can skip it until the modules land. A pre-v8 database is recreated
from these files, not upgraded in place.

Verify:

```bash
psql "$DATABASE_URL" -c "SELECT stage_name, pass_no, seq FROM pipeline_stage ORDER BY seq;"
```

Twelve rows, eight of them `is_phase1`. If `pipeline_stage` is empty,
core-pipeline's `/ready` returns 503 and no chart can progress.

**External services** — all optional; each one that is missing degrades a
specific stage in a way the run records:

| Service | Needed for | Absent ⇒ |
|---|---|---|
| Azure Blob | chart intake, manifest sweep from blob | ingest fails; `register-local` still works |
| Azure Document Intelligence | final2 OCR | no final2 text; handwritten pages get no pass-2 verdict |
| Azure OpenAI | the DOS LLM pass | DOS is regex-only, `extraction_method='rules'` |
| GLiNER runtime + checkpoints | member NER layer | rules-only; **no document can be Rejected**. Code is present; install `requirements-ner.txt` and run the downloader — [LOGIC.md](LOGIC.md#turning-it-on) |

---

## Installing dependencies

Three layers, installed in this order. Only the first is mandatory.

### 1. System tools

| Tool | Needed by | Install |
|---|---|---|
| Python 3.11+ | everything | — |
| `tesseract` | stage 1, preliminary OCR | `brew install tesseract` · `apt install tesseract-ocr` |
| `psql` | applying the schema | `brew install libpq` · `apt install postgresql-client` |
| Node 20+ | review-ui frontend, only if you run it outside Docker | — |

If `tesseract` is not on `PATH`, set `TESSERACT_CMD` to its absolute path.

### 2. Python packages

```bash
# core-pipeline
cd core-pipeline
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # ~400 MB

# review-ui backend (separate service, separate venv)
cd ../review-ui/backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# test suite — run from the repo root
pip install pytest
python -m pytest tests/ -q               # 108 tests, no database needed
```

### 3. The NER layer (GLiNER) — optional, and it gates rejection

This is the one dependency that changes what the pipeline can *conclude*.
Member verification's `wrong_member_on_page` check reads names off a page using
GLiNER. Without it, verification still runs — the rule pass does the extraction
— but **no page can be marked `wrong_member`, so no document can ever be
Rejected**. Every chart comes back Accepted or `needs_review`.

It is not installed by default because the runtime is ~2.5 GB and the
checkpoints another ~2 GB. Turning it on is three steps plus a flag:

```bash
cd core-pipeline
source .venv/bin/activate

# (a) runtime — gliner, torch, transformers, sentencepiece
pip install -r requirements-ner.txt

# (b) checkpoints — downloads gliner_large / gliner_medium / gliner_low
#     into MEMBER_NER_MODELS_PATH (~2 GB, resumable, skips what is present)
python -m stages.lib.member.extractors.ner_based.model_downloader

# (c) verify — loads each checkpoint and repairs its config paths.
#     Prints "3/3 models ready" and exits 0 when all are usable.
python -m stages.lib.member.extractors.ner_based.model_downloader --check

# (d) switch it on
export MEMBER_NER_ENABLED=true     # or set it in core-pipeline/.env
```

Downloader flags: `--force` re-downloads checkpoints already on disk,
`--check` verifies without downloading. It exits non-zero and names the failed
model id if any checkpoint is incomplete.

Where the checkpoints land, and which models load:

| Variable | Default | Meaning |
|---|---|---|
| `MEMBER_NER_MODELS_PATH` | `Reference/V1 Code/Member_Verification/Models` | Directory holding the checkpoints |
| `MEMBER_NER_MODEL_ID` | `gliner_medium` | Which model the extractor uses |
| `GLINER_LARGE` / `GLINER_MEDIUM` / `GLINER_LOW` | `true` | Which checkpoints are considered available |
| `MEMBER_NER_ENABLED` | `false` | Master switch. Everything above is inert while this is false |

**In Docker**, the runtime is a build arg and the checkpoints are a mount — the
image never contains them:

```bash
cd core-pipeline
# build with the GLiNER runtime baked in
docker compose build --build-arg WITH_NER=true

# download the checkpoints on the host once, then point the mount at them
python -m stages.lib.member.extractors.ner_based.model_downloader
NER_MODELS_HOST_PATH=./models/ner MEMBER_NER_ENABLED=true docker compose up -d
```

`WITH_NER=true` and `MEMBER_NER_ENABLED=true` and `NER_MODELS_HOST_PATH` are all
three required — the runtime, the switch, and the weights. `GET /health` names
whichever one is missing.

### Verifying what is actually on

```bash
curl -s localhost:8001/health | python -m json.tool
```

`member_ner.ready` is the answer. When it is `false`, `member_ner.reason` names
the single precondition to fix. See [LOGIC.md](LOGIC.md#turning-it-on) for what
the rejection path does once it is `true`.

### Other optional dependencies

| Feature | Enable with | Absent ⇒ |
|---|---|---|
| Azure Blob intake | `AZURE_STORAGE_*` in `.env` | `ingest` fails; `register-local` still works |
| Final OCR 2 | `AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT` + `_KEY` | no final2 text; handwritten pages get no pass-2 verdict |
| DOS LLM pass | `AZURE_OPENAI_*` + `DOS_LLM_ENABLED=true` | DOS is regex-only, rows stamped `extraction_method='rules'` |

---

## Running core-pipeline

### Docker (recommended)

```bash
cd core-pipeline
cp .env.example .env      # fill in DATABASE_URL and any Azure credentials
docker compose up -d --build
docker compose logs -f api
```

Serves on **:8001**. Interactive docs at <http://localhost:8001/docs>.

The compose file mounts:

| Host | Container | Mode | Why |
|---|---|---|---|
| `DATA_HOST_PATH` → `../review-ui/data/folders` | `/data/folders` | rw | the chart workspace this service writes |
| `METADATA_HOST_PATH` | `/data/metadata` | rw | mirrored manifest CSVs |
| `REFERENCE_HOST_PATH` → `../Reference` | `/app/Reference` | ro | handwriting model + rotation detector |
| `NER_MODELS_HOST_PATH` | `/app/models/ner` | ro | GLiNER checkpoints, if enabled |

> `DATA_HOST_PATH` must resolve to the **same storage** review-ui mounts. On one
> host a relative path is enough; across hosts use a shared volume or NFS mount.

### Local

```bash
cd core-pipeline
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python cli.py serve                       # or: uvicorn api.main:app --port 8001
```

Needs `tesseract` on PATH (`brew install tesseract` / `apt install tesseract-ocr`),
or set `TESSERACT_CMD`.

### Health

```bash
curl localhost:8001/health   # liveness + which optional features are on
curl localhost:8001/ready    # 503 unless the database is reachable and seeded
```

```json
{
  "status": "ok",
  "member_ner": {
    "enabled": false,
    "deps_installed": false,
    "deps_detail": "gliner not installed (…); pip install -r requirements-ner.txt",
    "weights_present": [],
    "weights_missing": ["gliner_large", "gliner_medium", "gliner_low"],
    "ready": false,
    "reason": "gliner not installed (…); pip install -r requirements-ner.txt"
  },
  "dos_llm_enabled": true,
  "stage_workers": 4
}
```

`member_ner.ready: false` means member verification runs rules-only — no page
can be marked `wrong_member`, so no document can be Rejected. `reason` names the
one precondition to fix. Enabling it:

```bash
cd core-pipeline
pip install -r requirements-ner.txt                                   # runtime
python -m stages.lib.member.extractors.ner_based.model_downloader     # checkpoints
python -m stages.lib.member.extractors.ner_based.model_downloader --check
export MEMBER_NER_ENABLED=true
```

In Docker: `docker build --build-arg WITH_NER=true .` and mount the checkpoints
at `MEMBER_NER_MODELS_PATH` (`NER_MODELS_HOST_PATH` in the compose file).

---

## Starting the APIs

Three processes, three ports. They start independently and in any order.

| Service | Port | Start it | Check it |
|---|---|---|---|
| core-pipeline API | 8001 | `cd core-pipeline && docker compose up -d --build` | <http://localhost:8001/docs> |
| review-ui backend | 3000 | `cd review-ui && docker compose up -d --build` | <http://localhost:3000/docs> |
| review-ui frontend | 3001 | (same compose file) | <http://localhost:3001> |

Ports differ outside Docker: run locally, the review-ui backend defaults to
**8002** (`API_PORT` in `review-ui/.env`) and the Vite dev server to **5174**,
which proxies `/api` to 8002.

The full sequence from an empty machine:

```bash
# 0. schema — once, before anything starts
psql "$DATABASE_URL" -f schema/v1.sql   # required — what is implemented
psql "$DATABASE_URL" -f schema/v2.sql   # optional — next phase, nothing uses it yet

# 1. core-pipeline (writes data/folders)
cd core-pipeline
cp .env.example .env            # fill in DATABASE_URL + any Azure credentials
docker compose up -d --build
curl -fsS localhost:8001/ready  # 200 once the DB is reachable and seeded

# 2. review-ui (reads data/folders, read-only)
cd ../review-ui
cp .env.example .env            # DATA_MODE=local needs no database at all
docker compose up -d --build
open http://localhost:3001
```

Logs and shutdown:

```bash
docker compose logs -f api        # in core-pipeline/
docker compose logs -f backend    # in review-ui/
docker compose down               # per service; they stop independently
```

---

## Running review-ui

```bash
cd review-ui
cp .env.example .env
docker compose up -d --build
```

Backend **:3000**, frontend **:3001** → <http://localhost:3001>.

### Without Docker

```bash
# backend — port 8002
cd review-ui/backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8002 --reload

# frontend — port 5174, proxies /api to 8002
cd ../frontend
npm install
npm run dev
```

Two modes, set by `DATA_MODE`:

| `DATA_MODE` | OCR + imaging read from | Page images |
|---|---|---|
| `local` | `data/folders` — `ocr/*.txt`, `imaging/*.csv` | `pages/` |
| `production` (alias `postgres`) | Postgres v8 tables | `pages/` |

Local Mode needs no database at all. The mode is shown as a pill in the top bar
and returned by `GET /api/config`.

> The review UI is **read-only**. Every route is a `GET`; it records no review
> decisions. `manual_review` and `rejection_results` exist in the schema for a
> later phase and nothing writes them today.

`LOCAL_CACHE_TTL_SECONDS` (default 5) is how long a scan of `data/folders` is
trusted before being rechecked against file mtimes — relevant because
core-pipeline writes that directory while the UI is serving.

---

## core-pipeline API reference

Base: `http://localhost:8001` · OpenAPI: `/openapi.json` · Swagger: `/docs`

Mutating endpoints are **asynchronous**: they return `202 Accepted` immediately
and work continues in the background. Poll the chart endpoint for progress.

### `GET /health`
Liveness plus the feature toggles that change what a run does.

### `GET /ready`
503 unless the database is reachable **and** `pipeline_stage` is seeded. Use as
the container readiness probe.

### `GET /api/stages`
The pipeline's shape, straight from the `pipeline_stage` table.

```bash
curl -s localhost:8001/api/stages | jq '.stages[] | {seq, stage_name, pass_no, is_phase1}'
```

### `POST /api/charts/ingest` → 202

Download a chart folder from blob and run the chain.

```jsonc
{
  "blob_container": "imaging-pipeline",   // required
  "blob_path": "run1/batch1/52743839_44976074",  // required — folder of page images
  "run_id": "R1",                          // optional
  "batch_id": "B1",                        // optional
  "run_pipeline": true,                    // default true
  "force": false                           // default false — resume, don't redo
}
```

```bash
curl -X POST localhost:8001/api/charts/ingest \
  -H 'Content-Type: application/json' \
  -d '{"blob_container":"imaging-pipeline","blob_path":"run1/batch1/52743839_44976074","run_id":"R1","batch_id":"B1"}'
```

```json
{ "status": "accepted", "chart_name": "52743839_44976074",
  "poll": "/api/charts/by-name/52743839_44976074" }
```

The chart id is not known yet — poll the `by-name` URL it hands back.

### `POST /api/charts/register-local` → 202

Register a folder already present under `data/folders` (no blob needed). Useful
for development and for re-processing a chart already on disk.

```bash
curl -X POST localhost:8001/api/charts/register-local \
  -H 'Content-Type: application/json' \
  -d '{"chart_name":"demo_chart_240315_1012"}'
```

```json
{ "status": "accepted", "chart_id": 7, "chart_name": "demo_chart_240315_1012",
  "page_count": 3, "manifest_rows_linked": 1 }
```

Returns synchronously for registration (so you get the id) and runs the chain in
the background.

### `GET /api/charts/{chart_id}` · `GET /api/charts/by-name/{chart_name}`

Chart row, per-stage progress, and the member verification outcome.
`?include_pages=false` omits the page rows.

```bash
curl -s localhost:8001/api/charts/7 | jq '{status: .chart.status, stage: .chart.current_stage,
  stages: [.progress.stages[] | {stage, pass_no, done, total, complete}]}'
```

```json
{
  "chart": { "id": 7, "chart_name": "…", "status": "processing",
             "current_stage": "ocr_final2", "current_pass": 1, "page_count": 500 },
  "progress": {
    "status": "processing", "current_stage": "ocr_final2", "pages_total": 500,
    "stages": [
      {"stage":"ocr_prelim","pass_no":1,"done":500,"total":500,"complete":true},
      {"stage":"ocr_final2","pass_no":1,"done":401,"failed":1,"total":500,"complete":false}
    ]
  },
  "member_verification": null
}
```

`404` if the chart is unknown.

### `POST /api/charts/{chart_id}/rerun` → 202

```jsonc
{
  "force": false,                    // true = reprocess completed pages too
  "only": ["member_verify"]          // optional; "name" = pass 1, "name:2" = pass 2
}
```

```bash
# Resume a chart that failed part-way — completed pages are not redone
curl -X POST localhost:8001/api/charts/7/rerun -H 'Content-Type: application/json' -d '{}'

# Re-run just member verification, e.g. after a manifest arrived
curl -X POST localhost:8001/api/charts/7/rerun -H 'Content-Type: application/json' \
  -d '{"only":["member_verify"],"force":true}'

# Re-run blank/junk pass 2 only
curl -X POST localhost:8001/api/charts/7/rerun -H 'Content-Type: application/json' \
  -d '{"only":["blank_junk:2"],"force":true}'
```

`400` on an unknown stage name (the response lists the valid ones), `404` on an
unknown chart.

> Default is **resume**. Use `force` deliberately: it re-sends every page to
> Azure Document Intelligence, which is billed per page.

### `POST /api/manifest/sweep` → 202

Load a batch manifest. Independent of ingest — sweep before or after, the link
is made either way.

```jsonc
{
  "local_path": "/data/metadata/metadata_R1_B1.csv",  // file OR directory
  // ── or ──
  "blob_container": "imaging-pipeline",
  "blob_prefix": "manifests/run1",

  "run_id": null,        // default: the R# in the filename
  "batch_id": null,      // default: the B# in the filename
  "mirror_local": true   // copy blob manifests into data/metadata
}
```

```bash
curl -X POST localhost:8001/api/manifest/sweep \
  -H 'Content-Type: application/json' \
  -d '{"local_path":"/data/metadata/metadata_R1_B1.csv"}'
```

`400` if neither source is given, or if both are.

Recognised columns (case-insensitive, first match wins):

| Field | Accepted headers |
|---|---|
| record id | `recordId`, `RecordId`, `record_id`, `chart_name`, `ChartName` |
| name parts | `DummyFirstName` / `DummyMiddleName` / `DummyLastName`, `FirstName` / … |
| joined name | `member_name`, `MemberName`, `name` — split as a fallback |
| DOB | `DummyDOB`, `member_dob`, `DOB`, `DateOfBirth` |
| member id | `MemberID`, `member_id`, `external_member_id` |

CSV and XLSX both work. Re-sweeping the same file **updates** rather than
duplicating.

### `GET /api/manifest/{record_id}`
Manifest rows for one record id (= chart folder name). `404` if none.

### `GET /api/jobs`
Recent `pipeline_jobs` rows — the run log.

```bash
curl -s "localhost:8001/api/jobs?chart_id=7&limit=20" \
  | jq '.jobs[] | {stage_name, pass_no, status, pages_done, pages_failed, duration_seconds}'
```

---

## review-ui API reference

Base: `http://localhost:3000`. All read-only.

| Method | Path | Returns |
|---|---|---|
| `GET` | `/api/health` | status + `data_mode` + `mode_label` |
| `GET` | `/api/config` | UI config: mode, blob viewer settings (no secrets) |
| `GET` | `/api/folders` | every chart with page counts and stage badges |
| `GET` | `/api/folders/{id}` | one chart: pages, OCR availability |
| `GET` | `/api/folders/{id}/pages/{n}/image` | the page image |
| `GET` | `/api/folders/{id}/ocr?kind=preliminary\|final1\|final2` | OCR text |
| `GET` | `/api/folders/{id}/imaging` | every imaging result for the chart |
| `GET` | `/api/blob/{id}/pages/{n}/image` | page image proxied from blob |
| `GET` | `/imaging/export.csv?status=&q=` | all imaging output as one CSV |

```bash
curl -s localhost:3000/api/folders | jq '.[] | {id, page_count, ocr_status}'
curl -s "localhost:3000/imaging/export.csv?status=IMAGING_COMPLETED" -o export.csv
```

---

## CLI

Everything the API does, without the HTTP hop. Run from `core-pipeline/`.

```bash
python cli.py serve                          # start the API

python cli.py stages                         # list the chain in order
python cli.py status <chart_id>              # per-stage progress as JSON

python cli.py ingest --container imaging-pipeline \
                     --path run1/batch1/52743839_44976074 \
                     --run-id R1 --batch-id B1
python cli.py ingest ... --no-pipeline       # download only

python cli.py register-local demo_chart_240315_1012
python cli.py run 7                          # resume
python cli.py run 7 --force                  # reprocess everything
python cli.py run 7 --only member_verify     # one stage
python cli.py run 7 --only blank_junk:2      # a specific pass

python cli.py manifest --local ../review-ui/data/metadata/metadata_R1_B1.csv
python cli.py manifest --local ../review-ui/data/metadata/   # whole directory
python cli.py manifest --blob-container imaging-pipeline --blob-prefix manifests/run1
```

---

## Typical sessions

### First run against a new database

```bash
psql "$DATABASE_URL" -f schema/v1.sql   # required — what is implemented
psql "$DATABASE_URL" -f schema/v2.sql   # optional — next phase, nothing uses it yet

cd core-pipeline && cp .env.example .env && docker compose up -d --build
curl -s localhost:8001/ready

# Manifest first, so member verification has something to verify against
curl -X POST localhost:8001/api/manifest/sweep -H 'Content-Type: application/json' \
  -d '{"local_path":"/data/metadata/metadata_R1_B1.csv"}'

# Then the chart
curl -X POST localhost:8001/api/charts/ingest -H 'Content-Type: application/json' \
  -d '{"blob_container":"imaging-pipeline","blob_path":"run1/batch1/52743839_44976074"}'

# Watch it
watch -n5 "curl -s localhost:8001/api/charts/by-name/52743839_44976074 \
  | jq '{status:.chart.status, stage:.chart.current_stage}'"

cd ../review-ui && cp .env.example .env && docker compose up -d --build
open http://localhost:3001
```

### A manifest arrived after the chart

Member verification will have recorded `decision_reason='manifest_missing'`.

```bash
curl -X POST localhost:8001/api/manifest/sweep -H 'Content-Type: application/json' \
  -d '{"local_path":"/data/metadata/metadata_R1_B1.csv"}'

curl -X POST localhost:8001/api/charts/7/rerun -H 'Content-Type: application/json' \
  -d '{"only":["member_verify"],"force":true}'
```

The sweep links the manifest to the chart automatically.

### Batch ingest

```bash
for path in $(az storage blob directory list ... ); do
  curl -s -X POST localhost:8001/api/charts/ingest \
    -H 'Content-Type: application/json' \
    -d "{\"blob_container\":\"imaging-pipeline\",\"blob_path\":\"$path\"}"
done
```

> Each request runs its chain in the same process. There is no queue and no
> concurrency cap across charts yet — feed them in batches sized to the host, or
> put a queue in front. See [ARCHITECTURE.md](ARCHITECTURE.md#known-limits).

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `/ready` → 503 "pipeline_stage is empty" | schema not applied | run `schema/v1.sql` |
| Chart stuck at `ocr_prelim` | Tesseract missing | install it, or set `TESSERACT_CMD` |
| `final2` produces no text | Azure DI not configured | set the endpoint + key; until then handwritten pages get no pass-2 verdict |
| `decision_reason: manifest_missing` | no manifest row for the record | sweep the manifest, then rerun `--only member_verify` |
| `decision_reason` ends `\|ner_disabled` | NER layer not ready | check `GET /health` → `member_ner.reason`; it names the missing piece |
| `ModelLoadError: gliner_* is not in …` | checkpoints absent | `python -m stages.lib.member.extractors.ner_based.model_downloader` |
| `ModelLoadError` mentioning `gliner`/`torch` import | runtime absent | `pip install -r requirements-ner.txt` |
| Review UI badges stale | scan cache | it self-expires in `LOCAL_CACHE_TTL_SECONDS` (default 5); lower it if needed |
| Review UI shows no charts | wrong `DATA_ROOT`, or the volume is not shared | confirm both compose files point at the same storage |
| `duplicate key value violates … chart_list_chart_name_key` | two ingests of one chart name racing | expected — chart names are unique; the second caller should poll instead |
| Re-run re-billed Azure | `force: true` was passed | omit it; the default resumes |

### Useful queries

```sql
-- Where is every in-flight chart?
SELECT chart_name, status, current_stage, current_pass, page_count
FROM chart_list WHERE status = 'processing' ORDER BY updated_at DESC;

-- Which pages are stuck, and why?
SELECT p.page_name, s.stage_name, s.pass_no, s.status, s.skip_reason, s.error_message
FROM page_stage_status s JOIN page_list p ON p.id = s.page_id
WHERE s.chart_id = 7 AND s.status IN ('failed','processing');

-- Charts rejected on wrong-member evidence
SELECT c.chart_name, m.wrong_member_pages, m.reject_threshold, m.decision_reason
FROM member_verification_summary m JOIN chart_list c ON c.id = m.chart_id
WHERE m.document_decision = 'reject';

-- Stage timing
SELECT * FROM pipeline_stage_performance ORDER BY avg_duration_seconds DESC NULLS LAST;
```
