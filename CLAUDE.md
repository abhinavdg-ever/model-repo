# CLAUDE.md

Working notes for this repository. Read this before changing the schema, the
stage chain, or anything that writes to Postgres.

Longer-form docs: [`docs/FLOW.md`](docs/FLOW.md) (what runs when),
[`docs/LOGIC.md`](docs/LOGIC.md) (how each decision is made),
[`docs/API.md`](docs/API.md) (setup, running, endpoints),
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) (system shape),
[`PLAN.md`](PLAN.md) (living plan).

---

## 1. The repository in one screen

Two services over one Postgres database and one shared chart workspace. They
**never call each other**.

```
core-pipeline/     chart intake + the eight-stage imaging chain.  Port 8001.
                   WRITES review-ui/data/folders.
review-ui/         read-only viewer over what the pipeline produced.
                   Ports 3000 (API) / 3001 (web).  MOUNTS that folder read-only.
schema/v1.sql      the schema that is IMPLEMENTED. See §5.
schema/v2.sql      next phase. Defined, wired to nothing. Optional.
Reference/         the V1 prototypes. Source of truth for ported logic.
                   NEVER EDIT — port from it, diff against it.
tests/             111 tests, no database required.
```

The stage chain, in order (`pipeline_stage` table is the authority):

| # | Stage | Runs on | Writes |
|---|---|---|---|
| 1 | Preliminary OCR (Tesseract) | every page | `ocr_results` |
| 2 | Rotation + handwriting | every page | `ocr_quality_results` |
| 3 | Blank/junk/duplicate — pass 1 | printed only | `blank_junk_classification` |
| 4 | Final OCR 1 (RapidOCR) | survivors + handwritten | `ocr_results` |
| 5 | Final OCR 2 (Azure DocIntel) | survivors + handwritten | `ocr_results` |
| 6 | Blank/junk/duplicate — pass 2 | handwritten + survivors | final verdict |
| 7 | Member extraction + verification | not blank/junk | member rows + summary |
| 8 | Date of service | not blank/junk | DOS rows + dates |

Stage 5 is **billed per page**. Re-runs resume by default; `--force` reprocesses
and costs money.

---

## 2. What is SHA-256, and why this repo uses it

**SHA-256** is a cryptographic hash function. Give it any number of bytes — a
JPEG, a page of OCR text, an empty string — and it returns a fixed 256-bit
value, written as 64 hexadecimal characters:

```
$ printf 'hello' | shasum -a 256
2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824
```

Four properties are what make it useful, and only the first three matter here:

1. **Deterministic.** The same bytes always produce the same digest, on any
   machine, in any language, forever.
2. **Fixed size.** A 40 MB TIFF and a one-character string both hash to 64 hex
   characters. Cheap to store, cheap to index, cheap to compare.
3. **Avalanche.** Change one pixel, one space, one character, and roughly half
   the output bits flip. There is no "close" — digests are equal or they are
   not.
4. **One-way / collision-resistant.** You cannot work backwards from a digest to
   the content, and nobody has ever produced two different inputs with the same
   SHA-256 digest. This is what makes it *cryptographic*, and this repo does not
   depend on it — we use SHA-256 as a very good content fingerprint, not as a
   security control.

Practically: **a digest is a short, comparable name for a blob of content.**
Instead of asking "are these two 40 MB images the same?" (read both, compare
byte by byte) you ask "are these two 64-character strings equal?" — an indexed
integer-speed comparison.

### Where it appears

| Column | Hashes | Intended for — see the status note below |
|---|---|---|
| `page_list.image_sha256` | the page image file's bytes | Download idempotency and image-level duplicate detection |
| `ocr_results.text_sha256` | the extracted `raw_text` | Cheap change detection without re-reading a `TEXT` column |

Computed by `sha256_file()` and `sha256_text()` in
[`core-pipeline/db/__init__.py`](core-pipeline/db/__init__.py). `sha256_file`
streams the file in 1 MB chunks, so hashing a large scan never loads it whole
into memory.

### Current status: written, never read

**Nothing reads these columns today.** Every reference in the codebase is the
column definition, its index, or a write path. No query selects, compares or
joins on them. The same is true of `page_list.file_size_bytes`.

The behaviours they were added to support are, today, provided by other
mechanisms entirely:

| Behaviour | What actually implements it |
|---|---|
| Skipping an already-downloaded page | `dest.is_file() and dest.stat().st_size > 0` in `stages/download_blob.py` — file existence on disk |
| Not duplicating `page_list` rows on re-ingest | `ON CONFLICT (chart_id, page_name)` — the UNIQUE constraint on page *name* |
| Duplicate page detection | `fingerprint()` in `stages/lib/junk/classify.py` — SHA-256 of whitespace-stripped lowercase **OCR text**, computed in memory and never stored |

So the duplicate check does use SHA-256 — a hash of normalised *text*, not the
stored image digest.

**This is not free.** `sha256_file()` runs on every page of every ingest,
including pages skipped as already-downloaded, so a re-ingest reads every file
end-to-end to populate a column nothing consults. Before building on
`image_sha256`, decide whether to use it or drop it.

### What it would buy us, if something read it

- **Byte-identical duplicate pages, detectable before OCR.** Two pages of a
  chart sharing an `image_sha256` are the same scan — the same fax page sent
  twice. Cheaper and more certain than the text fingerprint, which needs OCR
  to have run first.
- **True download idempotency.** Comparing the stored digest to the blob's
  would catch a page whose *contents* changed under an unchanged filename —
  which the current file-exists check cannot see.
- **"Did this page's OCR actually change?"** — one string comparison instead of
  pulling two possibly-megabyte `raw_text` values across the wire.

### What it could never do

- **Detect near-duplicates.** A page rescanned at a different brightness, or
  the same page rotated, has a completely different digest. That is precisely
  why the duplicate check hashes normalised text rather than pixels.
- **Guarantee integrity against tampering.** Nothing verifies a digest against
  a trusted external record; we would only be comparing our own digests to each
  other.
- **Say anything about content.** Two pages with different digests may be the
  same document. Two with the same digest are the same *file*.

`CHAR(64)` is the right column type: the output is always exactly 64 lowercase
hex characters.

---

## 3. `quality_tag` — a placeholder, on purpose

**Decision (2026-09-13): write a fixed grade derived from
`printed_or_handwritten` until the quality model is trained.**

| `printed_or_handwritten` | `quality_tag` | `quality_score` |
|---|---|---|
| `printed` | `high` | `0.8000` |
| `handwritten`, `mixed` | `low` | `0.5000` |
| missing | `NULL` | `NULL` |

The mapping lives in exactly one place — `quality_placeholder()` in
[`core-pipeline/db/__init__.py`](core-pipeline/db/__init__.py) — and
`upsert_quality()` calls it. No stage passes a quality grade in.

### Read this before using the column

**It carries no information.** It is a pure function of
`printed_or_handwritten`, which is already in the same row. Anything you could
learn from `quality_tag` you could learn from the column next to it.

Consequences, which are deliberate:

- **Do not branch on it.** A condition on `quality_tag` is a condition on
  `printed_or_handwritten` wearing a disguise, and it will silently change
  meaning the day a real model starts writing the column.
- **Do not show it to a reviewer as a measured score.** `0.8` next to a page
  reads as "the system assessed this page at 80%". It did not.
  `review-ui` therefore surfaces **`hw_confidence`** — the handwriting
  classifier's own score — and not `quality_score`.
- **Do not use it in accuracy reporting.** It would score exactly as well as
  guessing from `printed_or_handwritten`.

### Why the column was wrong before, and what changed

In v7 `quality_tag` held the handwriting classifier's *method* string
(`"model"` / `"heuristic"` / `"fallback"`). It never contained a quality grade
at all — the name and the contents disagreed. v8 splits them:

| Column | Holds |
|---|---|
| `quality_tag` / `quality_score` | the page quality grade (placeholder today) |
| `hw_method` | which classifier produced `printed_or_handwritten` |
| `hw_confidence` | that classifier's own confidence (was `confidence`) |

### Replacing the placeholder

When the model lands:

1. Delete `quality_placeholder()` and `QUALITY_PLACEHOLDER` from
   `core-pipeline/db/__init__.py`.
2. Add `quality_tag` and `quality_score` back as parameters of
   `upsert_quality()`.
3. Pass the model's output from `quality_rotation_hw.run()`.
4. Register the model in `model_registry` so `field_accuracy_log` and
   `model_accuracy_snapshots` can attribute scores to a version.
5. Then — and only then — let review-ui surface it.

No schema change is needed. The columns, types and CHECK constraint are already
what a real model would write.

---

## 4. Column naming conventions

Every table in **both** schema files obeys these. A new table that breaks one is a
bug, not a style preference. The conventions are repeated at the top of the
schema file so they are visible where they are applied.

1. **snake_case everywhere.** No camelCase. No abbreviations that are not
   already domain words (`dos` = date of service, `ocr`, `ner`, `hw`).
2. **Primary key is `id BIGSERIAL PRIMARY KEY`.** One exception:
   `pipeline_stage`, a static registry keyed by its natural key
   `(stage_name, pass_no)` and referenced by name, never by id.
3. **A foreign key is named `<referenced_table_singular>_id` and nothing else.**
   `chart_id`, `page_id`, `user_id`, `model_id`, `matched_member_list_id`.
   Never `reviewed_by` / `imported_by` / `review_id` for the same idea.
4. **Every table has `created_at` and `updated_at`**, both
   `TIMESTAMPTZ NOT NULL DEFAULT now()`, with a `trg_<table>_updated_at`
   trigger calling `set_updated_at()`. No bespoke row-birth names — no
   `imported_at`, `compared_at`, `evaluated_at`, `decided_at`. Timestamps that
   mean something *other* than row lifecycle keep their own name:
   `started_at`, `completed_at`, `queued_at`, `trained_at`, `lease_expires_at`.
5. **A model or rule score in [0,1] is `confidence NUMERIC(5,4)`.** When a table
   carries more than one, each is prefixed: `hw_confidence`, `quality_score`.
   Never `confidence_score` / `similarity_score` for the same idea.
6. **An ordinal within a parent row is `seq INT`.** Not `chunk_index`, not
   `sequence_no`, not `position`.
7. **Stage identity is always the pair `stage_name VARCHAR(50)` +
   `pass_no SMALLINT`.** A retry counter is `attempt INT`.
8. **Blob location is `blob_container VARCHAR(150)` + `blob_path TEXT`.**
9. **Booleans read as assertions:** `is_final`, `is_active`, `mirrored`,
   `rotation_applied`, `signature_present`.
10. **Indexes are `idx_<table>_<columns>`**, with the table's *full* name.
    Helper views are `v_<name>`; report views keep their business name
    (`consolidated_chart_results`).

### What v8 renamed

| Was | Now |
|---|---|
| `chart_list.blob_container_name` | `blob_container` |
| `chart_list.path` | `blob_path` |
| `ocr_quality_results.confidence` | `hw_confidence` |
| `ocr_quality_results.quality_tag` (held the method) | `hw_method` — see §3 |
| `page_classification.confidence_score` | `confidence` |
| `invoice_matching_results.similarity_score` | `confidence` |
| `chunk_results.chunk_index` | `seq` |
| `page_sequencing_results.sequence_no` | `seq` |
| `dos_extraction_dates` (whole table) | merged into `dos_extraction_results.dates` (JSONB array) |
| `member_verification_summary.matched_member_id` | `matched_member_list_id` |
| `member_verification_summary.decided_at` | `created_at` / `updated_at` |
| `rejection_results.reviewed_by` | `user_id` |
| `ground_truth_csv.imported_by` / `imported_at` | `user_id` / `created_at` |
| `field_accuracy_log.compared_at` | `created_at` |
| `model_accuracy_snapshots.evaluated_at` | `created_at` |
| `manual_review.review_id` | `user_id` |
| `pipeline_jobs.attempt_number` | `attempt` |

### Date of service is one table

`dos_extraction_results` is **one row per page**. The page-level and
document-level dates are single-valued columns; every date the page carries
lives in the multi-valued `dates` JSONB array:

```json
[{"seq": 1, "date_of_service_from": "2024-03-15",
  "date_of_service_to": "2024-03-15",
  "source_keyword": "Date of Service", "confidence": 0.95}]
```

`date_count` is `GENERATED ALWAYS AS (jsonb_array_length(dates)) STORED`, so it
cannot drift from the array. JSON keys deliberately mirror the column names —
one vocabulary, not two.

v7 split this across a parent and a `dos_extraction_dates` child, which cost a
DELETE plus one INSERT per date on every page (a 400-page chart with 3 dates
each: 400 deletes + 1200 inserts) — and nothing ever read the child table back.
It is now a single upsert per page.

Also dropped: `member_extraction_results.page_name` (join `page_list` instead —
no other result table denormalises it) and `v_chart_status_legacy` (no v6
consumer remains).

### Checking the conventions hold

The schema is validated structurally, not just by eye. Before committing a
schema change, confirm that every trigger matches its table, every index is
prefixed with its table's full name, every indexed column exists, and every
table has both lifecycle timestamps.

### Two vocabularies that are *not* the same thing

Database column names follow the rules above. **CSV column names are a separate,
frozen contract** with the V1 reference and with review-ui's Local Mode — e.g.
`MEMBER_EXTRACT_COLS` in `stages/member_extract_verify.py`, and the headers
`imaging_overlays.py` reads. Renaming a database column does **not** license
renaming the CSV header, and vice versa. When they differ, they differ on
purpose.

---

## 5. The schema: V1 is implemented, V2 is not

```
schema/
├── v1.sql          ← IMPLEMENTED. 12 tables + 2 views. Required.
└── v2.sql          ← NEXT PHASE. 14 tables + 3 views. Nothing uses them.
```

```bash
psql "$DATABASE_URL" -f schema/v1.sql   # required
psql "$DATABASE_URL" -f schema/v2.sql   # optional
```

**The split is the point.** `v1.sql` contains only relations that running code
writes or reads — enforced by `TestSchemaSplit` in `tests/test_contracts.py`,
which fails if any module starts touching a V2 table, if a relation is defined
in both files, or if V1 grows a reference into V2.

`v2.sql` is a set of **proposals**. The tables are defined so the design is
reviewable and so adding a module is a code change rather than a schema
argument — but none has ever been exercised by real code, so revisit a table's
columns before building the module that fills it. V1 references nothing in V2,
so V2 is genuinely optional.

When a V2 module ships, move its table block from `v2.sql` to `v1.sql`. The
split test tells you the moment that is needed.

There is **no migrations directory** and **no second copy**. `Reference/schema.sql`
(a stale v6 duplicate) and `schema/migrations/002_v6_to_v7.sql` were deleted in
v8 — three files describing one database is three chances to describe it
differently.

**A pre-v8 database is recreated from this file, not upgraded in place.** That
is the trade accepted when the migration was deleted: there is no v6→v8 or
v7→v8 path. If you are pointed at a database with data you care about, dump it
before applying.

When you change the schema, the whole change is an edit to this file. Then:

- update every reader and writer (`core-pipeline/db/__init__.py`, the stages,
  `review-ui/backend/app/adapters/postgres/repository.py`),
- update the column tables in `docs/ARCHITECTURE.md` and `docs/LOGIC.md`,
- add a line to the "WHAT CHANGED" block at the top of `v2.sql`,
- run `python -m pytest tests/ -q`.

Verify an applied schema:

```bash
psql "$DATABASE_URL" -c "SELECT stage_name, pass_no, seq FROM pipeline_stage ORDER BY seq;"
```

Twelve rows, eight of them `is_phase1`. An empty `pipeline_stage` makes
`/ready` return 503 and no chart can progress.

---

## 6. Setting up dependencies

Full detail in [`docs/API.md § Installing dependencies`](docs/API.md#installing-dependencies).
The short version:

### System tools

| Tool | macOS | Linux | Windows |
|---|---|---|---|
| **Python 3.12** (3.11 ok, **not 3.13+**) | `brew install python@3.12` | `apt install python3.12 python3.12-venv` | `winget install Python.Python.3.12` |
| `tesseract` (stage 1) | `brew install tesseract` | `apt install tesseract-ocr` | [UB Mannheim installer](https://github.com/UB-Mannheim/tesseract/wiki) |
| `psql` (applying the schema) | `brew install libpq` | `apt install postgresql-client` | PostgreSQL installer |
| Node 20+ (frontend outside Docker) | `brew install node` | `apt install nodejs npm` | `winget install OpenJS.NodeJS.LTS` |

**Python 3.13+ does not work** — `rapidocr-onnxruntime` requires `<3.13` and
pip reports it as "no matching distribution", which reads like a missing
package rather than a version conflict. The Docker image pins `python:3.12-slim`.

Set `TESSERACT_CMD` if `tesseract` is not on `PATH` — **always required on
Windows**, where the installer does not add it:
`TESSERACT_CMD=C:\Program Files\Tesseract-OCR\tesseract.exe`

### Python

Two services, two virtualenvs. Full commands for both platforms:
[`docs/API.md § Installing dependencies`](docs/API.md#installing-dependencies).

**macOS / Linux**

```bash
cd core-pipeline
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cd ../review-ui/backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# tests, from the repo root — no database needed
python -m pytest tests/ -q          # 111 tests
```

**Windows (PowerShell)**

```powershell
cd core-pipeline
py -3.12 -m venv .venv              # -3.12: plain `py` picks your newest
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

cd ..\review-ui\backend
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# tests, from the repo root — no database needed
python -m pytest tests/ -q          # 111 tests
```

If `Activate.ps1` is blocked:
`Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`.

### The NER layer (GLiNER) — optional, and it gates rejection

**This is the dependency that changes what the pipeline can conclude.** Member
verification's wrong-member check reads names off a page with GLiNER. Without
it, verification still runs (the rule pass extracts), but **no page can be
marked `wrong_member`, so no document can ever be Rejected** — every chart comes
back Accepted or `needs_review`.

It is off by default: the runtime is ~2.5 GB and the checkpoints ~2 GB, and
neither is vendored.

```bash
# macOS / Linux
cd core-pipeline && source .venv/bin/activate

pip install -r requirements-ner.txt                                     # runtime
python -m stages.lib.member.extractors.ner_based.model_downloader        # ~2 GB
python -m stages.lib.member.extractors.ner_based.model_downloader --check
export MEMBER_NER_ENABLED=true          # or set it in core-pipeline/.env
```

```powershell
# Windows (PowerShell)
cd core-pipeline; .venv\Scripts\Activate.ps1

pip install -r requirements-ner.txt
python -m stages.lib.member.extractors.ner_based.model_downloader
python -m stages.lib.member.extractors.ner_based.model_downloader --check
$env:MEMBER_NER_ENABLED = "true"        # session only; use .env to persist
```

The downloader fetches `gliner_large` / `gliner_medium` / `gliner_low`, skips
what is already present, and is resumable. `--force` re-downloads; `--check`
verifies on-disk checkpoints (loading each one and repairing its config paths)
without downloading. It prints `3/3 models ready` and exits 0 when all are
usable, or exits 1 naming the model that failed.

| Variable | Default | Meaning |
|---|---|---|
| `MEMBER_NER_ENABLED` | `false` | Master switch. Everything else is inert while false |
| `MEMBER_NER_MODELS_PATH` | `Reference/V1 Code/Member_Verification/Models` | Where checkpoints live |
| `MEMBER_NER_MODEL_ID` | `gliner_medium` | Which model the extractor uses |
| `GLINER_LARGE` / `_MEDIUM` / `_LOW` | `true` | Which checkpoints count as available |

In Docker the runtime is a build arg and the weights are a mount — the image
never contains them. All three are required:

```bash
docker compose build --build-arg WITH_NER=true
NER_MODELS_HOST_PATH=./models/ner MEMBER_NER_ENABLED=true docker compose up -d
```

Confirm with `curl -s localhost:8001/health | python -m json.tool`:
`member_ner.ready` is the answer, and when it is `false`, `member_ner.reason`
names the one precondition to fix.

### Other optional services

| Feature | Enable with | Absent ⇒ |
|---|---|---|
| Azure Blob intake | `AZURE_STORAGE_*` | `ingest` fails; `register-local` still works |
| Final OCR 2 | `AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT` + `_KEY` | no final2 text; handwritten pages get no pass-2 verdict |
| DOS LLM pass | `AZURE_OPENAI_*` + `DOS_LLM_ENABLED=true` | DOS is regex-only, rows stamped `extraction_method='rules'` |

Every one of these degrades a stage in a way the **run records**, so a degraded
run is visible in the data rather than silent.

---

## 7. Starting the APIs

| Service | Port (Docker) | Port (local) | Check |
|---|---|---|---|
| core-pipeline API | 8001 | 8001 | <http://localhost:8001/docs> |
| review-ui backend | 3000 | 8002 | <http://localhost:3000/docs> |
| review-ui frontend | 3001 | 5174 | <http://localhost:3001> |

From an empty machine:

```bash
# macOS / Linux
psql "$DATABASE_URL" -f schema/v1.sql          # 0. schema, once

cd core-pipeline                               # 1. writes data/folders
cp .env.example .env
docker compose up -d --build
curl -fsS localhost:8001/ready

cd ../review-ui                                # 2. mounts it read-only
cp .env.example .env                           # DATA_MODE=local needs no DB
docker compose up -d --build
open http://localhost:3001
```

```powershell
# Windows (PowerShell)
psql $env:DATABASE_URL -f schema/v1.sql

cd core-pipeline
Copy-Item .env.example .env
docker compose up -d --build
curl.exe -fsS localhost:8001/ready             # curl.exe, NOT curl

cd ..\review-ui
Copy-Item .env.example .env
docker compose up -d --build
start http://localhost:3001
```

Order does not matter — the services are independent and neither calls the
other. `docker compose down` in either directory stops only that service.

### Without Docker

```bash
# macOS / Linux — one terminal each
cd core-pipeline && source .venv/bin/activate && python cli.py serve
cd review-ui/backend && source .venv/bin/activate && uvicorn app.main:app --host 127.0.0.1 --port 8002 --reload
cd review-ui/frontend && npm install && npm run dev
```

```powershell
# Windows (PowerShell) — one terminal each
cd core-pipeline; .venv\Scripts\Activate.ps1; python cli.py serve
cd review-ui\backend; .venv\Scripts\Activate.ps1; uvicorn app.main:app --host 127.0.0.1 --port 8002 --reload
cd review-ui\frontend; npm install; npm run dev
```

### Health endpoints

```bash
curl localhost:8001/health   # liveness + which optional features are actually on
curl localhost:8001/ready    # 503 unless the DB is reachable AND pipeline_stage is seeded
```

`/health` is the first thing to read when a feature "does not work" — it names
the missing precondition rather than making you guess.

### Ingesting a chart

```bash
curl -X POST localhost:8001/api/charts/ingest \
  -H 'Content-Type: application/json' \
  -d '{"blob_container":"imaging-pipeline","blob_path":"run1/batch1/52743839_44976074"}'
```

Mutating endpoints return **202** and work in the background. Poll
`GET /api/charts/{chart_id}` for progress. Full reference and the CLI
equivalents: [`docs/API.md`](docs/API.md).

---

## 8. Windows

The code is portable — every file read/write declares `encoding="utf-8"`, CSV
writers set `newline=""`, there are no POSIX-only imports and no shell-outs.
Only the shell commands differ. Full tables in
[`docs/API.md § Windows notes`](docs/API.md#windows-notes); the four that
actually bite:

1. **Python 3.13+ fails** — `rapidocr-onnxruntime` requires `<3.13`. Create the
   venv with `py -3.12`; plain `py`/`python` picks the newest interpreter.
2. **`Activate.ps1` is blocked** until
   `Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser`.
3. **Tesseract is not on PATH** — set `TESSERACT_CMD` in `core-pipeline\.env`.
4. **`curl` is `Invoke-WebRequest`** in PowerShell — use `curl.exe`.

Quick reference:

| | macOS / Linux | PowerShell | cmd.exe |
|---|---|---|---|
| activate venv | `source .venv/bin/activate` | `.venv\Scripts\Activate.ps1` | `.venv\Scripts\activate.bat` |
| delete a tree | `rm -rf x` | `Remove-Item -Recurse -Force x` | `rmdir /s /q x` |
| copy a file | `cp a b` | `Copy-Item a b` | `copy a b` |
| env var | `export X=1` / `"$X"` | `$env:X = "1"` / `$env:X` | `set X=1` / `%X%` |

Git Bash uses the macOS/Linux column, except activation is
`source .venv/Scripts/activate` (`Scripts`, not `bin`).

**Get the code with `git clone`, never by copying a working tree.** A copy has
no integrity check; one file arriving with the wrong contents surfaces as an
`ImportError` that looks like a code bug. `.gitattributes` pins the repo to LF
(CRLF breaks shebangs inside the Linux containers), so leave `core.autocrlf`
alone. Prefer a path without spaces.

---

## 9. Working rules

- **`Reference/` is never edited.** It is the V1 prototype tree and the source
  of truth for ported logic. Port *from* it; diff *against* it. If ported code
  and the reference disagree, the reference is right until someone decides
  otherwise in `PLAN.md`.
- **Resume is the default; `--force` costs money.** Stage 5 (Azure Document
  Intelligence) is billed per page. `run_pipeline_for_chart(force=True)` and
  `cli.py run --force` reprocess pages already completed.
- **A degraded run must be visible in the data.** When an optional dependency is
  missing, stamp what actually happened (`extraction_method='rules'`,
  `hw_method='fallback'`, `member_ner.ready=false`) rather than failing silently
  or pretending the full path ran.
- **One bad page must not sink a chart.** Stages catch per-page exceptions,
  record them in `page_stage_status.error_message`, and continue.
- **CSVs are rebuilt from the database, never appended to.** A resumed run must
  not be able to leave a half-written file.
- **Run the tests.** `python -m pytest tests/ -q` — 111 tests, no database
  needed, under a second.

## 10. Known gaps

- **No authentication on either service.** Charts carry member names and dates
  of birth. This is the blocker before any non-local deployment.
- **Rejection is unreachable without GLiNER.** See §6.
- **No Postgres in this dev environment.** Both schema files are validated
  structurally (triggers, index columns, naming) but has not been executed
  against a live server from here — apply it to a scratch database before
  trusting it in place.
- `manual_review` and `rejection_results` exist in the schema for a later phase.
  Nothing writes them; review-ui is read-only and every route is a `GET`.
