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
- [**Deployment modes**](#deployment-modes) — the two ways to run this
  - [Mode A — Local (macOS / Windows / Linux)](#mode-a--local-macos--windows--linux)
  - [Mode B — VM (Linux with Docker)](#mode-b--vm-linux-with-docker)
- [Both modes: review-ui data source](#both-modes-review-ui-data-source)
- [Startup banner](#startup-banner)
- [Health checks](#health-checks)
- [Windows notes](#windows-notes)
  - [Calling the API from Windows](#calling-the-api-from-windows) — curl/JSON quoting
  - [Installing from a ZIP](#installing-from-a-zip) — when `git clone` is blocked
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

On **Windows PowerShell** the variable syntax differs — `$env:` not `$`:

```powershell
psql $env:DATABASE_URL -f schema/v1.sql
psql $env:DATABASE_URL -f schema/v2.sql
```

and in **cmd.exe** it is `%DATABASE_URL%`.

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
| Azure Blob | chart intake, manifest sweep from blob | `run`/`batch` fail in blob mode; `local_path` still works |
| Azure Document Intelligence | final2 OCR | no final2 text; handwritten pages get no pass-2 verdict |
| Azure OpenAI | the DOS LLM pass | DOS is regex-only, `extraction_method='rules'` |
| GLiNER runtime + checkpoints | member NER layer | rules-only; **no document can be Rejected**. Code is present; install `requirements-ner.txt` and run the downloader — [LOGIC.md](LOGIC.md#turning-it-on) |

---

## Installing dependencies

Three layers, installed in this order. Only the first is mandatory.

### 1. System tools

| Tool | Needed by | macOS | Linux | Windows |
|---|---|---|---|---|
| **Python 3.12** (3.11 also fine, **not 3.13+**) | everything | `brew install python@3.12` | `apt install python3.12 python3.12-venv` | `winget install Python.Python.3.12` |
| `tesseract` | stage 1, preliminary OCR | `brew install tesseract` | `apt install tesseract-ocr` | [UB Mannheim installer](https://github.com/UB-Mannheim/tesseract/wiki) |
| `psql` | applying the schema | `brew install libpq` | `apt install postgresql-client` | ships with the [PostgreSQL installer](https://www.postgresql.org/download/windows/) |
| Node 20+ | review-ui frontend, only outside Docker | `brew install node` | `apt install nodejs npm` | `winget install OpenJS.NodeJS.LTS` |

> **Python 3.13+ does not work.** `rapidocr-onnxruntime` declares
> `requires_python >=3.6,<3.13`, so pip refuses it with *"Could not find a
> version that satisfies the requirement"* — which reads like the package is
> missing rather than like a version conflict. The Docker image pins
> `python:3.12-slim`; match it.

**Tesseract on Windows** is not added to `PATH` by its installer. Set
`TESSERACT_CMD` in `core-pipeline\.env` to the absolute path:

```ini
TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe
```

The same variable works everywhere — set it on macOS/Linux too if `tesseract`
is not on `PATH`.

### 2. Python packages

Two services, two virtualenvs. Use `py -3.12` on Windows so the launcher picks
3.12 rather than your newest install.

**macOS / Linux**

```bash
# core-pipeline
cd core-pipeline
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # ~400 MB
deactivate

# review-ui backend — separate service, separate venv
cd ../review-ui/backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
deactivate

# test suite — from the repo root
cd ../..
python3.12 -m venv .venv-test && source .venv-test/bin/activate
pip install -r tests/requirements.txt
python -m pytest tests/ -q               # 172 tests, no database needed
```

**Windows (PowerShell)**

```powershell
# core-pipeline
cd core-pipeline
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -V                                # confirm 3.12.x before installing
pip install -r requirements.txt          # ~400 MB
deactivate

# review-ui backend — separate service, separate venv
cd ..\review-ui\backend
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
deactivate

# test suite — from the repo root
cd ..\..
py -3.12 -m venv .venv-test
.venv-test\Scripts\Activate.ps1
pip install -r tests/requirements.txt
python -m pytest tests/ -q               # 172 tests, no database needed
```

If `Activate.ps1` fails with *"running scripts is disabled on this system"*,
allow local scripts once per user:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

In **cmd.exe** the activate command is `.venv\Scripts\activate.bat`; in **Git
Bash** it is `source .venv/Scripts/activate` (note `Scripts`, not `bin`).

### 3. The NER layer (GLiNER) — optional, and it gates rejection

This is the one dependency that changes what the pipeline can *conclude*.
Member verification's `wrong_member_on_page` check reads names off a page using
GLiNER. Without it, verification still runs — the rule pass does the extraction
— but **no page can be marked `wrong_member`, so no document can ever be
Rejected**. Every chart comes back Accepted or `needs_review`.

It is not installed by default because the runtime is ~2.5 GB and the
checkpoints another ~2 GB. Turning it on is three steps plus a flag:

**macOS / Linux**

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

**Windows (PowerShell)**

```powershell
cd core-pipeline
.venv\Scripts\Activate.ps1

pip install -r requirements-ner.txt
python -m stages.lib.member.extractors.ner_based.model_downloader
python -m stages.lib.member.extractors.ner_based.model_downloader --check

$env:MEMBER_NER_ENABLED = "true"   # session only — set it in core-pipeline\.env to persist
```

> Setting an environment variable in a shell lasts only for that shell. Put
> `MEMBER_NER_ENABLED=true` in `core-pipeline/.env` so it survives a restart;
> the app loads that file on startup. In **cmd.exe** the session form is
> `set MEMBER_NER_ENABLED=true`.

Downloader flags: `--force` re-downloads checkpoints already on disk,
`--check` verifies without downloading. It exits non-zero and names the failed
model id if any checkpoint is incomplete.

Where the checkpoints land, and which models load:

| Variable | Default | Meaning |
|---|---|---|
| `MEMBER_NER_MODELS_PATH` | `core-pipeline/models/ner` | Directory holding the checkpoints |
| `MEMBER_NER_MODEL_ID` | `gliner_medium` | **The only model knob.** One of `gliner_large`, `gliner_medium`, `gliner_low`. Readiness and the download both follow it. |
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
| Azure Blob intake | `AZURE_STORAGE_*` in `.env` | `run`/`batch`/`write` fail in blob mode; local paths still work |
| Final OCR 2 | `AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT` + `_KEY` | no final2 text; handwritten pages get no pass-2 verdict |
| DOS LLM pass | `AZURE_OPENAI_ENDPOINT` + a key **or** a managed identity, + `DOS_LLM_ENABLED=true` | DOS is regex-only, rows stamped `extraction_method='rules'` |

#### Azure OpenAI: key or no key

The DOS LLM pass authenticates one of two ways, chosen by `AZURE_OPENAI_AUTH`.
The endpoint is always required; only the credential differs.

| `AZURE_OPENAI_AUTH` | Uses | For |
|---|---|---|
| `key` | `AZURE_OPENAI_API_KEY` | a laptop with a key pasted into `.env` |
| `entra` | `DefaultAzureCredential` — no key at all | **an Azure VM with a managed identity**, or anywhere `az login` has run; the only option on a resource with `disableLocalAuth` |
| `entra_interactive` *(blob only)* | managed identity → `az` CLI → **browser prompt**, token cached | a developer machine with no managed identity and no `az` on PATH. `DefaultAzureCredential` excludes the browser, which is why plain `entra` fails there. Never on a headless server — the prompt hangs instead of failing |
| `auto` *(default)* | key if one is set, otherwise entra | leaving it alone keeps existing key setups working unchanged |

Keyless needs `azure-identity`, which is already in
`core-pipeline/requirements.txt`, and — the part that is easy to miss — an
identity holding **Cognitive Services OpenAI User** *on the OpenAI resource*.
Being in the subscription is not enough, and a missing role assignment comes
back as a 401 that reads exactly like a wrong key.

So on a VM with a managed identity, the whole configuration is:

```bash
AZURE_OPENAI_ENDPOINT=https://YOUR-RESOURCE.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT=gpt-4o-mini
DOS_LLM_ENABLED=true
# AZURE_OPENAI_AUTH defaults to auto; with no key set, that resolves to entra
```

The stage logs which one it resolved to — `DOS: LLM pass enabled
(deployment=..., auth=entra)` — so a VM that quietly fell back to a stale key
is visible in the run log rather than inferred.

#### Checking it against the live service

To test the endpoint before running a chart, fill in the constants at the top
of `check_azure_openai.py` and run it:

```bash
python check_azure_openai.py      # one throwaway prompt; exits 1 naming what to fix
```

It reads nothing but itself — no `.env`, no environment, no pipeline imports —
so it isolates the endpoint from the rest of the configuration. It has the same
`AUTH` choice: paste a key, or set `AUTH = "entra"` to use the VM's identity
(`pip install -r requirements.txt` at the repo root brings `azure-identity` for
that). A wrong key or an unassigned role is a 401, a deployment name that does
not exist on the resource is a 404, and a wrong host is a connection error; all
three reach the stage only as "regex only". When the token itself cannot be
acquired it says so before making any call, and lists the credentials it tried.

`tests/test_azure_openai.py` runs the real DOS prompt under pytest and skips
itself when no credentials are usable — including the keyless case, so a VM
with a managed identity exercises those tests rather than skipping them.

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
| Python 3.12 needed | yes, on the host | no, it is in the image |
| tesseract needed | yes, on the host | no, it is in the image |

**The ports differ between modes.** That is the single most common source of
confusion: `localhost:3000` is nothing in Mode A, and `localhost:8002` is
nothing in Mode B.

Both modes need the schema applied first, and both run the two services
independently — neither calls the other.

---

## Mode A — Local (macOS / Windows / Linux)

Four terminals: nothing daemonises, so each process holds its own.

Prerequisites: the code (via `git clone`, or
[Installing from a ZIP](#installing-from-a-zip) if cloning is blocked on your
machine), Python 3.12, tesseract, and the two venvs from
[Installing dependencies](#installing-dependencies).

### 1. Schema — once

**macOS / Linux**

```bash
psql "$DATABASE_URL" -f schema/v1.sql
psql "$DATABASE_URL" -f schema/v2.sql   # optional
```

**Windows (PowerShell)**

```powershell
psql $env:DATABASE_URL -f schema/v1.sql
psql $env:DATABASE_URL -f schema/v2.sql   # optional
```

Skip this entirely if you only want the review UI in Local Mode — it reads
files, not Postgres.

### 2. core-pipeline — :8001

**macOS / Linux**

```bash
cd core-pipeline
cp .env.example .env          # DATABASE_URL + any Azure credentials
source .venv/bin/activate
python cli.py serve           # see the note below before reaching for uvicorn
```

**Windows (PowerShell)**

```powershell
cd core-pipeline
Copy-Item .env.example .env   # DATABASE_URL + any Azure credentials
.venv\Scripts\Activate.ps1
python cli.py serve           # see the note below before reaching for uvicorn
```

> **`ModuleNotFoundError: No module named 'api'`** means you ran `uvicorn`
> from the wrong directory. `api/` lives under `core-pipeline/`, and uvicorn
> puts the **current directory** on `sys.path` — so `uvicorn api.main:app`
> works from `core-pipeline/` and fails from the repository root.
> `python cli.py serve` works from anywhere, because `cli.py` puts its own
> directory on the path itself. If you want uvicorn's `--reload`, run
> `uvicorn api.main:app --port 8001 --reload` **from `core-pipeline/`**.
> The Dockerfile does the same thing with `WORKDIR /app`.

<http://localhost:8001/docs>. Needs `tesseract` on PATH or `TESSERACT_CMD` set
in `.env` — mandatory on Windows.

### 3. review-ui backend — :8002

**macOS / Linux**

```bash
cd review-ui/backend
cp ../.env.example ../.env    # DATA_MODE=local needs no database
source .venv/bin/activate
uvicorn app.main:app --host 127.0.0.1 --port 8002 --reload
```

**Windows (PowerShell)**

```powershell
cd review-ui\backend
Copy-Item ..\.env.example ..\.env
.venv\Scripts\Activate.ps1
uvicorn app.main:app --host 127.0.0.1 --port 8002 --reload
```

<http://localhost:8002/docs>.

### 4. review-ui frontend — :5174

```bash
cd review-ui/frontend
npm install
npm run dev
```

<http://localhost:5174>. Vite proxies `/api` to **8002**, which is why the
backend must be on that port in this mode.

### Stopping

Ctrl-C in each terminal. Nothing is left running.

---

## Mode B — VM (Linux with Docker)

Two compose projects, deployed independently. Each brings up its own service and
neither depends on the other being present.

### 1. Schema — once, from anywhere that can reach the database

```bash
psql "$DATABASE_URL" -f schema/v1.sql
psql "$DATABASE_URL" -f schema/v2.sql   # optional
psql "$DATABASE_URL" -c "SELECT count(*) FROM pipeline_stage;"   # expect 8 (12 with v2)
```

### 2. core-pipeline — :8001

```bash
cd core-pipeline
cp .env.example .env          # DATABASE_URL + Azure credentials
docker compose up -d --build
docker compose logs -f api
curl -fsS localhost:8001/ready
```

Mounts:

| Host | Container | Mode | Why |
|---|---|---|---|
| `DATA_HOST_PATH` → `../review-ui/data/folders` | `/data/folders` | rw | the chart workspace this service writes |
| `METADATA_HOST_PATH` | `/data/metadata` | rw | mirrored manifest CSVs |
| `NER_MODELS_HOST_PATH` → `./models/ner` | `/app/models/ner` | ro | GLiNER checkpoints, if enabled |

> `DATA_HOST_PATH` must resolve to the **same storage** review-ui mounts. On one
> VM a relative path is enough; across hosts use a shared volume or an NFS mount.
> If they diverge, the pipeline writes charts the UI never sees, with no error
> on either side.

### 3. review-ui — :3000 API, :3001 web

```bash
cd ../review-ui
cp .env.example .env
docker compose up -d --build
```

<http://localhost:3001>. It mounts the chart workspace **read-only**.

### Updating a deployed VM

```bash
git pull
cd core-pipeline && docker compose up -d --build     # rebuild, recreate
cd ../review-ui  && docker compose up -d --build
```

An image rebuild is required for any code change — there is no reload in this
mode. Stop with `docker compose down` in either directory; they stop
independently.

### Behind a reverse proxy

Both services bind all interfaces inside their containers and publish to the
host. Neither terminates TLS and **neither has authentication** — put them
behind nginx/Caddy with auth before exposing either port beyond the VM. See
[ARCHITECTURE.md § Known limits](ARCHITECTURE.md#6-known-limits).

---

## Both modes: review-ui data source

`DATA_MODE` decides where the UI reads results from. It is independent of
whether you are in Mode A or Mode B.

| `DATA_MODE` | OCR + imaging read from | Page images | Database needed |
|---|---|---|---|
| `local` | `data/folders` — `ocr/*.txt`, `imaging/*.csv` | `pages/` | no |
| `production` (alias `postgres`) | Postgres v8 tables | `pages/` | yes |

The mode shows as a pill in the top bar and is returned by `GET /api/config`.

> The review UI is **read-only**. Every route is a `GET`; it records no review
> decisions. `manual_review` and `rejection_results` exist in the schema for a
> later phase and nothing writes them today.

`LOCAL_CACHE_TTL_SECONDS` (default 5) is how long a scan of `data/folders` is
trusted before being rechecked against file mtimes — relevant because
core-pipeline writes that directory while the UI is serving.

---

## Startup banner

Every start logs what it resolved, so "am I even pointed at the right database?"
is answerable without reading config. The password is masked.

```
INFO core-pipeline starting
INFO   database    : postgresql://postgres:***@localhost:5432/imaging_outputs
INFO   data root   : .../review-ui/data/folders
INFO   metadata    : .../review-ui/data/metadata
INFO   workers     : 4
INFO   schema      : OK, 8 stage(s) registered
```

If the database is unreachable it says so and starts anyway — `/docs` and
`/health` exist precisely to work when it does not:

```
WARNING   database    : UNREACHABLE — connection refused ...
WARNING   Mutating endpoints will return 503 until this is fixed. DATABASE_URL
          is read once at startup, so restart after editing .env.
```

---

## Health checks

Same in both modes, on :8001.

```bash
curl localhost:8001/health   # liveness + which optional features are on
curl localhost:8001/ready    # 503 unless the database is reachable and seeded
```

On Windows PowerShell use `curl.exe`, not `curl`.

```json
{
  "status": "ok",
  "member_ner": {
    "enabled": false,
    "models_path": ".../core-pipeline/models/ner",
    "enabled_models": [],
    "deps_installed": false,
    "deps_detail": "gliner not installed (…); pip install -r requirements-ner.txt",
    "model_id": "gliner_medium",
    "weights_present": [],
    "weights_missing": [],
    "ready": false,
    "reason": "MEMBER_NER_ENABLED=false"
  },
  "dos_llm_enabled": false,
  "stage_workers": 4
}
```

`reason` reports the *first* thing that stops NER working, in that order:
switched off beats missing runtime beats missing checkpoints — so it never
tells you to install 2.5 GB you have deliberately disabled. `model_id` is the
single checkpoint this run needs; `weights_missing` lists it if absent.

`member_ner.ready: false` means member verification runs rules-only — no page
can be marked `wrong_member`, so no document can be Rejected. `reason` names the
one precondition to fix; see
[Installing dependencies](#3-the-ner-layer-gliner--optional-and-it-gates-rejection).

---

## Windows notes

Everything runs on Windows — the Python is portable (all file IO declares
`encoding="utf-8"`, CSV writers set `newline=""`, no POSIX-only modules, no
shell-outs). What differs is the shell, not the code.

### Command equivalents

| Task | macOS / Linux | Windows PowerShell | cmd.exe |
|---|---|---|---|
| Create a venv | `python3.12 -m venv .venv` | `py -3.12 -m venv .venv` | same |
| Activate it | `source .venv/bin/activate` | `.venv\Scripts\Activate.ps1` | `.venv\Scripts\activate.bat` |
| Deactivate | `deactivate` | `deactivate` | `deactivate` |
| Copy a file | `cp a b` | `Copy-Item a b` | `copy a b` |
| Delete a tree | `rm -rf .venv` | `Remove-Item -Recurse -Force .venv` | `rmdir /s /q .venv` |
| Empty a file | `: > file` | `Clear-Content file` | `type nul > file` |
| Show a file | `cat file` | `Get-Content file` | `type file` |
| Env var (session) | `export X=1` | `$env:X = "1"` | `set X=1` |
| Use an env var | `"$X"` | `$env:X` | `%X%` |
| Open a URL | `open URL` | `start URL` | `start URL` |
| HTTP request | `curl -fsS URL` | `curl.exe -fsS URL` | `curl URL` |

In **Git Bash** the macOS/Linux column works as-is, with one exception:
activation is `source .venv/Scripts/activate` — `Scripts`, not `bin`.

### The four things that actually bite

1. **Python 3.13+ fails.** `rapidocr-onnxruntime` requires `<3.13`. Install
   3.12 and create the venv with `py -3.12`, because plain `py` or `python`
   selects your newest interpreter.
2. **`Activate.ps1` is blocked by default.** Run
   `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser` once.
3. **Tesseract is not on PATH.** Its installer does not add it. Set
   `TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe` in
   `core-pipeline\.env`.
4. **`curl` is not curl.** PowerShell aliases it to `Invoke-WebRequest`. Use
   `curl.exe`.

### Calling the API from Windows

The `curl` examples in this document are written for bash. Pasted into cmd.exe
or PowerShell they fail in three separate ways at once, and the errors do not
name the cause:

| bash | cmd.exe | PowerShell |
|---|---|---|
| `\` at end of line | `^` | `` ` `` (backtick) |
| `'{"a":"b"}'` | `"{\"a\":\"b\"}"` | `'{"a":"b"}'` works |
| `curl` | `curl` (real curl) | **`curl.exe`** — bare `curl` is `Invoke-WebRequest` |

A fourth trap is JSON-specific: **a Windows path cannot be pasted raw into
JSON.** `C:\Projects\x.csv` makes `\P` an invalid escape and the body is
rejected before it reaches the endpoint. Either double every backslash, or —
simpler — use forward slashes, which Windows accepts everywhere:

```text
"C:/Projects/Imaging Pipeline/review-ui/data/metadata/metadata_R1_B1.csv"
```

**cmd.exe** — one line, double quotes outside, escaped quotes inside:

```bat
curl -X POST http://localhost:8001/api/manifest/sweep -H "Content-Type: application/json" -d "{\"local_path\":\"C:/Projects/Imaging Pipeline/review-ui/data/metadata/metadata_R1_B1.csv\"}"
```

**PowerShell** — `Invoke-RestMethod` avoids the quoting entirely and pretty-prints
the reply:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8001/api/manifest/sweep `
  -ContentType 'application/json' `
  -Body '{"local_path":"C:/Projects/Imaging Pipeline/review-ui/data/metadata/metadata_R1_B1.csv"}'
```

**Easier than either** — skip HTTP. The CLI takes the path as a normal argument,
so the shell quotes it and no JSON is involved:

```powershell
cd core-pipeline
python cli.py manifest --local "C:\Projects\Imaging Pipeline\review-ui\data\metadata\metadata_R1_B1.csv"
```

Or open <http://localhost:8001/docs>, pick the endpoint, **Try it out**, and
edit the JSON in the browser — no shell quoting at all.

### Getting the code

Two routes. Use the first if your machine permits it.

**With git** — preferred, because every object is checksummed and `git status`
tells you instantly whether anything drifted:

```powershell
git clone https://github.com/abhinavdg-ever/pipeline-demo.git
cd pipeline-demo
```

**Without git** — many locked-down corporate machines block `git clone`. Download
the ZIP instead; see [Installing from a ZIP](#installing-from-a-zip) below for
the full procedure, including how to verify the extraction and how to update
later without losing your `.env` files.

Prefer a path **without spaces** (`C:\Projects\pipeline-demo`). Paths with
spaces work, but every unquoted command you paste from elsewhere will break.

### Installing from a ZIP

No `git` needed. The trade-off is that you lose the integrity check and the
update path, so both are handled manually below.

**1. Download**

Browser: <https://github.com/abhinavdg-ever/pipeline-demo> → **Code** →
**Download ZIP**. Or from PowerShell:

```powershell
curl.exe -L -o pipeline-demo.zip https://github.com/abhinavdg-ever/pipeline-demo/archive/refs/heads/main.zip
```

**2. Unblock before extracting**

Windows tags anything downloaded from the internet with the Mark of the Web, and
that tag propagates to every extracted file. Clear it on the ZIP first, so it is
one operation rather than hundreds:

```powershell
Unblock-File .\pipeline-demo.zip
```

(Equivalently: right-click the ZIP → Properties → tick **Unblock** → OK.)

**3. Extract**

```powershell
Expand-Archive .\pipeline-demo.zip -DestinationPath C:\Projects
cd C:\Projects\pipeline-demo-main
```

GitHub names the folder `<repo>-<branch>`, so you get **`pipeline-demo-main`**,
not `pipeline-demo`. Rename it if you prefer.

**4. Verify the extraction — do not skip this**

Without git there is nothing checksumming the transfer, and a partial or
corrupted extraction produces `ImportError`s that read like code bugs. The test
suite is the check: it imports every module and parses every source file, so it
fails loudly if anything arrived damaged.

```powershell
py -3.12 -m venv .venv-test
.venv-test\Scripts\Activate.ps1
pip install -r tests/requirements.txt
python -m pytest tests/ -q
```

**172 passed** means the extraction is sound. Anything else — especially
`ModuleNotFoundError` or `SyntaxError` — means re-download rather than debug.

**5. Updating later**

There is no `git pull`. Re-download and re-extract to a *new* folder, then carry
your configuration across — a fresh ZIP does not contain your `.env` files,
because they are gitignored and were never in the repo:

```powershell
# keep these from the old folder
copy C:\Projects\pipeline-demo-old\core-pipeline\.env      C:\Projects\pipeline-demo-main\core-pipeline\
copy C:\Projects\pipeline-demo-old\review-ui\.env          C:\Projects\pipeline-demo-main\review-ui\
copy C:\Projects\pipeline-demo-old\review-ui\frontend\.env C:\Projects\pipeline-demo-main\review-ui\frontend\
```

Also carry over anything large you do not want to fetch again:
`core-pipeline\models\ner\` (the ~2 GB of GLiNER checkpoints) and
`review-ui\data\folders\` (downloaded charts). Neither is in the ZIP.

Extract to a new folder rather than over the old one — extracting on top leaves
deleted files behind, which is how a tree ends up with a stale module that
shadows a current one.

### Line endings

`.gitattributes` normalises the repo to LF and checks out LF on every platform.
Shell scripts, Dockerfiles and YAML are pinned to LF because CRLF breaks a
shebang inside a Linux container; `.bat` and `.cmd` are pinned to CRLF. You do
not need to set `core.autocrlf` — leave it alone and let `.gitattributes` win.

---

## core-pipeline API reference

Base: `http://localhost:8001` · OpenAPI: `/openapi.json` · Swagger: `/docs`

Mutating endpoints are **asynchronous**: they return `202 Accepted` immediately
and work continues in the background. Poll the chart endpoint for progress.

### Signing in to the review UI

`imaging-user` / `aipocpw2026`. Shared with everyone using the POC, inlined
into the JS bundle, and committed to this repository — it keeps a casual
visitor off the page and is **not** a security control. The backend has no auth
on any route, so everything behind the screen is reachable without it.

Override per deployment with `VITE_LOGIN_USERNAME` / `VITE_LOGIN_PASSWORD` in
`review-ui/.env`. Vite inlines these at **build** time, so they only take effect
on a rebuild:

```bash
cd review-ui && docker compose up -d --build frontend   # --build is the point
```

`docker compose up -d` on its own keeps serving the previously built bundle
with the old pair.

### `GET /health`

Liveness, plus **every optional feature and the one precondition each is
missing**. Each of these degrades a stage rather than failing it, so without
this the first sign of a misconfiguration is a chart that completed with less in
it than expected.

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

Every capability that is not `ready` carries a `reason` naming the single thing
to fix. What each one costs when off:

| Not ready | Consequence |
|---|---|
| `blob` | `run`/`batch`/`write` work from local paths; blob mode fails |
| `azure_document_intelligence` | final2 produces no text; handwritten pages get no pass-2 verdict |
| `dos_llm` | DOS is regex-only, rows stamped `extraction_method='rules'` |
| `member_ner` | no page can be `wrong_member`, so **no document can be Rejected** |

**This endpoint opens no sockets.** It reports configuration — environment and
installed packages — so it stays fast and cannot hang when Azure is down.
Credentials being present is not proof the role is assigned; for blob, the
**startup log** does one bounded round trip and reports that separately.

The same values are logged once at startup, from the same function, so the
banner and the endpoint cannot disagree:

```
  database    : postgresql://user@localhost:5432/imaging_outputs
  workers     : 4
  schema      : OK, 8 stage(s) registered
  blob        : OK — entra, account=acct, container=imaging-pipeline
  final2 OCR  : off — not set: AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT, ..._KEY
  DOS LLM     : OK — deployment=gpt-4o, auth=entra
  member NER  : off — MEMBER_NER_ENABLED=false
```

A blob line reading `configured but UNREACHABLE` means the credentials resolved
and the call was refused — usually an Entra identity without **Storage Blob
Data Reader** on the account, which is a 403 that reads like a missing
container.

### `GET /ready`
503 unless the database is reachable **and** `pipeline_stage` is seeded. Use as
the container readiness probe.

### `GET /api/stages`
The pipeline's shape, straight from the `pipeline_stage` table.

```bash
curl -s localhost:8001/api/stages | jq '.stages[] | {seq, stage_name, pass_no, is_phase1}'
```

### `POST /api/charts/run` → 202

Read one chart, run the chain, and optionally write the results — one call.

A source is a **read path plus a folder name**, which resolve together, and the
**folder name is the chart name**:

```json
{
  "blob_container": "imaging-pipeline",
  "blob_read_path": "Raw_Input/Run1/Batch1/DEID_PNGs",
  "blob_read_folder_name": "52754737_48221214",
  "blob_write_path": "Processed/Run1"
}
```
```json
{
  "local_read_path": "/data/inbox",
  "local_folder_name": "52754737_48221214",
  "local_write_path": "/data/outbox"
}
```

```
reads   Raw_Input/Run1/Batch1/DEID_PNGs/52754737_48221214/
writes  Processed/Run1/52754737_48221214/
```

The write path resolves the same way, with the same folder name, so a chart
keeps its identity on both sides and two charts written to one destination
cannot merge.

| Field | Mode | Meaning |
|---|---|---|
| `blob_container` | blob | Container, for **both** read and write |
| `blob_read_path` + `blob_read_folder_name` | blob | Prefix, and the chart folder under it. Both required. |
| `blob_write_path` | blob | Prefix to write to. Omit to run without writing. |
| `local_read_path` + `local_folder_name` | local | Directory, and the chart folder under it. Both required. |
| `local_write_path` | local | Directory to write to. Omit to run without writing. |
| `write_mode` | both | `skip_orig_pages` (default) or `all_files` — see below |
| `overwrite` | both | Replace files already at the destination |
| `through`, `only` | both | Run part of the chain |
| `force` | both | Reprocess completed pages. **Stage 5 is billed per page.** |

**The folder name is given, never inferred.** It becomes `chart_list.chart_name`,
prefixes every output CSV, and is the key the member manifest joins on
(`record_id`) — too load-bearing to guess from a path.

**Read and write stay on one backend.** Blob in, blob out; local in, local out.
Mixing them is a `400`, because a write landing somewhere the caller did not
mean is worse than an error.

| Body | Result |
|---|---|
| both a blob and a local source | `400` |
| neither | `400` |
| read path without folder name, or vice versa | `400`, naming the missing field |
| blob source with `local_write_path` (or the reverse) | `400` |
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
`POST /api/charts/write` can send it later.

**A write failure does not fail the run.** The chart is in the workspace either
way; the log says so, and `/api/charts/write` retries without reprocessing.

#### `write_mode`

| | Sends | Use when |
|---|---|---|
| `skip_orig_pages` *(default)* | `corrected-pages/`, `ocr/`, `imaging/` | The originals came **from** the destination you are writing back to. Re-sending them doubles storage and transfer for bytes already there. |
| `all_files` | those **plus** `pages/` | The destination is a handoff that must stand alone. |

Corrected pages are sent in **both** modes: the pipeline produced those and the
source does not have them. Measured on a 3-page chart: 3 files / 3 KB by
default against 6 files / 885 KB with `all_files`.

If a chart has produced no output yet, the default writes nothing and says so,
naming `all_files` as the way to send the originals anyway.

#### Running part of the chain

`through` and `only` answer different questions, and both are accepted by
`/run`, `/batch` and `/{chart_id}/rerun` with the same spelling.

| | Means | Use when |
|---|---|---|
| `through` | Run from the top, **stop after** this stage | You want the first N stages and nothing paid for beyond them |
| `only` | Run **just** these stages, whatever ran before | The earlier output on disk is good and one step changed |

A stage is named `ocr_final2` for pass 1, or `blank_junk:2` for pass 2. A bare
name always means pass 1 — never "whichever pass exists" — so `--only
blank_junk` cannot mean different things on different days. An unknown name is
a `400` naming the known stages, rather than a `202` and a run that silently
does nothing.

```bash
# Everything up to and including Final OCR 2, then stop
curl -X POST localhost:8001/api/charts/run -H 'Content-Type: application/json' \
  -d '{"local_read_path":"/data/inbox","local_folder_name":"chart_x","through":"ocr_final2"}'

# Just the DOS stage, on a chart whose OCR is already done
curl -X POST localhost:8001/api/charts/3/rerun -H 'Content-Type: application/json' \
  -d '{"only":["dos_extract"]}'
```

`only` runs a stage against whatever its inputs are on disk. That makes it the
right tool for re-running the last step after a fix, and the wrong tool on a
chart that has never run — the stage will find nothing to read.

### `POST /api/charts/write` → 202

Write an already-run chart out, **without reprocessing it**. Use it to send a
chart to a second destination, or to export one that ran before a write path
was given.

```bash
curl -X POST localhost:8001/api/charts/write -H 'Content-Type: application/json' \
  -d '{"chart_name":"52754737_48221214","blob_container":"imaging-pipeline","blob_write_path":"Processed/Run1"}'
```

| Field | Default | Meaning |
|---|---|---|
| `chart_name` | — | Folder under `data/folders`. Required — this is the chart. |
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

`write` **copies** — the workspace is left intact, so review-ui keeps serving
the chart and you can write it again elsewhere. macOS AppleDouble stubs
(`._1.jpg`) are never written out.

### `POST /api/charts/batch` → 202

Run **every chart under a read path**, sequentially, and optionally write each.

The same fields as `/run` **minus the folder name** — here every sub-folder
holding images is one chart and names itself:

```json
{
  "local_read_path": "/data/inbox/2026-09-13",
  "local_write_path": "/data/outbox",
  "limit": 1
}
```
```json
{
  "blob_container": "imaging-pipeline",
  "blob_read_path": "Raw_Input/Run1/Batch1/DEID_PNGs",
  "blob_write_path": "Processed/Run1"
}
```

```
reads   <read_path>/52754737_48221214/   ->  writes  <write_path>/52754737_48221214/
        <read_path>/52755507_45500395/   ->          <write_path>/52755507_45500395/
```

**What counts as a chart:** each immediate sub-folder holding at least one
image. Folders with no images are skipped rather than attempted, dotfolders are
ignored, and macOS `._` stubs do not make a folder count. Point it at a single
chart folder and it runs just that one, so the same command works for a drop of
fifty or a drop of one.

| Field | Meaning |
|---|---|
| `blob_container` + `blob_read_path` | Container, and the prefix whose sub-folders are charts |
| `local_read_path` | Parent directory whose sub-folders are charts |
| `blob_write_path` / `local_write_path` | Where each chart is written, under its own folder name. Omit to run without writing. |
| `write_mode`, `overwrite` | As `/run` |
| `through`, `only`, `force` | As `/run` |
| `limit` | Only the first N charts. **Use `limit: 1` for a dry run.** |

Read and write stay on the same backend, exactly as in `/run`.

**Batch is `/run`, once per folder.** Each chart goes through the same call, so
an option means the same thing in both places and a batch of one is
indistinguishable from a single run.

For a local read path the chart list is resolved **before** returning, so a
wrong path gives `400` immediately rather than `202` and an empty batch an hour
later. `charts_found` tells you how many will run.

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

**Charts run one at a time, deliberately.** Each already fans out across pages
(`STAGE_WORKERS`), and stage 5 is billed per page — overlapping charts
multiplies memory and spend without finishing the batch sooner. See
[PLAN.md](../PLAN.md#proposed-shard-a-batch-across-n-chart-workers) for the
design that would change that, and why it is not built.

**One bad folder does not stop the batch, and neither does one unwritable
destination.** A chart that fails is recorded and the run continues; a chart
that ran but could not be written is marked `write.status = "failed"` while
staying `status = "completed"`, because the pipeline did its job and
`/api/charts/write` can retry without reprocessing.

A batch can run for hours, so the reply is `202` and progress goes to the log:

```
INFO Batch: 37 chart folder(s) under /data/inbox/2026-09-13
INFO [1/37] 52743839_44976074
INFO Background batch finished: ... -> 36/37 completed, 1 failed in 4213.8s
WARNING   failed: 52744171_44423942 — RuntimeError: No images in ...
```

The CLI equivalent runs inline and prints a per-chart JSON summary, each entry
carrying its own `write` result:

```bash
python cli.py batch --local-read-path ./drops --local-write-path ./out --limit 2
```

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
  "only": ["member_verify"],         // optional; "name" = pass 1, "name:2" = pass 2
  "through": null                    // optional; run from the top, stop after this stage
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

# run: one chart, from blob or a local folder. Copies the images in, renames
# them 1.jpg/2.jpg…, loads any manifest beside them, registers, runs the chain.
python cli.py run --blob-container imaging-pipeline \
                  --blob-path run1/batch1/52743839_44976074 \
                  --run-id R1 --batch-id B1
python cli.py run --local "C:\drops\52743839_44976074"      # same command, local source
python cli.py run --local ./drop --chart-name 52743839_44976074
python cli.py run --local ./drop --force                     # replace an existing chart
python cli.py run --local ./drop --no-pipeline               # intake only

# Stop after a stage, or run one stage on its own.
python cli.py run --local ./drop --through ocr_final2
python cli.py run --local ./drop --through ocr_prelim        # cheapest intake check

# Batch: scan a parent folder (or blob prefix) and run EVERY chart in it,
# one at a time. One bad folder does not stop the rest. Same flags as run.
python cli.py batch --local-read-path "D:\drops\2026-09-13"
python cli.py batch --local-read-path ./drops --local-write-path ./out
python cli.py batch --local-read-path ./drops --limit 2 --no-pipeline   # dry run first
python cli.py batch --local-read-path ./drops --through ocr_prelim      # no paid OCR
python cli.py batch --blob-container imaging-pipeline \
                    --blob-read-path Raw_Input/Run1/Batch1 \
                    --blob-write-path Processed/Run1

# write: the reverse of run's intake step — the whole chart folder back out.
python cli.py write 52743839_44976074 --local /data/outbox
python cli.py write 52743839_44976074 --blob-container imaging-pipeline \
                                      --blob-path run1/out --overwrite

python cli.py rerun 7                        # resume
python cli.py rerun 7 --force                # reprocess everything
python cli.py rerun 7 --only member_verify   # one stage
python cli.py rerun 7 --only blank_junk:2    # a specific pass
python cli.py rerun 7 --through ocr_final1   # from the top, stop after Final OCR 1

# Manifests — a local file, a whole local directory, or a blob prefix.
# All three upsert: re-running with a corrected CSV updates in place.
python cli.py manifest --local ../review-ui/data/metadata/metadata_R1_B1.csv
python cli.py manifest --local ../review-ui/data/metadata/   # whole directory
python cli.py manifest --local "/Users/me/Desktop/manifests"  # any path
python cli.py manifest --blob-container imaging-pipeline --blob-prefix manifests/run1
python cli.py manifest --local ./m.csv --run-id R1 --batch-id B1   # override parsed ids
```

---

## How to run a chart

Three verbs cover everything: **`run`** one chart, **`batch`** a folder of them,
**`write`** the results back out. Every one of them is also a CLI subcommand
with the same flags, so you can work without the HTTP hop.

### The short version

```bash
# One chart: read it, run it, write the results — one call
curl -X POST localhost:8001/api/charts/run -H 'Content-Type: application/json' \
  -d '{"local_read_path":"/data/inbox",
       "local_folder_name":"52743839_44976074",
       "local_write_path":"/data/outbox"}'

# Watch it
curl -s localhost:8001/api/charts/by-name/52743839_44976074 | jq '.chart.status'
```

Reads `/data/inbox/52743839_44976074/`, writes `/data/outbox/52743839_44976074/`.
Drop `local_write_path` to run without writing.

That is the whole happy path. Everything below is the detail behind it.

### The folder name is the chart name

A source is a read path **plus** a folder name, and they resolve together:

| You send | Reads | Chart name |
|---|---|---|
| `local_read_path: /data/inbox`, `local_folder_name: 52743839_44976074` | `/data/inbox/52743839_44976074/` | `52743839_44976074` |
| `blob_read_path: run1/batch1`, `blob_read_folder_name: 52743839_44976074` | `run1/batch1/52743839_44976074/` | `52743839_44976074` |

The write path resolves the same way with the same folder name, so
`/data/outbox/52743839_44976074/`.

It is **given, not inferred**: the chart name becomes `chart_list.chart_name`,
prefixes every output CSV, and is the key the member manifest joins on
(`record_id`). Guessing it from a path would name the chart by accident, and a
mismatch there is what makes a chart come back `needs_review` with
`decision_reason='manifest_missing'`.

The name is sanitised to `[A-Za-z0-9._-]`, so `My Chart 001` becomes
`My_Chart_001`. That matters more than it looks: the member stage joins a chart
to its roster on `record_id = chart_name`. If the manifest carries the
unsanitised string, nothing matches and the chart returns `needs_review` with
`decision_reason='manifest_missing'` and no obvious cause. The response echoes
the resolved name back — worth a glance on the first chart of a new drop.

### Where the pages can live

The source folder may hold the images directly or in a subfolder; subfolders are
always searched, so a chart keeping its scans in `pages/` needs no flag. The
source is always **copied**, never moved, so a failed run is a no-op rather than
data loss.

```
/data/inbox/52743839_44976074/1.jpg          ← works
/data/inbox/52743839_44976074/pages/1.jpg    ← also works
```

Under Docker, `local_path` must be a path **inside the container** — mount the
folder first; the host filesystem is not visible to the service.

### Running part of the chain

Both options work on `run`, `batch` and `rerun`, spelled identically.

```bash
# Everything up to Final OCR 2, then stop — nothing past it is paid for
curl -X POST localhost:8001/api/charts/run -H 'Content-Type: application/json' \
  -d '{"local_read_path":"/data/inbox","local_folder_name":"chart_x","through":"ocr_final2"}'

# Just the DOS stage, against OCR that is already on disk
curl -X POST localhost:8001/api/charts/3/rerun -H 'Content-Type: application/json' \
  -d '{"only":["dos_extract"]}'
```

`through` bounds the chain from the top; `only` picks stages out of it and runs
them against whatever is already there. `through: "ocr_prelim"` is the cheapest
way to confirm intake picked up the right pages before committing to stage 5.

### A whole drop at once

```bash
curl -X POST localhost:8001/api/charts/batch -H 'Content-Type: application/json' \
  -d '{"local_read_path":"/data/inbox/2026-09-13","local_write_path":"/data/outbox","limit":1}'
```

Each immediate subfolder holding at least one image is one chart. Charts run
**one at a time** — each already fans out across its pages, and stage 5 is
billed per page, so overlapping them multiplies memory and spend without
finishing sooner. A chart that fails is recorded and the batch continues.

**Use `limit: 1` first.** The reply tells you `charts_found` before anything
runs, so a wrong path is a `400` immediately rather than an empty batch an hour
later. Pair it with `"through": "ocr_prelim"` to dry-run a large drop for the
price of Tesseract.

Progress goes to the server log, not the response:

```
INFO Batch: 37 chart folder(s) under /data/inbox/2026-09-13
INFO [1/37] 52743839_44976074
INFO Background batch finished: ... -> 36/37 completed, 1 failed in 4213.8s
WARNING   failed: 52744171_44423942 — RuntimeError: No images in ...
```

### Writing the results back out

```bash
# To blob
curl -X POST localhost:8001/api/charts/write -H 'Content-Type: application/json' \
  -d '{"chart_name":"52743839_44976074","blob_container":"imaging-pipeline","blob_path":"run1/out"}'
```

`write` **copies** — the workspace under `data/folders` is left intact, so
review-ui keeps serving the chart and you can write the same chart to a second
destination without re-running anything. The chart's name is appended to the
destination, so two charts written to one place do not merge. A destination
that already holds files needs `overwrite: true`.

Note that nothing prunes `data/folders` after a write. On a long-running host it
grows monotonically; cleanup is a manual `rm` once you have confirmed the write
landed.

### The same thing from the CLI

```bash
cd core-pipeline
python cli.py run --local-read-path /data/inbox --folder-name 52743839_44976074
python cli.py run --local-read-path /data/inbox --folder-name 52743839_44976074 \
                  --local-write-path /data/outbox            # run AND write
python cli.py run --blob-container imaging-pipeline \
                  --blob-read-path Raw_Input/Run1/Batch1/DEID_PNGs \
                  --folder-name 52743839_44976074 \
                  --blob-write-path Processed/Run1
python cli.py run --local-read-path ./drops --folder-name chart_x --through ocr_final2
python cli.py batch --local-read-path ./drops --limit 2 --through ocr_prelim
python cli.py write 52743839_44976074 --local-write-path /data/outbox
python cli.py write 52743839_44976074 --local-write-path /data/outbox --all-files
python cli.py rerun 7 --only member_verify --force
```

The CLI runs **inline** and prints a JSON summary when the chain finishes; the
API returns `202` and works in the background. Same code underneath.

---

## Typical sessions

### First run against a new database

```bash
psql "$DATABASE_URL" -f schema/v1.sql   # required — what is implemented
psql "$DATABASE_URL" -f schema/v2.sql   # optional — next phase, nothing uses it yet

cd core-pipeline && cp .env.example .env && docker compose up -d --build
curl -s localhost:8001/ready            # {"status":"ready","stages":8}

# Manifest first, so member verification has something to verify against
curl -X POST localhost:8001/api/manifest/sweep -H 'Content-Type: application/json' \
  -d '{"local_path":"/data/metadata/metadata_R1_B1.csv"}'

# Then the chart
curl -X POST localhost:8001/api/charts/run -H 'Content-Type: application/json' \
  -d '{"blob_container":"imaging-pipeline","blob_path":"run1/batch1/52743839_44976074"}'

# Watch it
watch -n5 "curl -s localhost:8001/api/charts/by-name/52743839_44976074 \
  | jq '{status:.chart.status, stage:.chart.current_stage}'"

cd ../review-ui && cp .env.example .env && docker compose up -d --build
open http://localhost:3001
```

### Local Postgres, no Docker

A scratch database on the host, for development:

```bash
brew install postgresql@16 && brew services start postgresql@16   # macOS
createdb imaging_outputs
psql -d imaging_outputs -f schema/v1.sql

# DATABASE_URL in core-pipeline/.env — brew's role is your own username,
# not "postgres", so the packaged default will not connect as-is:
#   DATABASE_URL=postgresql://$(whoami)@localhost:5432/imaging_outputs

cd core-pipeline && source .venv/bin/activate && python cli.py serve
```

`/ready` returning `{"stages": 8}` means v1 applied. Twelve stages means you
also applied v2 — the four extra are registered but not orchestrated.

### A manifest arrived after the chart

Member verification will have recorded `decision_reason='manifest_missing'`.

```bash
curl -X POST localhost:8001/api/manifest/sweep -H 'Content-Type: application/json' \
  -d '{"local_path":"/data/metadata/metadata_R1_B1.csv"}'

curl -X POST localhost:8001/api/charts/7/rerun -H 'Content-Type: application/json' \
  -d '{"only":["member_verify"],"force":true}'
```

The sweep links the manifest to the chart automatically.

### A chart that died part-way

Re-runs **resume**: pages already completed are not redone, so a chart that
failed at DOS on page 400 of 500 finishes without repeating paid OCR.

```bash
curl -X POST localhost:8001/api/charts/7/rerun \
  -H 'Content-Type: application/json' -d '{}'
```

Reach for `force` only when you mean "reprocess everything" — it re-sends every
page to Azure Document Intelligence, which is billed per page.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Log flooded with `http_logging_policy: Request headers:` | Azure SDK logging at INFO | default is now `AZURE_LOG_LEVEL=WARNING`; if you see it, that variable is set to INFO/DEBUG somewhere |
| `ModuleNotFoundError: No module named 'api'` | `uvicorn` run outside `core-pipeline/` | `cd core-pipeline` first, or use `python cli.py serve`, which works from anywhere |
| `/ready` → 503 "pipeline_stage is empty" | schema not applied | run `schema/v1.sql` |
| Chart stuck at `ocr_prelim` | Tesseract missing | install it, or set `TESSERACT_CMD` |
| Rotated pages OCR as gibberish | Tesseract `osd` traineddata missing, so stage 1 cannot detect orientation | install the full Tesseract package; pages pass through unrotated until then |
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
