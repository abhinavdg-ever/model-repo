# Core Pipeline + Review UI — architecture plan

> Living document. Update it in the same change set as any architecture, schema,
> mode or stage-order change, and bump the date.
> Last updated: 2026-09-12 (schema v7, V1 member verification port, resumable stages)

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
- **The reference is authoritative.** Logic under `Reference/` is ported verbatim
  where possible; every adaptation is commented with what changed and why, and
  pinned by tests.
- **Review UI is a viewer.** Read-only by design for this phase.

---

## What changed in this revision

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

`Reference/V1 Code/Member_Verification/` (~2,800 lines) is now in
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
├── docs/                       FLOW · LOGIC · API · ARCHITECTURE
├── Reference/                  prototypes — source of truth, never edited
├── schema/
│   ├── v1.sql                  implemented: 12 tables + 2 views
│   └── v2.sql                  next phase: 14 tables + 3 views, unused
├── tests/                      108 tests
├── core-pipeline/              own docker-compose, port 8001
│   ├── api/ cli.py config.py
│   ├── orchestrator/runner.py
│   ├── db/                     persistence · status · paths · blob
│   ├── jobs/manifest_sweeper.py
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

## How to update this plan

1. Edit this file in the same change set.
2. Bump **Last updated**.
3. Adjust the **Status** table.
4. If the change touches flow, logic, the API or file roles, update the matching
   document in `docs/`. Keep `README.md` short — depth lives here and in `docs/`.
