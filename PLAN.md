# Core Pipeline + Review UI — architecture plan

> Living document. Update it in the same change set as any architecture, schema,
> mode or stage-order change, and bump the date.
> Last updated: 2026-09-14 (rotation first + corrected-pages; API run/batch/write; sharding proposed)

**Detailed documentation lives in [`docs/`](docs/):**

| Document | Covers |
|---|---|
| [docs/FLOW.md](docs/FLOW.md) | What runs when — end-to-end diagrams, skip rules, resume, status transitions |
| [docs/LOGIC.md](docs/LOGIC.md) | How each decision is made, and the exact rows it writes |
| [docs/API.md](docs/API.md) | Running both services, full API reference, CLI, troubleshooting |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System shape, data model, **role of every file**, known limits |

---

## Status

| Phase | Item | Status |
|-------|------|--------|
| 0 | Version control, contract tests (108), dead-link cleanup | **Done** |
| 1 | Monorepo scaffold + shared `data/folders` contract | Done |
| 2 | Ingest API + blob download + `chart_list` / `page_list` | Done |
| 3 | Tesseract prelim + rotation / handwriting | Done |
| 4 | Blank/junk pass 1 + final OCR + pass 2 | Done |
| 5 | Manifest batch loader | Done |
| 6 | DOS stage + review-ui Local / Production modes | Done |
| **7a** | **Schema v7** — stage registry, `page_stage_status`, decoupled manifest | **Done** |
| **7b** | **Member verification ported from V1** (rules + NER + what-if + downloader) | **Done** — code complete; NER runtime + checkpoints are an install step, not vendored |
| **7c** | **Review-UI viewer fixes** — cache invalidation, junk stream, v7 queries | **Done** |
| **7e** | **Orchestration** — pooling, per-page resume, engine reuse, parallelism | **Done** |
| 7d | Review write actions (accept/reject/correct) | **Deliberately not built** — UI stays a viewer |
| — | Auth / PHI handling | **Not addressed** — see [Known limits](docs/ARCHITECTURE.md#6-known-limits) |
| — | Work queue (claim/lease worker over `pipeline_jobs`) | Schema ready, worker not built |
| — | Batch sharding over N chart workers | **Designed, not built** — see [Proposed](#proposed-shard-a-batch-across-n-chart-workers) |
| — | Rotation correction applied to the page image | Plumbing shipped; **disabled** — the detector cannot recover a rotated page, see [Blocked](#blocked-the-orientation-detector-cannot-recover-a-rotated-page) |
| Later | Page subtype / encounter / sequencing / rejection | Registered in `pipeline_stage`, `is_phase1=false` |

---

## Default decisions

- **Two independent deployments.** `core-pipeline/` and `review-ui/` each have
  their own `docker-compose.yml`. They share a Postgres database and the
  `data/folders` volume (pipeline writes, UI mounts read-only) and **never call
  each other**.
- **Schema source of truth:** [`schema/v1.sql`](schema/v1.sql) — every table
  that is implemented and running. [`schema/v2.sql`](schema/v2.sql) holds the
  next phase and is not implemented; applying it is optional and V1 never
  references it. No migrations directory; a pre-v8 database is recreated.
- **Phase-1 scope:** intake → prelim OCR → quality → blank/junk ×2 → final OCR ×2
  → member verify → DOS. Later modules stay in the schema and the stage registry
  but are not orchestrated.
- **The V1 prototypes are authoritative.** Their logic is ported verbatim
  where possible; every adaptation is commented with what changed and why, and
  pinned by tests.
- **Review UI is a viewer.** Read-only by design for this phase.

---

## What changed in this revision

### Chart API — three verbs (2026-09-14)

| # | Change | Why |
|---|---|---|
| 1 | `POST /api/charts/run` replaces `ingest`, `import-local` and `register-local` | Three endpoints differing only in where the pages came from. One source argument covers all three, and endpoints that overlap are how they drift apart. |
| 2 | `batch` now calls `ingest_and_run` per folder instead of keeping its own local branch | The duplicate branch silently dropped `force` on local charts. An option added to `run` now reaches `batch` without being plumbed twice. |
| 3 | `POST /api/charts/write` — the reverse of run's intake step | `pages/` + `ocr/` + `imaging/` out to blob or local, so a destination is readable without the database. Copies; the workspace is left intact. |
| 4 | `through` / `only` on `run`, `batch` and `rerun` | "Stop after this stage" and "run just this stage" are different questions. `resolve_stage()` is the single parser, so a bare name is always pass 1 and an unknown name is a 400, not a silent no-op. |
| 5 | Removed `move`, `recursive`, `load_manifest`, `run_pipeline` | Each had one correct setting: always copy, always search subfolders, always load a manifest beside the images, always run. |
| 6 | `chart_name` derives from the last path segment everywhere | Optional on `run`, absent from `batch`, required only on `write` where there is no source to derive it from. |
| 7 | CLI mirrors the API: `run`, `batch`, `write`, `rerun` | `run <chart_id>` became `rerun <chart_id>`, freeing `run` for the intake command it names. The one breaking CLI change. |

Also: Azure OpenAI authenticates by managed identity as well as by key
(`AZURE_OPENAI_AUTH=key|entra|auto`), so a keyless VM can run the DOS LLM pass —
previously `DOS_LLM_ENABLED` required an API key and gated itself off.

### Schema v6 → v7

| # | Change | Why |
|---|---|---|
| 1 | `pipeline_stage` table | The pipeline's shape as data. Adding a stage is an INSERT, not a CHECK migration. |
| 2 | `page_stage_status` replaces the 11 `page_list.*_status` columns | Keyed `(page_id, stage_name, pass_no)`, so blank/junk's two passes are representable — v6 could not express them — and resume has a query. |
| 3 | `chart_list.status` split into lifecycle + `current_stage`/`current_pass` | v6 packed the stage name into `status`, needing a migration per stage. |
| 4 | `chart_list UNIQUE (chart_name)` | v6 looked charts up with `ORDER BY id DESC LIMIT 1`; two concurrent ingests could create two charts with one name. |
| 5 | `manifest_member_list` decoupled from `chart_list`; keyed on `record_id`; first/middle/last stored separately | A manifest is a fact about a client RecordId. v6 created a placeholder chart per row, and the verification rules need name parts individually. |
| 6 | `blank_junk_classification`: `pass_no` + `is_final` + constrained `junk_subtype` | Exactly one final verdict per page, enforced by a partial unique index. No downstream precedence guessing. |
| 7 | Member results carry the V1 vocabulary | `page_status` (`verified`/`wrong_member`/`not_verified`) and per-field detection provenance. Without `wrong_member` there is no reject path. |
| 8 | Member summary carries `document_decision`, `wrong_member_pages`, `reject_threshold` | The actual business decision, which v6's `final_status` could not represent. |
| 9 | `dos_extraction_dates` child table | A page can carry several dates; v6 held one pair while the CSV contract already allowed lists. |
| 10 | `page_list.image_sha256` / `ocr_results.text_sha256` | Download idempotency, image-level dedup, cheap change detection. |
| 11 | `pipeline_jobs`: `lease_expires_at`, `heartbeat_at`, page counters | So the table can back a real work queue when one is built. |

### Member verification — the real port

The V1 `Member_Verification/` tree (~2,800 lines) is now in
`core-pipeline/stages/lib/member/`: rule-based extractors, the GLiNER NER layer,
the wrong-member check and the what-if thresholds. Logic is verbatim; only
imports changed.

It replaced a three-regex placeholder. The algorithm is **expected-member
driven** — it searches each page for the manifest's specific name, DOB and
MemberID rather than extracting a name and comparing afterwards. Rules run first;
NER is reached only for a field the rules missed.

`_member_v1_compare.csv` is written in the reference's own column order so a
pipeline run can be diffed directly against a V1 run.

#### The GLiNER layer — ported, but off by default

Everything is in the repository: the extractors, the key-sentence builder, the
model loader and catalog, and the model downloader. Two things are deliberately
**not** vendored, because neither belongs in git:

| Not vendored | Size | How it arrives |
|---|---|---|
| Runtime (`gliner`, `torch`, `transformers`) | ~2.5 GB | `pip install -r core-pipeline/requirements-ner.txt`, or `docker build --build-arg WITH_NER=true` |
| The three checkpoints | ~2 GB | `python -m stages.lib.member.extractors.ner_based.model_downloader` |

So `MEMBER_NER_ENABLED` defaults to `false`, and while it is off **no page can
be classified `wrong_member`, so no document can be Rejected.** That is stamped
on every row (`ner_enabled`), suffixed onto the summary's `decision_reason`, and
reported by `GET /health` → `member_ner`, which names whichever of the three
preconditions is unmet. [Turning it on](docs/LOGIC.md#turning-it-on).

### Defects fixed

| Defect | Was | Now |
|---|---|---|
| Duplicate detection scope | Fingerprints rebuilt per pass over that pass's pages only, so a printed page duplicating a handwritten page was invisible | Seeded from every page already judged, in page order |
| CSV idempotency | Pass 1 truncated, pass 2 appended — re-running pass 2 duplicated every row | Every CSV rebuilt from the database |
| DOS stage | Called only the per-page regex, bypassing the reference driver — no LLM pass, no document carry-forward, and `doc_dos_from_iso` was a copy of the un-normalised value | Calls `detect_dos_per_page`, the reference's own driver |
| Junk stream invisible in the UI | The folder list branched on five CSV suffixes but not `_junk.csv` | Branch added; a contract test now covers every suffix |
| Review UI served stale data | The scan cache had no invalidation, so pipeline writes were invisible until restart | mtime-signature check with a short TTL |
| Per-page connections | A new Postgres connection per page per stage | Pooled |
| Models rebuilt per page | `RapidOCR()`, the DI client and the handwriting model were constructed inside the per-page function | Built once per process |
| No resume | Every re-run redid all eight stages from the top, re-billing Azure DI | `pages_needing_stage` skips completed work; `force` opts out |

---

## Repository layout

```text
advantmed-imaging-pipeline/
├── PLAN.md                     ← this file
├── check_azure_openai.py       ad-hoc, self-contained: does the LLM endpoint answer?
├── docs/                       FLOW · LOGIC · API · ARCHITECTURE
├── schema/
│   ├── v1.sql                  implemented: 12 tables + 2 views
│   └── v2.sql                  next phase: 14 tables + 3 views, unused
├── tests/                      192 tests
├── core-pipeline/              own docker-compose, port 8001
│   ├── api/ cli.py config.py
│   ├── orchestrator/runner.py
│   ├── db/                     persistence · status · paths · blob
│   ├── jobs/                   manifest_sweeper · batch_intake · export_chart
│   └── stages/
│       ├── (8 stage modules) + _support.py
│       └── lib/junk · lib/member · lib/dos
└── review-ui/                  own docker-compose, ports 3000/3001
    ├── backend/app/            adapters: local | postgres
    ├── frontend/src/
    └── data/folders/           ★ shared workspace
```

Per-file roles: [docs/ARCHITECTURE.md § File inventory](docs/ARCHITECTURE.md#4-file-inventory).

---

## The stage chain

| # | Stage | Pass | Runs on | Produces |
|---|---|---|---|---|
| 1 | `ocr_prelim` | 1 | every page | `ocr_results` (tesseract) |
| 2 | `ocr_quality` | 1 | every page | `ocr_quality_results` |
| 3 | `blank_junk` | 1 | printed only | `blank_junk_classification` |
| 4 | `ocr_final1` | 1 | survivors + handwritten | `ocr_results` (docling) |
| 5 | `ocr_final2` | 1 | survivors + handwritten | `ocr_results` (azuredocintel) |
| 6 | `blank_junk` | 2 | handwritten + survivors | final verdict stamped |
| 7 | `member_verify` | 1 | not blank/junk | member rows + summary |
| 8 | `dos_extract` | 1 | not blank/junk | DOS rows + dates |

Stage 5 is billed per page. Stage 7 produces the accept/reject decision.

**Chart status rule:** `current_stage` is the earliest stage in
`pipeline_stage.seq` order where not every page is `completed` or `skipped`.

---

## Quick start

```bash
# 1. Schema
psql "$DATABASE_URL" -f schema/v1.sql   # required — what is implemented
psql "$DATABASE_URL" -f schema/v2.sql   # optional — next phase, nothing uses it yet

# 2. core-pipeline
cd core-pipeline && cp .env.example .env && docker compose up -d --build

# 3. review-ui
cd ../review-ui && cp .env.example .env && docker compose up -d --build

# 4. Tests
python -m pytest tests/ -q
```

Full instructions: [docs/API.md](docs/API.md).

---

## Blocked: the orientation detector cannot recover a rotated page

**Status: measured 2026-09-14. Plumbing shipped, correction disabled.**

Rotation now runs as stage 1, ahead of every OCR pass, and writes corrected
images to `<chart>/corrected-pages/` which all three OCR stages prefer over the
original. That part is done and tested. The *writing* is off by default
(`ROTATION_CORRECTION_ENABLED=false`) because the detector is not good enough
to act on.

### The measurement

Demo chart, three pages, each rotated to all four orientations, then detected,
corrected, and compared with the original:

| Input | Detected `rotation` | Outcome |
|---|---|---|
| upright | 0 | correct — no-op |
| 90° CW | 0 or 180, never 270 | **sideways page left sideways** |
| 180° | 0 or 180 | sometimes right |
| 270° CW | 0, never 90 | **sideways page left sideways** |

**0 of 6 sideways pages recovered.** `mirror=true` was reported on 3 of 12
cases that were not mirrored — applying that flips a page horizontally and
makes OCR strictly worse than leaving it alone.

`rotation_confidence` was **1.000 on the wrong answers**, so it cannot gate the
decision, and `needs_review` was `true` on every case including the upright
one. There is no field in the result that separates the right answers from the
wrong ones.

### Why this matters more now than before

Before this change the detector's output was recorded in
`imaging/<chart>_rotation.csv` and read by nobody — a wrong angle was a wrong
number in a file. Now the same number would rewrite the page image that OCR
reads. The blast radius changed; the accuracy did not.

### What would unblock it

The detector's coarse-rotation stage is what fails — tilt and the upright case
are fine. Either:

1. Fix `_detect_coarse_rotation` in `stages/lib/imaging/rotation.py` so a 90°
   page reports 270 and a 270° page reports 90, or
2. Replace the coarse step with Tesseract OSD (`--psm 0`), which reports
   orientation directly and is already a dependency — note this would make
   stage 1 depend on Tesseract, which it currently does not.

Either way the acceptance test is the round trip: rotate a page, detect,
correct, and assert the result matches the original. That test is cheap and
should land with the fix.

Until then `rotation_applied` is always `false` and the angle columns record
what was measured, so nothing is lost — the pipeline behaves exactly as it did
before, and turning the flag on is a one-line change once the round trip passes.

---

## Proposed: shard a batch across N chart workers

**Status: designed, not built.** Decided 2026-09-14. Recorded here because it
reverses a deliberate decision — `run_batch` runs charts one at a time, and the
comment saying so is load-bearing. Anyone changing it should read this first.

### What is being asked for

`POST /api/charts/batch` gains `workers` (default 1, so nothing changes unless
asked): N charts run concurrently inside the API process, instead of strictly
one after another.

### Why the original "one at a time" reasoning is only half right

The existing comment argues that each chart already fans out across its pages
(`STAGE_WORKERS`), so overlapping charts multiplies memory and spend without
finishing sooner. Measured, that holds for **big** charts and fails for **small**
ones:

- A stage's page pool is `min(STAGE_WORKERS, len(todo))`. A 3-page chart with
  `STAGE_WORKERS=4` uses **three** threads and leaves the fourth idle. A drop of
  forty small charts therefore never saturates the box, and the serial loop is
  the binding constraint, not the CPU.
- A 400-page chart already saturates its pool. Running four of those
  concurrently adds queueing, not throughput.

So the win is real but **conditional: many small charts, or stages that wait**
(Azure DI, blob download). It is not a general speedup, and the plan should not
be sold as one.

### What does NOT block this

The per-chart engines are already process-wide singletons behind double-checked
locks — `_get_hw_model()`, `_get_detector()`, `_get_engine()`, `_get_client()`.
They are already called concurrently by `STAGE_WORKERS` threads today, so:

- **Model memory does not multiply.** Four concurrent charts share one RapidOCR
  engine and one handwriting classifier. This is the single biggest reason to
  prefer threads over processes here.
- **Chart-level concurrency is not a new class of hazard.** It is more threads
  against objects that already take concurrent calls.
- Charts share no mutable state otherwise: separate workspace directories,
  separate `chart_list` rows, per-chart try/except already in the batch loop.

### What DOES block it, in order

1. **The connection pool, which will deadlock first.** `DB_POOL_MAX` is 8;
   `workers × STAGE_WORKERS` is 16 at the proposed defaults. The symptom is a
   `PoolTimeout` 30 seconds in, which reads like a database fault rather than a
   configuration one. **`workers × STAGE_WORKERS + headroom ≤ DB_POOL_MAX` is
   the invariant**, and the code should refuse to start a batch that violates it
   rather than discovering it under load. `config.py` already tells the reader
   to keep `STAGE_WORKERS <= DB_POOL_MAX`; this makes that arithmetic a
   precondition instead of a comment.

2. **CPU oversubscription.** Tesseract and RapidOCR are CPU-bound and release
   the GIL, so threads do give real parallelism — up to the core count. Sixteen
   threads on 8 cores is slower per chart, not faster overall. The useful
   default is `workers × STAGE_WORKERS ≈ cores`, which for a 4-worker batch
   means dropping `STAGE_WORKERS`, not raising the total.

3. **Azure Document Intelligence.** Stage 5 is billed per page and rate-limited.
   Four charts in stage 5 at once is 4× the in-flight requests; the failure is a
   429 that currently surfaces as a per-page error. Cross-chart concurrency
   needs a **global** cap on stage-5 calls, not a per-chart one — a module-level
   semaphore in `ocr_final2_azure`, sized independently of `workers`.

4. **Interleaved logs.** `[n/total]` progress and the per-stage `=== [Stage] ===`
   banners assume one chart at a time. With four in flight the log stops being
   readable as a narrative. Every line needs the chart name, and `[n/total]`
   should become "started/completed" counters rather than a position.

### Shape of the change

| File | Change |
|---|---|
| `jobs/batch_intake.py` | The `for` loop becomes a `ThreadPoolExecutor(workers)` over the same `sources` list. `ingest_and_run` is already the single per-chart call, so this is the only place that changes. |
| `api/main.py` | `workers` on `BatchRequest`; reject `workers × STAGE_WORKERS > DB_POOL_MAX - headroom` with a 400 naming both numbers. |
| `cli.py` | `--workers` on `batch`. |
| `stages/ocr_final2_azure.py` | Module-level semaphore capping concurrent Azure DI calls across all charts. |
| `stages/_support.py` | Chart name in every per-stage log line. |
| `config.py` | `BATCH_WORKERS` default 1; document the invariant next to `STAGE_WORKERS`. |

Deliberately **not** in scope: process-level workers, and anything touching
`pipeline_jobs` leases. See below.

### Why this is not the claim/lease worker

`pipeline_jobs` already carries `lease_expires_at`, `heartbeat_at` and `attempt`
precisely so a worker process can claim charts —
[ARCHITECTURE.md §6](docs/ARCHITECTURE.md#6-known-limits) records that as the
intended answer. A thread pool inside one process is **not** that, and does not
become that:

- Work in flight is still lost on restart (resume makes a re-run cheap, but the
  batch must be re-issued).
- The cap is per batch call, not global — two concurrent `/batch` requests still
  oversubscribe.
- No automatic retry of a stage that raised.

The thread pool is worth building first because it is small, reversible, and
answers the actual complaint (a drop of small charts takes too long). It should
be understood as a stopgap that the queue later replaces, not as the queue.

### How we would know it worked

Measure before changing anything: a drop of ~20 small charts, `workers=1` vs
`workers=4`, wall-clock from the batch log. If the improvement is under ~1.5×,
the bottleneck is CPU rather than the serial loop and the change is not worth
its concurrency cost.

---

## How to update this plan

1. Edit this file in the same change set.
2. Bump **Last updated**.
3. Adjust the **Status** table.
4. If the change touches flow, logic, the API or file roles, update the matching
   document in `docs/`. Keep `README.md` short — depth lives here and in `docs/`.
