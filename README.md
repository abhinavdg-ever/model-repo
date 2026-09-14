# Advantmed Imaging Pipeline

Two independently deployable services over one Postgres database and one shared
chart workspace:

- **`core-pipeline/`** — chart intake and the eight-stage imaging chain (OCR,
  quality, blank/junk, member verification, date of service). Port 8001.
- **`review-ui/`** — a read-only viewer over what the pipeline produced.
  Ports 3000 (API) and 3001 (web).

They never call each other. Each has its own `docker-compose.yml`.

---

## Documentation

| Document | Covers |
|---|---|
| [`PLAN.md`](PLAN.md) | Living architecture plan — status, decisions, what changed |
| [`docs/FLOW.md`](docs/FLOW.md) | What runs when: end-to-end diagrams, skip rules, resume, status |
| [`docs/LOGIC.md`](docs/LOGIC.md) | How each decision is made and exactly what it writes to the database |
| [`docs/API.md`](docs/API.md) | Running both services, full API reference, CLI, troubleshooting |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | System shape, data model, role of every file, known limits |

---

## Quick start

Runs on macOS, Linux and Windows. **Python 3.12** — 3.13+ does not work
(`rapidocr-onnxruntime` requires `<3.13`).

Two ways to run it, and they use different ports:
**[Mode A — Local](docs/API.md#mode-a--local-macos--windows--linux)** (uvicorn,
for development) and **[Mode B — VM](docs/API.md#mode-b--vm-linux-with-docker)**
(docker compose, for deployment). The quick start below is Mode B.

No `git clone` on your machine? See
[Installing from a ZIP](docs/API.md#installing-from-a-zip).

**macOS / Linux**

```bash
# 1. Schema — once, before either service starts
psql "$DATABASE_URL" -f schema/v1.sql   # required — what is implemented
psql "$DATABASE_URL" -f schema/v2.sql   # optional — next phase, nothing uses it yet

# 2. core-pipeline
cd core-pipeline
cp .env.example .env          # fill in DATABASE_URL and any Azure credentials
docker compose up -d --build  # http://localhost:8001/docs

# 3. review-ui
cd ../review-ui
cp .env.example .env
docker compose up -d --build  # http://localhost:3001

# 4. Tests
python -m pytest tests/ -q    # 172 tests
```

**Windows (PowerShell)**

```powershell
# 1. Schema
psql $env:DATABASE_URL -f schema/v1.sql
psql $env:DATABASE_URL -f schema/v2.sql

# 2. core-pipeline
cd core-pipeline
Copy-Item .env.example .env
docker compose up -d --build  # http://localhost:8001/docs

# 3. review-ui
cd ..\review-ui
Copy-Item .env.example .env
docker compose up -d --build  # http://localhost:3001

# 4. Tests
python -m pytest tests/ -q    # 172 tests
```

Windows specifics — venv activation, `TESSERACT_CMD`, `curl.exe`, the
PowerShell execution policy: [`docs/API.md § Windows notes`](docs/API.md#windows-notes).

Run a chart — from blob, or from a folder on the server:

```bash
curl -X POST localhost:8001/api/charts/run -H 'Content-Type: application/json' \
  -d '{"blob_container":"imaging-pipeline",
       "blob_read_path":"run1/batch1",
       "blob_read_folder_name":"52743839_44976074",
       "blob_write_path":"Processed/Run1"}'

curl -X POST localhost:8001/api/charts/run -H 'Content-Type: application/json' \
  -d '{"local_read_path":"/data/inbox","local_folder_name":"52743839_44976074"}'
```

The chart names itself from the last path segment — `52743839_44976074` in both
cases — so there is nothing else to fill in. Poll
`GET /api/charts/by-name/52743839_44976074` for progress.

Three verbs cover the service:

| | |
|---|---|
| `POST /api/charts/run` | one chart in, from blob or local, then the 8 stages |
| `POST /api/charts/batch` | the same, once per subfolder of a drop, one at a time |
| `POST /api/charts/write` | the finished folder back out to blob or local |

Add `"through": "ocr_final2"` to stop after a stage, or `"only": ["dos_extract"]`
to run one on its own. Every verb is also a CLI subcommand with the same flags.

Full instructions — partial runs, batches, writing results back, running without
Docker: [`docs/API.md § How to run a chart`](docs/API.md#how-to-run-a-chart).

---

## The stage chain

| # | Stage | Runs on | Writes |
|---|---|---|---|
| 1 | Rotation + handwriting | every page | `ocr_quality_results` (+ `corrected-pages/` when enabled) |
| 2 | Preliminary OCR (Tesseract) | every page | `ocr_results` |
| 3 | Blank/junk/duplicate — pass 1 | printed only | `blank_junk_classification` |
| 4 | Final OCR 1 (RapidOCR) | survivors + handwritten | `ocr_results` |
| 5 | Final OCR 2 (Azure DocIntel) | survivors + handwritten | `ocr_results` |
| 6 | Blank/junk/duplicate — pass 2 | handwritten + survivors | final verdict |
| 7 | Member extraction + verification | not blank/junk | member rows + summary |
| 8 | Date of service | not blank/junk | DOS rows + dates |

Stage 5 is billed per page — which is why re-runs **resume** by default rather
than reprocessing. Stage 7 produces the accept/reject decision.

The manifest loader runs independently, before or after ingest.

---

## Shared workspace

`core-pipeline` writes it; `review-ui` mounts it read-only.

```text
review-ui/data/folders/<chart_name>/
  pages/1.jpg … N.jpg
  ocr/<chart>_prelim.txt | _final1.txt | _final2.json
  imaging/<chart>_rotation.csv | _hw_printed.csv | _junk.csv
          _member_extraction.csv | _member_verification.csv | _dos.csv
```

---

## Before production

Two limits worth knowing up front — both detailed in
[`docs/ARCHITECTURE.md § Known limits`](docs/ARCHITECTURE.md#6-known-limits):

- **There is no authentication** on either service. Charts carry member names
  and dates of birth.
- **Wrong-member rejection needs the GLiNER layer.** The code is ported and
  wired, but the runtime (`pip install -r core-pipeline/requirements-ner.txt`,
  ~2.5 GB) and the checkpoints (via the bundled downloader, ~2 GB) are not
  vendored, so it is **off by default**. While off, member verification runs
  rules-only and **no document can be Rejected**. `GET /health` reports exactly
  which precondition is unmet — see
  [`docs/LOGIC.md`](docs/LOGIC.md#turning-it-on).
