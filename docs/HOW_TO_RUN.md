# How to Run (and re-run without OCR)

Practical recipes for charts that **already finished OCR** (e.g. hundreds of
docs with `ocr/` + `ocr_results` in the workspace) when you only want later
stages — codeable, encounter, sequencing, DOS, member, section headers — or a
selective re-pass.

All examples below use **Azure Blob** paths. Local paths work the same way if
you swap in `local_read_path` / `local_folder_name` / `local_write_path`
(see [`API.md`](API.md)).

Full API reference: [`API.md`](API.md). Stage flow: [`FLOW.md`](FLOW.md).

**Blob field cheat-sheet**

| Field | Meaning | Example |
|---|---|---|
| `blob_container` | Storage container | `"imaging-pipeline"` |
| `blob_read_path` | Parent prefix that holds chart folders | `"Raw_Input/Run1/Batch1/DEID_Images"` or `"run1/batch1"` |
| `blob_read_folder_name` | One chart folder (= chart name) | `"52743839_44976074"` |
| `blob_write_path` | Destination prefix for Processed output | `"Processed/Run1/Batch1"` |

Batch-run uses the same container + read/write paths **without** a folder name
(each subfolder under `blob_read_path` is one chart).

---

## 0. One-time: bring the DB up to date

If these charts ran on an **older** schema (before codeable / encounter /
sequencing), apply the additive patch first — do **not** re-run `v1.sql` on a
live DB:

```bash
psql "$DATABASE_URL" -f schema/patch_output_path.sql
```

That creates `encounter_type_results` + `page_sequencing_results`, registers
the three new stages, and seeds `page_stage_status` pending rows.

First-time empty database instead:

```bash
psql "$DATABASE_URL" -f schema/v1.sql
# optional: psql "$DATABASE_URL" -f schema/v2.sql
```

Confirm stages (and that blob credentials are live):

```bash
curl -fsS localhost:8001/ready
curl -fsS localhost:8001/health | python -m json.tool   # blob.ready should be true
curl -fsS localhost:8001/api/stages | python -m json.tool
```

---

## 1. Stage names (chain order)

| Token | What it does | Bills Azure DI? |
|---|---|---|
| `ocr_quality` | Rotation + handwriting | No |
| `ocr_prelim` | Tesseract | No |
| `blank_junk` / `blank_junk:2` | Junk pass 1 / pass 2 | No |
| `ocr_final1` | RapidOCR / Docling | No |
| `ocr_final2` | Azure Document Intelligence | **Yes — per page** |
| `section_headers` | Canon match on OCR JSON | No |
| `member_verify` | Member extract + verify | No |
| `dos_extract` | Date of service | No (LLM optional) |
| `page_subtype` | Codeable / Non-Codeable / Discharge Frequency | No |
| `encounter_type` | Outpatient F2F / Tele / Inpatient / Home | No |
| `page_sequencing` | Suggested page order | No |

---

## 2. The three knobs

| Field | Default | Use when |
|---|---|---|
| `force` | **`true`** | Reprocess stages even if already `completed`. **Re-bills Final2.** Does **not** wipe `pages/`. |
| `force: false` | — | **Resume** incomplete work. Required for `skip_ocr` to take effect. |
| `only: ["stage", …]` | omit | Run **just** those stages (quality still runs under `skip_ocr`). |
| `skip_ocr: true` | env `SKIP_OCR` | See § skip_ocr below. Ignored when `force: true`. |
| `redownload_pages: true` | `false` | Wipe `pages/` + `corrected-pages/` and re-fetch pages from Raw_Input. |
| `through: "stage"` | omit | Run from the top of the chain and **stop after** that stage. |

### What `skip_ocr` does

Looks under `data/folders/<chart>/` (review-ui workspace):

1. **`pages/` present** → use it. **Missing** → download from **Raw_Input** (`blob_path`).
2. **`ocr/` present** → use it. **Missing** → pull from **Processed** (`output_path`). **Still missing** → materialize from Postgres `ocr_results`. **Still missing** → **re-run OCR engines**.
3. **Quality + rotation always re-run** → rewrite `corrected-pages/`. Gate-delta may reopen OCR only for pages whose HW/quality/rotation path flipped.
4. **Write** (if a write path is set) **overwrites** destination `ocr/`, `corrected-pages/`, `imaging/` (default write mode still omits `pages/`).

```bash
curl -X POST localhost:8001/api/charts/run -H 'Content-Type: application/json' \
  -d '{"chart_id": 123, "skip_ocr": true, "force": false,
       "blob_write_path": "Processed/Run1/Batch1"}'
```

Rule of thumb for a large blob batch that already finished OCR:

- Want new classifiers only → **`only`** + `chart_id` / `chart_name` (no re-download), optional `blob_write_path` to sync CSVs out.
- Want “everything after OCR again” → **`only`** listing post-OCR stages (§5A).
- Never set `force: true` on a full chain (no `only`) unless you intend to pay for Final2 again.

Rule of thumb for a large blob batch that already finished OCR:

- Want new classifiers only → **`only`** + `chart_id` / `chart_name` (no re-download), optional `blob_write_path` to sync CSVs out.
- Want “everything after OCR again” → **`only`** listing post-OCR stages (§5A).
- Never set `force: true` on a full chain (no `only`) unless you intend to pay for Final2 again.

---

## 3. Fresh chart from blob (full pipeline)

```bash
curl -X POST localhost:8001/api/charts/run -H 'Content-Type: application/json' \
  -d '{
    "blob_container": "imaging-pipeline",
    "blob_read_path": "Raw_Input/Run1/Batch1/DEID_Images",
    "blob_read_folder_name": "52743839_44976074",
    "blob_write_path": "Processed/Run1/Batch1",
    "run_id": "R1",
    "batch_id": "B1"
  }'
```

CLI:

```bash
cd core-pipeline && source .venv/bin/activate
python cli.py run \
  --blob-container imaging-pipeline \
  --blob-read-path Raw_Input/Run1/Batch1/DEID_Images \
  --blob-read-folder-name 52743839_44976074 \
  --blob-write-path Processed/Run1/Batch1 \
  --run-id R1 --batch-id B1
```

Stop before Azure OCR (no Final2 bill):

```bash
curl -X POST localhost:8001/api/charts/run -H 'Content-Type: application/json' \
  -d '{
    "blob_container": "imaging-pipeline",
    "blob_read_path": "Raw_Input/Run1/Batch1/DEID_Images",
    "blob_read_folder_name": "52743839_44976074",
    "blob_write_path": "Processed/Run1/Batch1",
    "through": "ocr_final1"
  }'
```

---

## 4. Your case: OCR already done — run the new stages

Charts already exist in Postgres (`chart_id` / `chart_name`). No need to
re-download pages from blob unless you also want a write sync.

### One chart — codeable + encounter + sequencing

```bash
curl -X POST localhost:8001/api/charts/run -H 'Content-Type: application/json' \
  -d '{
    "chart_id": 123,
    "force": true,
    "only": ["page_subtype", "encounter_type", "page_sequencing"],
    "blob_write_path": "Processed/Run1/Batch1"
  }'
```

Or by name:

```bash
curl -X POST localhost:8001/api/charts/run -H 'Content-Type: application/json' \
  -d '{
    "chart_name": "52743839_44976074",
    "force": true,
    "only": ["page_subtype", "encounter_type", "page_sequencing"],
    "blob_write_path": "Processed/Run1/Batch1"
  }'
```

CLI:

```bash
python cli.py run --chart-id 123 --force \
  --only page_subtype --only encounter_type --only page_sequencing \
  --blob-write-path Processed/Run1/Batch1
```

`force: true` here only re-runs those stages (not OCR), because `only` limits
the chain. Outputs (workspace + optional blob write):

- `imaging/<chart>_codeable.csv`
- `imaging/<chart>_encounter.csv` + `encounter_type_results`
- `imaging/<chart>_sequencing.csv` + `page_sequencing_results`

### One chart — also refresh DOS first (encounter needs DOS)

```bash
curl -X POST localhost:8001/api/charts/run -H 'Content-Type: application/json' \
  -d '{
    "chart_name": "52743839_44976074",
    "force": true,
    "only": ["dos_extract", "page_subtype", "encounter_type", "page_sequencing"],
    "blob_write_path": "Processed/Run1/Batch1"
  }'
```

### Batch — same stages on every chart under a blob prefix

Use the **same** `blob_container` + `blob_read_path` you ingested from. With
`only` set, OCR stages are not in the chain (so Final2 is not re-billed) even
when `force: true`:

```bash
curl -X POST localhost:8001/api/charts/batch-run -H 'Content-Type: application/json' \
  -d '{
    "blob_container": "imaging-pipeline",
    "blob_read_path": "Raw_Input/Run1/Batch1/DEID_Images",
    "blob_write_path": "Processed/Run1/Batch1",
    "force": true,
    "only": ["page_subtype", "encounter_type", "page_sequencing"],
    "workers": 2
  }'
```

CLI:

```bash
python cli.py batch-run \
  --blob-container imaging-pipeline \
  --blob-read-path Raw_Input/Run1/Batch1/DEID_Images \
  --blob-write-path Processed/Run1/Batch1 \
  --force \
  --only page_subtype --only encounter_type --only page_sequencing \
  --workers 2
```

Poll: `GET /api/charts/{id}` or core-pipeline logs / `progress.txt` under the chart.

---

## 5. Re-run one component only

| Goal | `only` |
|---|---|
| Section headers (after editing `section_header_canon.json`) | `["section_headers"]` |
| Blank/junk pass 2 | `["blank_junk:2"]` |
| Member verification (e.g. after enabling NER) | `["member_verify"]` |
| DOS only | `["dos_extract"]` |
| Codeable only | `["page_subtype"]` |
| Encounter only | `["encounter_type"]` |
| Sequencing only | `["page_sequencing"]` |

```bash
curl -X POST localhost:8001/api/charts/run -H 'Content-Type: application/json' \
  -d '{
    "chart_id": 123,
    "force": true,
    "only": ["section_headers"],
    "blob_write_path": "Processed/Run1/Batch1"
  }'
```

---

## 6. Skip OCR but re-run the rest of the chain

### A. Explicit post-OCR list (clearest — no Final2)

```bash
curl -X POST localhost:8001/api/charts/run -H 'Content-Type: application/json' \
  -d '{
    "chart_id": 123,
    "force": true,
    "only": [
      "section_headers",
      "blank_junk:2",
      "member_verify",
      "dos_extract",
      "page_subtype",
      "encounter_type",
      "page_sequencing"
    ],
    "blob_write_path": "Processed/Run1/Batch1"
  }'
```

Batch equivalent:

```bash
curl -X POST localhost:8001/api/charts/batch-run -H 'Content-Type: application/json' \
  -d '{
    "blob_container": "imaging-pipeline",
    "blob_read_path": "Raw_Input/Run1/Batch1/DEID_Images",
    "blob_write_path": "Processed/Run1/Batch1",
    "force": true,
    "only": [
      "section_headers",
      "blank_junk:2",
      "member_verify",
      "dos_extract",
      "page_subtype",
      "encounter_type",
      "page_sequencing"
    ],
    "workers": 2
  }'
```

### B. Adaptive `skip_ocr` + resume (gate-delta)

Reuses OCR on disk; refreshes quality; only re-opens OCR engines for pages
whose HW/quality/rotation **gate** changed:

```bash
curl -X POST localhost:8001/api/charts/run -H 'Content-Type: application/json' \
  -d '{
    "chart_id": 123,
    "skip_ocr": true,
    "force": false,
    "blob_write_path": "Processed/Run1/Batch1"
  }'
```

Most completed pages stay skipped. Final2 may still bill for pages that leave
the `high+printed` skip path. Prefer §6A when you want **zero** OCR billing.

---

## 7. Resume a half-finished chart / batch (do not re-OCR completed pages)

```bash
curl -X POST localhost:8001/api/charts/run -H 'Content-Type: application/json' \
  -d '{
    "chart_id": 123,
    "force": false,
    "blob_write_path": "Processed/Run1/Batch1"
  }'

python cli.py run --chart-id 123 --resume \
  --blob-write-path Processed/Run1/Batch1
```

Batch after a timeout — **same blob read path**, `force: false`:

```bash
curl -X POST localhost:8001/api/charts/batch-run -H 'Content-Type: application/json' \
  -d '{
    "blob_container": "imaging-pipeline",
    "blob_read_path": "Raw_Input/Run1/Batch1/DEID_Images",
    "blob_write_path": "Processed/Run1/Batch1",
    "force": false,
    "workers": 2
  }'

python cli.py batch-run \
  --blob-container imaging-pipeline \
  --blob-read-path Raw_Input/Run1/Batch1/DEID_Images \
  --blob-write-path Processed/Run1/Batch1 \
  --resume --workers 2
```

---

## 8. What “force” does to your wallet

| Call | Final2 (Azure DI) |
|---|---|
| `only` list with **no** `ocr_final2` | Not billed |
| `force: true` full chain / no `only` | **Re-bills every page** |
| `force: false` resume | Bills only pages still pending Final2 |
| `skip_ocr: true` + `force: false` | Usually none; rare pages if gate-delta reopens them |

---

## 9. Write path / review-ui

`blob_write_path` syncs results under that prefix (chart folder appended if
missing). Workspace CSVs under each chart’s `imaging/` are what Local Mode
review-ui overlays (`*_codeable.csv`, `*_encounter.csv`, `*_sequencing.csv`, …).
Production Mode reads Postgres (`encounter_type_results`,
`page_sequencing_results`, …).

---

## 10. Quick checklist for “500 docs on blob, OCR done, add new classifiers”

1. `psql … -f schema/patch_output_path.sql`
2. Confirm `GET /ready` and `blob.ready` on `GET /health`
3. Batch:

```bash
curl -X POST localhost:8001/api/charts/batch-run -H 'Content-Type: application/json' \
  -d '{
    "blob_container": "imaging-pipeline",
    "blob_read_path": "Raw_Input/Run1/Batch1/DEID_Images",
    "blob_write_path": "Processed/Run1/Batch1",
    "force": true,
    "only": ["page_subtype", "encounter_type", "page_sequencing"],
    "workers": 2
  }'
```

4. Review-ui → Codeable / Encounter Type / Actual Sequence populate from the
   new CSVs (or DB in production mode)
