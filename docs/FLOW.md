# Flow — what happens when

What moves through the system, in order, and what each step leaves behind.
Setup and the API: [API.md](API.md). The VM and the release path:
[specials/VM_GUIDE.md](specials/VM_GUIDE.md).

---

## 1. The pieces

```
            Azure Blob                                   PostgreSQL
  raw_input/... (page images)                    every result, per page
  Processed/... (the chart's outputs)            (schema/schema.sql)
        │   ▲                                         ▲        │
        │   │ writes outputs                   writes │        │ reads
        ▼   │                                         │        ▼
  ┌───────────────────────────┐              ┌──────────────────────────┐
  │ core-pipeline  (port 8001)│              │ review-ui  (4000 / 4001) │
  │ FastAPI + the 15 stages   │              │ viewer + ground truth    │
  │ outputs/charts = scratch  │              │ page images from Blob    │
  └───────────────────────────┘              └──────────────────────────┘
```

- The two services share **data, not code**: the database and Azure Blob.
- They never call each other, with one exception: review-ui's **Playground** (admins
  only) starts a core-pipeline batch run on one of `CORE_PIPELINE_URLS`.
- `core-pipeline/outputs/charts/<chart>/` is core-pipeline's working folder. review-ui
  never reads it, and a folder not run for 7 days is deleted.
- Either service can be redeployed without the other.
- **db-utilities** is a third, separate deployment: one-off jobs that load manifests and
  ground truth into the database (§5).

---

## 2. One chart, end to end

```
POST /api/charts/run  {input_type, container_name, input_path, chart_name, output_path}
   │   returns 202 at once; the work runs in the background
   ▼
INTAKE   download pages -> outputs/charts/<chart>/pages/1.jpg ... N.jpg
         chart_list + page_list rows; page_stage_status seeded (pages x stages)
         manifest rows swept earlier are linked to the chart
   ▼
STAGES   1 ocr_quality ─ 2 ocr_prelim ─ 3 blank_junk (1) ─ 4 ocr_final1 ─ 5 ocr_final2 ($)
         ─ 6 section_headers ─ 7 kv_extract ─ 8 blank_junk (2) ─ 9 member_verify
         ─ 10 page_subtype ─ 11 dos_extract ─ 12 continuity ─ 13 encounter_type
         ─ 14 page_sequencing ─ 15 imaging_final
   ▼
FINISH   refresh_chart_status -> completed / needs_review / failed
         outputs written to Blob <output_path>/<chart>/ when an output_path is given
```

Progress: `GET /api/charts/{chart_id}` or `/api/charts/by-name/{chart_name}`.
**Stage 5 is billed per page** (Azure Document Intelligence). **Stage 9 decides
accept / reject** (it needs GLiNER: without it no page can be marked wrong-member).

---

## 3. What each stage reads and writes

The order is the `pipeline_stage` seed in `schema/schema.sql`. "Best text" means
Final OCR 2, else Final OCR 1, else the preliminary text (only on printed pages).

| # | Stage | Runs on | Reads | Writes (database) | Writes (chart folder) |
|---|---|---|---|---|---|
| 1 | `ocr_quality` | every page | page image | `ocr_quality_results` | `imaging/_rotation.csv`, `_hw_printed.csv`, `_quality.csv`; `corrected-pages/` when a page is turned or straightened |
| 2 | `ocr_prelim` | every page | corrected page if any, else the image | `ocr_results` (tesseract) | `ocr/<chart>_prelim.txt` |
| 3 | `blank_junk` pass 1 | printed pages | prelim text | `blank_junk_classification` | `imaging/_junk.csv` |
| 4 | `ocr_final1` | every page (pass-1 blank / junk / duplicate stored only) | corrected page or image | `ocr_results` (RapidOCR) | `ocr/<chart>_final1.json` |
| 5 | `ocr_final2` | survivors + handwritten | corrected page or image | `ocr_results` (Azure) | `ocr/<chart>_final2.json` |
| 6 | `section_headers` | pages with Final 1 / 2 JSON | those JSON files | headers inside `ocr_results` | rewrites the JSON files |
| 7 | `kv_extract` | pages with word boxes | Final 2 JSON, else Final 1, + image | provider signature, page details | `staging/extraction.json`, `imaging/_provider_signature.csv` |
| 8 | `blank_junk` pass 2 | handwritten + survivors | best text | final verdict (`is_final`) | rewrites `_junk.csv` |
| 9 | `member_verify` | not blank / junk | best text + `manifest_member_list` | `member_extraction_results`, `member_verification_summary` | `imaging/_member_*.csv` |
| 10 | `page_subtype` | every page | best text + blank / junk | `page_classification` | `imaging/_codeable.csv`, `staging/page_classification.json` |
| 11 | `dos_extract` | not blank / junk | best text + page type | `dos_extraction_results` | `imaging/_dos.csv` |
| 12 | `continuity` | every page | page types, dates, signatures | `page_continuity_results` | `staging/continuity.json`, `imaging/_continuity.csv` |
| 13 | `encounter_type` | not blank / junk | best text + documents | `encounter_type_results` | `imaging/_encounter.csv` |
| 14 | `page_sequencing` | every page (junk set aside) | best text + documents | `page_sequencing_results` | `imaging/_sequencing.csv` |
| 15 | `imaging_final` | every page | all of the above | `imaging_final` | `imaging/_final.csv` |

Every stage also writes a `pipeline_jobs` row and updates `page_stage_status` per page.
One bad page never sinks a chart: its error goes in `page_stage_status.error_message` and
the stage carries on.

---

## 4. Why a page drops out

```
page ──► printed? ── no (handwritten / form / blank / low quality) ──► skips pass 1
           │ yes
           ▼
      blank / junk pass 1 on the prelim text
           │ blank, junk or exact duplicate ──► stored only: no Azure, no member, no DOS
           ▼
      Final OCR 1 ──► Final OCR 2 (Azure; high-quality printed pages skip it)
           ▼
      blank / junk pass 2 on the best text
           │ blank, junk or duplicate ──► no member, no DOS, no encounter
           ▼
      member, DOS, encounter on the best text
```

A skip is recorded as `page_stage_status.status = 'skipped'` with a `skip_reason`.
A skipped page counts as done, so a chart of blank pages still completes.

**Blank / junk pass 2, in order** (full logic: [`specials/BLANK_JUNK_LOGIC.md`](specials/BLANK_JUNK_LOGIC.md)):

```
blank / tiny / placeholder ─► visual < 20 words (Main) ─► > 30% handwritten (Main)
  ─► Electronic Signature, none on the previous / next non-blank page, p(junk) < 0.95 (Main)
  ─► card (Junk / Cards) ─► Fax header, unsigned (Junk / Letter/Fax)
  ─► contents page: "Index" / "Contents" title + page numbers (Junk / Others)
  ─► short page (content in the top 20%):
        a. the page before ends in its Electronic Signature, carried on here
           (provider name / credentials or a date on this page) ...... Main
        b. "End of Report" ........................................... Junk / Cover Page
        c. < 5 distinct words not on the previous / next page ........ Junk / Others
  ─► the classifier
  ─► keeps: lab results, ICD-10 / Social History; an invoice with an amount and a date
```

---

## 5. Manifests and ground truth: a separate job (db-utilities)

```
metadata_R1_B1.csv / ground_truth.xlsx  (a file in db-utilities/input, or a Blob prefix)
   ──► docker compose run --rm db-utilities manifest ...      ──► manifest_member_list
   ──► docker compose run --rm db-utilities ground-truth ...  ──► page_ground_truth
```

`db-utilities/` is its own deployment (code, image, `.env`); core-pipeline and review-ui
never load these files. A manifest can be loaded before or after its charts run:
`record_id` is the chart name, and the member stage reads the table when it runs (no rows
= the member stage waits). `run_id` / `batch_id` come from the `R#` / `B#` in the file name.

---

## 6. Running a chart again

| Request | What happens |
|---|---|
| same `input_path` + `chart_name` | Pages already in the workspace are reused; **every stage runs again**, including OCR (Azure is billed again) |
| `"skip_ocr": true` | OCR is reused; everything else runs again. No Azure bill |
| `"only": ["dos_extract"]` | Only those stages, on what is already in the chart folder |
| `"run_through": "ocr_final2"` | Stops after that stage |
| `"skip_completed": true` | A chart already completed / needs_review is not run |
| CLI `python cli.py run --resume` | Completed pages are left alone (the API has no resume) |

`only` and `skip_ocr` need the chart folder, which is deleted 7 days after the chart's
last run. After that, run the chart in full.

---

## 7. Chart status

```
received ──► downloading ──► processing ──► completed
                                  │     └──► needs_review   (member verification unresolved)
                                  └────────► failed ──► processing (run again)
```

`chart_list.status` is the lifecycle and `current_stage` the position: the earliest
stage, in `pipeline_stage.seq` order, where not every page is completed or skipped.
Both are worked out from the page rows after every stage, never set by hand. Accept /
reject is on `member_verification_summary`, not on the chart status.

---

## 8. Where the time goes

Most of a chart's wall-clock is OCR: Tesseract on every page, RapidOCR, then Azure for
the survivors. Blank / junk pass 1 pays for itself, because every page it rules out is
a page the two final OCR stages never read, and stage 5 is the billed one.
`python cli.py usage --days 7` shows the real numbers per day: runs, pages, time and
Azure cost (`run_usage`).

---

## 9. The Service Bus queue (built 2026-11-08, off until RUN_MODE=queue)

With `RUN_MODE=inline` (the default) the API runs the stages itself, in the background of the
same process (§2). With `RUN_MODE=queue` in `core-pipeline/.env` it hands each chart to the
Service Bus queue `chart-runs` and separate workers run it
(`docker compose up -d --build --scale worker=3`; worker = `python -m apps.worker.queue_worker`):

```
 caller ──X-API-Key──► orchestrator API (1 copy, small, no models)
                           │  records the run, 1 message per chart (session = chart_id)
                           ▼
                  Service Bus queue "chart-runs"
                     │            │            │
                     ▼            ▼            ▼
                  worker 1     worker 2     worker 3      docker compose up --scale worker=3
                  1 chart      1 chart      1 chart       models loaded once per worker
                     └────────────┴─────┬──────┘
                                        ▼  each worker runs §2's 15 stages and writes
                               Postgres (Cosmos later) + Blob directly
```

- **The API stops processing charts.** It checks the key (`API_KEY_REQUIRED=true`), records
  the run, queues one message per chart and answers `202` with a `run_id`. Status:
  `GET /api/runs/{run_id}`, or `?wait=60` for a small chart.
- **Workers pull one chart at a time.** A batch spreads itself over the workers; adding a
  worker needs no code change.
- **Session = chart_id**, so the same chart never runs twice at once.
- **Crash-safe.** A dead worker's message returns after its 5-minute lock and another
  worker carries on, skipping finished stages. After 5 failed deliveries it goes to the
  dead-letter queue.
- The stage chain, the tables and review-ui do not change.

The VM side (worker count, memory): [specials/VM_GUIDE.md §3](specials/VM_GUIDE.md). The
whole plan and what is still missing: [specials/PLATFORM_PLAN.md](specials/PLATFORM_PLAN.md).
