# API & running

How to run each service and call its endpoints. Architecture is in
[ARCHITECTURE.md](ARCHITECTURE.md), the algorithms in [LOGIC.md](LOGIC.md).

**The two services deploy separately.** Each has its own `docker-compose.yml`.
They never call each other — they share a Postgres database and the
`data/folders` volume.

---

## Contents

- [Prerequisites](#prerequisites)
- [Installing dependencies](#installing-dependencies)
- [Deployment modes](#deployment-modes)
  - [Mode A — Local (macOS / Windows / Linux)](#mode-a--local-macos--windows--linux)
  - [Mode B — VM (Linux with Docker)](#mode-b--vm-linux-with-docker)
- [Both modes: review-ui data source](#both-modes-review-ui-data-source)
- [Startup banner and health checks](#startup-banner-and-health-checks)
- [How to run a chart](#how-to-run-a-chart)
- [core-pipeline API reference](#core-pipeline-api-reference)
- [review-ui API reference](#review-ui-api-reference)
- [CLI](#cli)
- [Troubleshooting](#troubleshooting)

---

## Prerequisites

**Database — once, before either service starts.**

```bash
psql "$DATABASE_URL" -f schema/v1.sql   # required
psql "$DATABASE_URL" -f schema/v2.sql   # optional — next phase, nothing uses it yet
psql "$DATABASE_URL" -c "SELECT count(*) FROM pipeline_stage;"   # 8, or 12 with v2
```

PowerShell uses `$env:DATABASE_URL`, cmd.exe `%DATABASE_URL%`.

`v1.sql` is 12 tables and 2 views, every one written or read by running code, and
is required. `v2.sql` is 14 more that nothing touches yet; V1 never references it,
so applying it is optional. There is no migrations directory — a pre-v8 database
is recreated from these files, not upgraded in place. An empty `pipeline_stage`
makes `/ready` return 503 and no chart can progress.

**External services** — all optional. Each one missing degrades a specific stage
in a way the run records:

| Service | Needed for | Absent ⇒ |
|---|---|---|
| Azure Blob | chart intake, manifest sweep from blob | `run`/`batch` fail in blob mode; local paths still work |
| Azure Document Intelligence | final2 OCR | no final2 text; handwritten pages get no pass-2 verdict |
| Azure OpenAI | the DOS LLM pass | DOS is regex-only, `extraction_method='rules'` |
| GLiNER runtime + checkpoints | member NER layer | rules-only; **no document can be Rejected** |

---

## Installing dependencies

Three layers, in this order. Only the first is mandatory.

### 1. System tools

| Tool | Needed by | macOS | Linux | Windows |
|---|---|---|---|---|
| **Python 3.12** (3.11 fine, **not 3.13+**) | everything | `brew install python@3.12` | `apt install python3.12 python3.12-venv` | `winget install Python.Python.3.12` |
| `tesseract` | stage 1, preliminary OCR | `brew install tesseract` | `apt install tesseract-ocr` | [UB Mannheim installer](https://github.com/UB-Mannheim/tesseract/wiki) |
| `psql` | applying the schema | `brew install libpq` | `apt install postgresql-client` | ships with the [PostgreSQL installer](https://www.postgresql.org/download/windows/) |
| Node 20+ | review-ui frontend, outside Docker only | `brew install node` | `apt install nodejs npm` | `winget install OpenJS.NodeJS.LTS` |

- **Python 3.13+ does not work.** `rapidocr-onnxruntime` declares
  `requires_python >=3.6,<3.13`. The Docker image pins `python:3.12-slim`; match it.
- **`TESSERACT_CMD`** — set it to the absolute path whenever `tesseract` is not on
  `PATH`. Always required on Windows, where the installer does not add it:
  `TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe` in `core-pipeline\.env`.

### 2. Python packages

Two services, two virtualenvs, plus one for the tests. On Windows use `py -3.12`
so the launcher does not pick your newest interpreter.

**macOS / Linux**

```bash
cd core-pipeline                                    # service 1
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # ~400 MB
deactivate

cd ../review-ui/backend                             # service 2, separate venv
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
deactivate

cd ../..                                            # tests, from the repo root
python3.12 -m venv .venv-test && source .venv-test/bin/activate
pip install -r tests/requirements.txt
python -m pytest tests/ -q               # 172 tests, no database needed
```

**Windows (PowerShell)**

```powershell
cd core-pipeline
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -V                                # confirm 3.12.x before installing
pip install -r requirements.txt
deactivate

cd ..\review-ui\backend
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
deactivate

cd ..\..
py -3.12 -m venv .venv-test
.venv-test\Scripts\Activate.ps1
pip install -r tests/requirements.txt
python -m pytest tests/ -q
```

If `Activate.ps1` is blocked, allow local scripts once per user:
`Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`. In
cmd.exe activation is `.venv\Scripts\activate.bat`; in Git Bash
`source .venv/Scripts/activate` — `Scripts`, not `bin`.

### 3. The NER layer (GLiNER) — optional, and it gates rejection

**This is the one dependency that changes what the pipeline can conclude.**
Member verification's `wrong_member_on_page` check reads names off a page with
GLiNER. Without it verification still runs — the rule pass extracts — but no page
can be marked `wrong_member`, so **no document can ever be Rejected**. Every chart
comes back Accepted or `needs_review`.

Off by default: the runtime is ~2.5 GB and the checkpoints another ~2 GB.

```bash
# macOS / Linux
cd core-pipeline && source .venv/bin/activate
pip install -r requirements-ner.txt                                 # runtime
python -m stages.lib.member.extractors.ner_based.model_downloader   # ~2 GB of weights
python -m stages.lib.member.extractors.ner_based.model_downloader --check
export MEMBER_NER_ENABLED=true          # or set it in core-pipeline/.env
```

```powershell
# Windows (PowerShell)
cd core-pipeline; .venv\Scripts\Activate.ps1
pip install -r requirements-ner.txt
python -m stages.lib.member.extractors.ner_based.model_downloader
python -m stages.lib.member.extractors.ner_based.model_downloader --check
$env:MEMBER_NER_ENABLED = "true"        # session only — use .env to persist
```

The downloader fetches `gliner_large` / `gliner_medium` / `gliner_low`, skips what
is already present and is resumable. `--force` re-downloads; `--check` loads each
checkpoint and repairs its config paths without downloading, printing
`3/3 models ready` and exiting 0, or exiting non-zero naming the model that failed.

| Variable | Default | Meaning |
|---|---|---|
| `MEMBER_NER_ENABLED` | `false` | Master switch. Everything else is inert while this is false |
| `MEMBER_NER_MODELS_PATH` | `core-pipeline/models/ner` | Directory holding the checkpoints |
| `MEMBER_NER_MODEL_ID` | `gliner_medium` | **The only model knob** — `gliner_large`, `gliner_medium` or `gliner_low`. Readiness and the download both follow it |

In Docker the runtime is a build arg and the weights are a mount, so the image
never contains them. All three lines are required:

```bash
cd core-pipeline
docker compose build --build-arg WITH_NER=true
python -m stages.lib.member.extractors.ner_based.model_downloader   # on the host
NER_MODELS_HOST_PATH=./models/ner MEMBER_NER_ENABLED=true docker compose up -d
```

Confirm with `curl -s localhost:8001/health | python -m json.tool`:
`member_ner.ready` is the answer, and when it is `false`, `member_ner.reason`
names the single precondition to fix. What the rejection path does once it is
`true`: [LOGIC.md](LOGIC.md#turning-it-on).

### Other optional dependencies

| Feature | Enable with | Absent ⇒ |
|---|---|---|
| Azure Blob intake | `AZURE_STORAGE_*` in `.env` | `run`/`batch`/`write` fail in blob mode; local paths still work |
| Final OCR 2 | `AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT` + `_KEY` | no final2 text; handwritten pages get no pass-2 verdict |
| DOS LLM pass | `AZURE_OPENAI_ENDPOINT` + a key **or** a managed identity, + `DOS_LLM_ENABLED=true` | DOS is regex-only, rows stamped `extraction_method='rules'` |

#### Azure OpenAI: key or no key

The endpoint is always required; only the credential differs, chosen by
`AZURE_OPENAI_AUTH`:

| `AZURE_OPENAI_AUTH` | Uses | For |
|---|---|---|
| `key` | `AZURE_OPENAI_API_KEY` | a laptop with a key in `.env` |
| `entra` | `DefaultAzureCredential` — no key at all | an Azure VM with a managed identity, or anywhere `az login` has run; the only option on a resource with `disableLocalAuth` |
| `entra_interactive` *(blob only)* | managed identity → `az` CLI → **browser prompt**, token cached | a developer machine with no managed identity and no `az` on PATH. Never on a headless server — the prompt hangs instead of failing |
| `auto` *(default)* | key if one is set, otherwise entra | leaves existing key setups working unchanged |

Keyless needs `azure-identity` — already in `core-pipeline/requirements.txt` — and
an identity holding **Cognitive Services OpenAI User** *on the OpenAI resource*.
Being in the subscription is not enough, and a missing role assignment comes back
as a 401 that reads exactly like a wrong key.

On a VM with a managed identity the whole configuration is:

```ini
AZURE_OPENAI_ENDPOINT=https://YOUR-RESOURCE.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT=gpt-4o-mini
DOS_LLM_ENABLED=true
# AZURE_OPENAI_AUTH defaults to auto; with no key set, that resolves to entra
```

The stage logs which it resolved to — `DOS: LLM pass enabled (deployment=…,
auth=entra)` — so a VM that quietly fell back to a stale key is visible in the run
log rather than inferred.

To test the endpoint on its own, fill in the constants at the top of
`check_azure_openai.py` and run `python check_azure_openai.py`. It reads no `.env`
and imports nothing from the pipeline, so it isolates the endpoint from the rest
of the configuration, and exits 1 naming what to fix.
`tests/test_azure_openai.py` runs the real DOS prompt under pytest and skips
itself when no credentials are usable — including the keyless case.

---

## Deployment modes

There are two, and they do not mix. Pick one per machine.

| | **Mode A — Local** | **Mode B — VM** |
|---|---|---|
| For | development on your laptop | the deployed environment |
| Platform | macOS, Windows, Linux | Linux with Docker |
| How | uvicorn in your own venv | `docker compose` |
| core-pipeline | :8001 | :8001 |
| review-ui API | **:8002** | **:3000** |
| review-ui web | **:5174** (Vite dev server) | **:3001** (nginx) |
| Reload on edit | yes (`--reload`) | no — rebuild the image |
| Python 3.12 / tesseract | on the host | in the image |

**The ports differ between the modes.** `localhost:3000` is nothing in Mode A, and
`localhost:8002` is nothing in Mode B.

Both modes need the schema applied first, and both run the two services
independently — neither calls the other.

---

## Mode A — Local (macOS / Windows / Linux)

Four terminals: nothing daemonises, so each process holds its own. Needs the code,
Python 3.12, tesseract and the venvs from
[Installing dependencies](#installing-dependencies).

**1. Schema — once.** See [Prerequisites](#prerequisites). Skip it entirely if you
only want the review UI in Local Mode, which reads files, not Postgres.

**2. core-pipeline — :8001**

```bash
cd core-pipeline
cp .env.example .env          # DATABASE_URL + any Azure credentials
source .venv/bin/activate

python cli.py serve                                          # either this
uvicorn api.main:app --host 127.0.0.1 --port 8001 --reload   # or this
```

```powershell
cd core-pipeline
Copy-Item .env.example .env
.venv\Scripts\Activate.ps1

python cli.py serve                                          # either this
uvicorn api.main:app --host 127.0.0.1 --port 8001 --reload   # or this
```

> **Both lines start the same app; run one.** Use `uvicorn` when you want
> `--reload`, and run it **from `core-pipeline/`** — uvicorn puts the *current*
> directory on `sys.path`, and `api/` lives there. (The Dockerfile does the same
> with `WORKDIR /app`.) `python cli.py serve` works from any directory, because
> `cli.py` adds its own directory to the path, and it binds `API_HOST`/`API_PORT`
> from `.env` — `0.0.0.0:8001` — with reload off.

<http://localhost:8001/docs>. Needs `tesseract` on PATH or `TESSERACT_CMD` set.

**3. review-ui backend — :8002**

```bash
cd review-ui/backend
cp ../.env.example ../.env    # DATA_MODE=local needs no database
source .venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8002 --reload
```

```powershell
cd review-ui\backend
Copy-Item ..\.env.example ..\.env
.venv\Scripts\Activate.ps1
uvicorn app.main:app --host 127.0.0.1 --port 8002 --reload
```

<http://localhost:8002/docs>.

**4. review-ui frontend — :5174**

```bash
cd review-ui/frontend && npm install && npm run dev
```

<http://localhost:5174>. Vite proxies `/api` to **8002**, which is why the backend
must be on that port in this mode.

Stop with Ctrl-C in each terminal. Nothing is left running.

---

## Mode B — VM (Linux with Docker)

Two compose projects, deployed independently; neither depends on the other being
present.

**1. Schema — once,** from anywhere that can reach the database. See
[Prerequisites](#prerequisites).

**2. core-pipeline — :8001**

```bash
cd core-pipeline
cp .env.example .env          # DATABASE_URL + Azure credentials
docker compose up -d --build
docker compose logs -f api
curl -fsS localhost:8001/ready
```

| Host | Container | Mode | Why |
|---|---|---|---|
| `DATA_HOST_PATH` → `../review-ui/data/folders` | `/data/folders` | rw | the chart workspace this service writes |
| `METADATA_HOST_PATH` | `/data/metadata` | rw | mirrored manifest CSVs |
| `NER_MODELS_HOST_PATH` → `./models/ner` | `/app/models/ner` | ro | GLiNER checkpoints, if enabled |

> `DATA_HOST_PATH` must resolve to the **same storage** review-ui mounts. On one VM
> a relative path is enough; across hosts use a shared volume or an NFS mount. If
> they diverge, the pipeline writes charts the UI never sees, with no error on
> either side.

**3. review-ui — :3000 API, :3001 web**

```bash
cd ../review-ui
cp .env.example .env
docker compose up -d --build
```

<http://localhost:3001>. It mounts the chart workspace **read-only**.

**Updating:** `git pull`, then `docker compose up -d --build` in each directory. An
image rebuild is required for any code change — there is no reload in this mode.
`docker compose down` stops each independently.

**Behind a reverse proxy:** both services bind all interfaces inside their
containers and publish to the host. Neither terminates TLS and **neither has
authentication** — put them behind nginx/Caddy with auth before exposing either
port beyond the VM. See
[ARCHITECTURE.md § Known limits](ARCHITECTURE.md#6-known-limits).

---

## Both modes: review-ui data source

`DATA_MODE` decides where the UI reads results from, independently of which
deployment mode you are in.

| `DATA_MODE` | OCR + imaging read from | Page images | Database needed |
|---|---|---|---|
| `local` | `data/folders` — `ocr/*.txt`, `imaging/*.csv` | `pages/` | no |
| `production` (alias `postgres`) | Postgres v8 tables | `pages/` | yes |

The mode shows as a pill in the top bar and is returned by `GET /api/config`.

`LOCAL_CACHE_TTL_SECONDS` (default 5) is how long a scan of `data/folders` is
trusted before being rechecked against file mtimes — relevant because
core-pipeline writes that directory while the UI is serving.

> The review UI is **read-only**. Every route is a `GET`; it records no review
> decisions. `manual_review` and `rejection_results` exist in the schema for a
> later phase and nothing writes them today.

---

## Startup banner and health checks

Every start logs what it resolved — password masked — from the same function that
serves `/health`, so the banner and the endpoint cannot disagree.

```
INFO core-pipeline starting
INFO   database    : postgresql://postgres:***@localhost:5432/imaging_outputs
INFO   data root   : .../review-ui/data/folders
INFO   metadata    : .../review-ui/data/metadata
INFO   workers     : 4
INFO   schema      : OK, 8 stage(s) registered
INFO   blob        : OK — entra, account=acct, container=imaging-pipeline
INFO   final2 OCR  : off — not set: AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT, ..._KEY
INFO   DOS LLM     : OK — deployment=gpt-4o, auth=entra
INFO   member NER  : off — MEMBER_NER_ENABLED=false
```

An unreachable database is a warning and the service starts anyway — `/docs` and
`/health` exist precisely to work when it does not. `DATABASE_URL` is read once at
startup, so restart after editing `.env`.

A blob line reading `configured but UNREACHABLE` means the credentials resolved
and the call was refused — usually an Entra identity without **Storage Blob Data
Reader** on the account, which is a 403 that reads like a missing container.

```bash
curl localhost:8001/health   # liveness + which optional features are actually on
curl localhost:8001/ready    # 503 unless the database is reachable and seeded
```

On Windows PowerShell use `curl.exe`, not `curl`. Full payload: [`GET /health`](#get-health).

---

## How to run a chart

Three verbs cover everything: **`run`** one chart, **`batch`** a folder of them,
**`write`** the results back out. Each is also a CLI subcommand with the same
options — see [CLI](#cli). Field-by-field contracts are in the
[API reference](#core-pipeline-api-reference) below.

```bash
# Read it, run the chain, write the results — one call
curl -X POST localhost:8001/api/charts/run -H 'Content-Type: application/json' \
  -d '{"local_read_path":"/data/inbox",
       "local_folder_name":"52743839_44976074",
       "local_write_path":"/data/outbox"}'

# Watch it
curl -s localhost:8001/api/charts/by-name/52743839_44976074 | jq '.chart.status'
```

Reads `/data/inbox/52743839_44976074/`, writes `/data/outbox/52743839_44976074/`.
Drop `local_write_path` to run without writing.

### The folder name is the chart name

A source is a read path **plus** a folder name, and they resolve together. The
write path resolves the same way with the same name, so a chart keeps its identity
on both sides and two charts written to one destination cannot merge.

| You send | Reads | Chart name |
|---|---|---|
| `local_read_path: /data/inbox`, `local_folder_name: 52743839_44976074` | `/data/inbox/52743839_44976074/` | `52743839_44976074` |
| `blob_read_path: run1/batch1`, `blob_read_folder_name: 52743839_44976074` | `run1/batch1/52743839_44976074/` | `52743839_44976074` |

It is **given, never inferred**: the name becomes `chart_list.chart_name`, prefixes
every output CSV, and is the key the member manifest joins on (`record_id`) — too
load-bearing to guess from a path.

It is sanitised to `[A-Za-z0-9._-]`, so `My Chart 001` becomes `My_Chart_001`. If
the manifest carries the unsanitised string, nothing matches and the chart returns
`needs_review` with `decision_reason='manifest_missing'` and no obvious cause. The
response echoes the resolved name back — worth a glance on the first chart of a new
drop.

**Read and write stay on one backend.** Blob in, blob out; local in, local out.
Mixing them is a `400`, because a write landing somewhere the caller did not mean
is worse than an error.

### Where the pages can live

The source folder may hold the images directly or in a subfolder; subfolders are
always searched, so a chart keeping its scans in `pages/` needs no flag. The source
is always **copied**, never moved, so a failed run is a no-op rather than data loss.

```
/data/inbox/52743839_44976074/1.jpg          ← works
/data/inbox/52743839_44976074/pages/1.jpg    ← also works
```

Under Docker a local path must be a path **inside the container** — mount the
folder first; the host filesystem is not visible to the service.

### Running part of the chain

`through` and `only` answer different questions. Both are accepted by `/run`,
`/batch` and `/rerun`, spelled identically.

| | Means | Use when |
|---|---|---|
| `through` | Run from the top, **stop after** this stage | You want the first N stages and nothing paid for beyond them |
| `only` | Run **just** these stages, whatever ran before | The earlier output on disk is good and one step changed |

A stage is named `ocr_final2` for pass 1, or `blank_junk:2` for pass 2. A bare name
always means pass 1 — never "whichever pass exists". An unknown name is a `400`
naming the known stages, rather than a `202` and a run that silently does nothing.

```bash
# Everything up to and including Final OCR 2, then stop
curl -X POST localhost:8001/api/charts/run -H 'Content-Type: application/json' \
  -d '{"local_read_path":"/data/inbox","local_folder_name":"chart_x","through":"ocr_final2"}'

# Just the DOS stage, on a chart whose OCR is already done
curl -X POST localhost:8001/api/charts/3/rerun -H 'Content-Type: application/json' \
  -d '{"only":["dos_extract"]}'
```

`only` runs a stage against whatever its inputs are on disk. That makes it the right
tool for re-running the last step after a fix, and the wrong tool on a chart that has
never run. `through: "ocr_prelim"` is the cheapest way to confirm intake picked up
the right pages before committing to stage 5.

### A whole drop at once

```bash
curl -X POST localhost:8001/api/charts/batch -H 'Content-Type: application/json' \
  -d '{"local_read_path":"/data/inbox/2026-09-13","local_write_path":"/data/outbox","limit":1}'
```

Each immediate subfolder holding at least one image is one chart. Folders with no
images are skipped rather than attempted, dotfolders are ignored, and macOS `._`
stubs do not make a folder count. Point it at a single chart folder and it runs just
that one, so the same command works for a drop of fifty or a drop of one.

**Charts run one at a time, deliberately.** Each already fans out across its pages
(`STAGE_WORKERS`), and stage 5 is billed per page, so overlapping them multiplies
memory and spend without finishing sooner. See
[PLAN.md](../PLAN.md#proposed-shard-a-batch-across-n-chart-workers) for the design
that would change that, and why it is not built.

**One bad folder does not stop the batch, and neither does one unwritable
destination.** A chart that fails is recorded and the run continues; a chart that ran
but could not be written is marked `write.status = "failed"` while staying
`status = "completed"`, because the pipeline did its job and `/api/charts/write` can
retry without reprocessing.

**Use `limit: 1` first.** For a local read path the chart list is resolved *before*
returning, so `charts_found` tells you how many will run and a wrong path is a `400`
immediately rather than an empty batch an hour later. Pair it with
`"through": "ocr_prelim"` to dry-run a large drop for the price of Tesseract.

A batch can run for hours, so progress goes to the server log, not the response:

```
INFO Batch: 37 chart folder(s) under /data/inbox/2026-09-13
INFO [1/37] 52743839_44976074
INFO Background batch finished: ... -> 36/37 completed, 1 failed in 4213.8s
WARNING   failed: 52744171_44423942 — RuntimeError: No images in ...
```

### Writing the results back out

```bash
curl -X POST localhost:8001/api/charts/write -H 'Content-Type: application/json' \
  -d '{"chart_name":"52743839_44976074","blob_container":"imaging-pipeline","blob_write_path":"Processed/Run1"}'
```

`write` **copies** — the workspace under `data/folders` is left intact, so review-ui
keeps serving the chart and you can write it to a second destination without
re-running anything. A destination that already holds files needs `overwrite: true`.
macOS AppleDouble stubs (`._1.jpg`) are never written out.

`write_mode` decides what goes:

| | Sends | Use when |
|---|---|---|
| `skip_orig_pages` *(default)* | `corrected-pages/`, `ocr/`, `imaging/` | The originals came **from** the destination you are writing back to. Re-sending them doubles storage and transfer for bytes already there |
| `all_files` | those **plus** `pages/` | The destination is a handoff that must stand alone |

Corrected pages go in **both** modes: the pipeline produced those and the source does
not have them. On a 3-page chart that is 3 files / 3 KB by default against 6 files /
885 KB with `all_files`. If a chart has produced no output yet, the default writes
nothing and says so, naming `all_files` as the way to send the originals anyway.

Nothing prunes `data/folders` after a write. On a long-running host it grows
monotonically; cleanup is a manual delete once you have confirmed the write landed.

### Common situations

```bash
# A manifest arrived after the chart — it will have recorded
# decision_reason='manifest_missing'. The sweep links it automatically.
curl -X POST localhost:8001/api/manifest/sweep -H 'Content-Type: application/json' \
  -d '{"local_path":"/data/metadata/metadata_R1_B1.csv"}'
curl -X POST localhost:8001/api/charts/7/rerun -H 'Content-Type: application/json' \
  -d '{"only":["member_verify"],"force":true}'

# A chart that died part-way. Re-runs resume: a chart that failed at DOS on
# page 400 of 500 finishes without repeating paid OCR.
curl -X POST localhost:8001/api/charts/7/rerun \
  -H 'Content-Type: application/json' -d '{}'
```

Reach for `force` only when you mean "reprocess everything" — it re-sends every page
to Azure Document Intelligence, which is billed per page.

---

## core-pipeline API reference

Base: `http://localhost:8001` · OpenAPI: `/openapi.json` · Swagger: `/docs`

Mutating endpoints are **asynchronous**: they return `202 Accepted` immediately and
work continues in the background. Poll the chart endpoint for progress.

### Signing in to the review UI

`imaging-user` / `aipocpw2026`. Shared with everyone using the POC, inlined into the
JS bundle and committed to the repository — it keeps a casual visitor off the page
and is **not** a security control. The backend has no auth on any route, so
everything behind the screen is reachable without it.

Override per deployment with `VITE_LOGIN_USERNAME` / `VITE_LOGIN_PASSWORD` in
`review-ui/.env`. Vite inlines these at **build** time, so a plain
`docker compose up -d` keeps serving the old pair:

```bash
cd review-ui && docker compose up -d --build frontend   # --build is the point
```

### `GET /health`

Liveness, plus every optional feature and the one precondition each is missing.

```jsonc
{
  "status": "ok",
  "blob":        {"container": "imaging-pipeline", "account": "acct",
                  "auth": "entra", "ready": true},
  "azure_document_intelligence": {"endpoint": null, "ready": false,
                  "reason": "not set: AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT, ..._KEY"},
  "dos_llm":     {"enabled": true, "deployment": "gpt-4o", "auth": "entra",
                  "ready": true},
  "member_ner":  {"enabled": false, "ready": false,
                  "reason": "MEMBER_NER_ENABLED=false"},
  "stage_workers": 4
}
```

Every capability that is not `ready` carries a `reason` naming the single thing to
fix. What each costs when off:

| Not ready | Consequence |
|---|---|
| `blob` | `run`/`batch`/`write` work from local paths; blob mode fails |
| `azure_document_intelligence` | final2 produces no text; handwritten pages get no pass-2 verdict |
| `dos_llm` | DOS is regex-only, rows stamped `extraction_method='rules'` |
| `member_ner` | no page can be `wrong_member`, so **no document can be Rejected** |

For `member_ner`, `reason` reports the *first* thing that stops it working —
switched off beats missing runtime beats missing checkpoints — so it never tells you
to install 2.5 GB you have deliberately disabled. `model_id` is the single checkpoint
this run needs; `weights_missing` lists it if absent.

**This endpoint opens no sockets.** It reports configuration — environment and
installed packages — so it stays fast and cannot hang when Azure is down.
Credentials being present is not proof the role is assigned; for blob, the startup
log does one bounded round trip and reports that separately.

### `GET /ready`

503 unless the database is reachable **and** `pipeline_stage` is seeded. Use as the
container readiness probe.

### `GET /api/stages`

The pipeline's shape, straight from the `pipeline_stage` table.

```bash
curl -s localhost:8001/api/stages | jq '.stages[] | {seq, stage_name, pass_no, is_phase1}'
```

### `POST /api/charts/run` → 202

Read one chart, run the chain, and optionally write the results — one call. Concepts
and examples: [How to run a chart](#how-to-run-a-chart).

| Field | Mode | Meaning |
|---|---|---|
| `blob_container` | blob | Container, for **both** read and write |
| `blob_read_path` + `blob_read_folder_name` | blob | Prefix, and the chart folder under it. Both required |
| `blob_write_path` | blob | Prefix to write to. Omit to run without writing |
| `local_read_path` + `local_folder_name` | local | Directory, and the chart folder under it. Both required |
| `local_write_path` | local | Directory to write to. Omit to run without writing |
| `write_mode` | both | `skip_orig_pages` (default) or `all_files` |
| `overwrite` | both | Replace files already at the destination |
| `through`, `only` | both | Run part of the chain |
| `force` | both | Reprocess completed pages. **Stage 5 is billed per page** |

| Body | Result |
|---|---|
| both a blob and a local source, or neither | `400` |
| read path without folder name, or vice versa | `400`, naming the missing field |
| blob source with `local_write_path`, or the reverse | `400` |
| unknown `write_mode` | `400` |
| unknown `through` / `only` stage | `400`, naming the known stages |

Local mode resolves the folder **before** returning, so a bad path is a `400`
immediately rather than a `202` and a silent background failure:

```json
{
  "status": "accepted", "mode": "local",
  "chart_id": 12, "chart_name": "52754737_48221214",
  "source": "/data/inbox/52754737_48221214",
  "imported": 34,
  "page_count": 34,
  "write": {
    "destination": "/data/outbox/52754737_48221214",
    "write_mode": "skip_orig_pages",
    "overwrite": false
  },
  "poll": "/api/charts/12"
}
```

`write` is `null` when no write path was given — the chart still runs, and
`POST /api/charts/write` can send it later. **A write failure does not fail the
run:** the chart is in the workspace either way, and `/api/charts/write` retries
without reprocessing.

### `POST /api/charts/write` → 202

Write an already-run chart out, **without reprocessing it**. Use it to send a chart
to a second destination, or to export one that ran before a write path was given.

| Field | Default | Meaning |
|---|---|---|
| `chart_name` | — | Folder under `data/folders`. Required — this is the chart |
| `local_write_path` | — | Destination directory on the server |
| `blob_container` + `blob_write_path` | — | Destination container and prefix |
| `write_mode` | `skip_orig_pages` | As `/run` |
| `overwrite` | `false` | Replace files already there |

The chart name is appended to the destination, exactly as `/run` does:
`<write_path>/<chart_name>/`.

| Body | Result |
|---|---|
| two destinations, or none | `400` |
| `blob_write_path` without `blob_container` | `400` |
| unknown `write_mode` | `400` |
| chart has no workspace on disk | `404`, naming the path it looked for |
| destination not empty, no `overwrite` | `409` for a local destination |

### `POST /api/charts/batch` → 202

Run **every chart under a read path**, sequentially, and optionally write each. The
same fields as `/run` **minus the folder name** — here every sub-folder holding
images is one chart and names itself.

| Field | Meaning |
|---|---|
| `blob_container` + `blob_read_path` | Container, and the prefix whose sub-folders are charts |
| `local_read_path` | Parent directory whose sub-folders are charts |
| `blob_write_path` / `local_write_path` | Where each chart is written, under its own folder name. Omit to run without writing |
| `write_mode`, `overwrite`, `through`, `only`, `force` | As `/run` |
| `limit` | Only the first N charts. **Use `limit: 1` for a dry run** |

Read and write stay on the same backend, exactly as in `/run`. **Batch is `/run`,
once per folder** — each chart goes through the same call, so an option means the
same thing in both places and a batch of one is indistinguishable from a single run.

```json
{
  "status": "accepted",
  "mode": "local",
  "source": "/data/inbox/2026-09-13",
  "charts_found": 37,
  "limit": null,
  "write": {
    "destination": "/data/outbox",
    "write_mode": "skip_orig_pages",
    "note": "each chart is written under its own folder name"
  },
  "note": "runs sequentially; watch the server log for [n/total] progress"
}
```

### `GET /api/charts/{chart_id}` · `GET /api/charts/by-name/{chart_name}`

Chart row, per-stage progress, and the member verification outcome.
`?include_pages=false` omits the page rows. `404` if the chart is unknown.

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

### `POST /api/charts/{chart_id}/rerun` → 202

```jsonc
{
  "force": false,                    // true = reprocess completed pages too
  "only": ["member_verify"],         // optional; "name" = pass 1, "name:2" = pass 2
  "through": null                    // optional; run from the top, stop after this stage
}
```

`400` on an unknown stage name (the response lists the valid ones), `404` on an
unknown chart. The default is **resume** — `force` re-sends every page to Azure
Document Intelligence, which is billed per page.

### `POST /api/manifest/sweep` → 202

Load a batch manifest. Independent of ingest — sweep before or after, the link is
made either way. `400` if neither source is given, or if both are.

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

Base: `http://localhost:3000` (Mode B) or `:8002` (Mode A). All read-only.

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

Everything the API does, without the HTTP hop, and inline rather than in the
background — `run` and `batch` print a JSON summary when the chain finishes. Run
from `core-pipeline/`. Options mean exactly what they mean in the API.

```bash
python cli.py serve                          # start the API
python cli.py stages                         # list the chain in order
python cli.py status <chart_id>              # per-stage progress as JSON

# run — one chart. --folder-name is required and becomes the chart name.
python cli.py run --local-read-path /data/inbox --folder-name 52743839_44976074
python cli.py run --local-read-path /data/inbox --folder-name 52743839_44976074 \
                  --local-write-path /data/outbox              # run AND write
python cli.py run --blob-container imaging-pipeline \
                  --blob-read-path Raw_Input/Run1/Batch1/DEID_PNGs \
                  --folder-name 52743839_44976074 \
                  --blob-write-path Processed/Run1
python cli.py run --local-read-path ./drops --folder-name chart_x --through ocr_final2
python cli.py run --local-read-path ./drops --folder-name chart_x --no-pipeline  # intake only

# batch — every chart under a path, one at a time. One bad folder does not stop it.
python cli.py batch --local-read-path /data/inbox/2026-09-13
python cli.py batch --local-read-path ./drops --local-write-path ./out
python cli.py batch --local-read-path ./drops --limit 2 --through ocr_prelim   # dry run
python cli.py batch --blob-container imaging-pipeline \
                    --blob-read-path Raw_Input/Run1/Batch1 \
                    --blob-write-path Processed/Run1

# write — an already-run chart back out, no reprocessing.
python cli.py write 52743839_44976074 --local-write-path /data/outbox
python cli.py write 52743839_44976074 --local-write-path /data/outbox --all-files
python cli.py write 52743839_44976074 --blob-container imaging-pipeline \
                                      --blob-write-path Processed/Run1 --overwrite

# rerun — by chart_id. Default resumes; --force reprocesses and costs money.
python cli.py rerun 7
python cli.py rerun 7 --force
python cli.py rerun 7 --only member_verify   # one stage; repeatable
python cli.py rerun 7 --only blank_junk:2    # a specific pass
python cli.py rerun 7 --through ocr_final1   # from the top, stop after Final OCR 1

# manifest — a local file, a whole local directory, or a blob prefix.
# All three upsert: re-running with a corrected CSV updates in place.
python cli.py manifest --local ../review-ui/data/metadata/metadata_R1_B1.csv
python cli.py manifest --local ../review-ui/data/metadata/         # whole directory
python cli.py manifest --blob-container imaging-pipeline --blob-prefix manifests/run1
python cli.py manifest --local ./m.csv --run-id R1 --batch-id B1   # override parsed ids
```

`--skip-orig-pages` (default) and `--all-files` are the two `write_mode` values;
`--overwrite` replaces files already at the destination. `--run-id` / `--batch-id`
are accepted by `run` and `batch` as well.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: No module named 'api'` | `uvicorn` run outside `core-pipeline/` | `cd core-pipeline` first, or use `python cli.py serve`, which works from anywhere |
| `/ready` → 503 "pipeline_stage is empty" | schema not applied | run `schema/v1.sql` |
| Chart stuck at `ocr_prelim` | Tesseract missing | install it, or set `TESSERACT_CMD` |
| Rotated pages OCR as gibberish | Tesseract `osd` traineddata missing, so stage 1 cannot detect orientation | install the full Tesseract package; pages pass through unrotated until then |
| `final2` produces no text | Azure DI not configured | set the endpoint + key; until then handwritten pages get no pass-2 verdict |
| `decision_reason: manifest_missing` | no manifest row for the record | sweep the manifest, then rerun `--only member_verify` |
| `decision_reason` ends `\|ner_disabled` | NER layer not ready | `GET /health` → `member_ner.reason` names the missing piece |
| `ModelLoadError: gliner_* is not in …` | checkpoints absent | `python -m stages.lib.member.extractors.ner_based.model_downloader` |
| `ModelLoadError` mentioning `gliner`/`torch` import | runtime absent | `pip install -r requirements-ner.txt` |
| Log flooded with `http_logging_policy: Request headers:` | Azure SDK logging at INFO | `AZURE_LOG_LEVEL` defaults to `WARNING`; something in your environment has set it to INFO or DEBUG |
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
