# Utilities

Standalone helpers that use the **core-pipeline** venv and `DATABASE_URL`
from `core-pipeline/.env`. There is no separate utilities requirements file.

Loading manifests (`metadata_R1_B1.csv`, `metadata_extra.csv`) and ground truth (the full
sheet or only its Sequence column) is **not** here any more: it is its own job,
[`db-utilities/`](../db-utilities/README.md) (2026-11-07), run with Docker:

```bash
cd db-utilities
docker compose run --rm db-utilities manifest --local /input/metadata_R1_B1.csv
docker compose run --rm db-utilities ground-truth --local /input/ground_truth.xlsx
```

## Pull the client's ground truth into ours

`pull_client_ground_truth.py` (2026-11-08) copies the ground truth the client entered in their
review database (review-ui-client) into ours. Six tables, all by chart name: `page_ground_truth`
(+ `page_ground_truth_entry`), `chart_ground_truth`, `encounter_review`, `chart_review`
(+ `chart_review_entry`). For every chart the client has ground truth on, ours is replaced by
theirs; other charts keep ours. `--apply` first backs up all six tables (schema
`gt_backup_<stamp>` in our database + CSVs in `utilities/output/gt_backup_<stamp>/`) and
copies in one transaction. `--restore <schema>` undoes it. A dry run is the default.

```bash
# Ubuntu VM
cd ~/advantmed-imaging-pipeline/core-pipeline && source .venv/bin/activate
export CLIENT_DATABASE_URL='postgresql://...client db...'     # DATABASE_URL comes from .env
python ../utilities/pull_client_ground_truth.py                 # dry run: per-table counts
python ../utilities/pull_client_ground_truth.py --apply         # back up, then copy
python ../utilities/pull_client_ground_truth.py --list-backups
python ../utilities/pull_client_ground_truth.py --restore gt_backup_20261108_093000
```

```powershell
# Windows (PowerShell)
cd core-pipeline; .venv\Scripts\Activate.ps1
$env:CLIENT_DATABASE_URL = "postgresql://...client db..."
python ..\utilities\pull_client_ground_truth.py
python ..\utilities\pull_client_ground_truth.py --apply
```

`--charts-file charts.txt` limits it to those charts. `--add-users` creates the client's
accounts we lack (inactive, no email) so their saves keep a name in the Review Log;
without it those rows keep no user.

## Restore the sheet's Page Type (Accept / Unaccept)

The client sheet's Page Type column holds Accept / Unaccept (its page type is in Encounter
Type). Before 2026-10-29 a review-ui Page Details save overwrote it with the page type.
`restore_sheet_page_type.py` puts back each such page's value from its latest spreadsheet
load (page_ground_truth_entry). Only `page_type` is written; encounter_type, the history and
the Review Log are untouched. Preview by default; safe to run again.

```bash
cd core-pipeline && source .venv/bin/activate
python ../utilities/restore_sheet_page_type.py                                  # preview
python ../utilities/restore_sheet_page_type.py --apply                          # write
python ../utilities/restore_sheet_page_type.py --database-url "$OTHER_DB_URL"   # another database
python ../utilities/restore_sheet_page_type.py --sql > restore.sql              # for psql
```

## Find chart folders under a blob prefix

Two inputs: the blob location (`container/prefix`) and a local Excel file of
folder names. Walks Run / Batch / DEID subfolders under that prefix and
appends a `found_location` column. The sheet must have a `Folder Name`
header. Prints `processing <folder>` and saves the same xlsx every 10 finds.
Does not list page files. Azure auth comes from `core-pipeline/.env`.

```bash
python ../utilities/find_blob_folders.py imaging-pipeline/Raw_Input folders.xlsx
python ../utilities/find_blob_folders.py imaging-pipeline/Raw_Input C:/lists/folders.xlsx --column "Folder Name"
```

Writes `folders_located.xlsx` next to the input. A folder in more than one
place is several paths separated by ` | `. A miss is `NOT FOUND`.

## Merge training files

Combines several `training_data.jsonl` files from the annotation tool into one.
Standard library only — runs with any Python 3.9+, no venv needed. Prints
counts and file names only, never page text.

```bash
python utilities/merge_training_json.py \
    old/training_data.jsonl ~/Desktop/Training/processed/training_data.jsonl \
    --out training/bert-training/data/training_data.jsonl

python utilities/merge_training_json.py ~/Desktop/Training/ --out merged.jsonl   # every *.jsonl under a folder
python utilities/merge_training_json.py --list files.txt --out merged.jsonl
python utilities/merge_training_json.py a.jsonl b.jsonl --out merged.jsonl --dry-run   # report only
```

* **List the newest file last.** The same page (same image name and file size)
  in more than one file is kept once, with the **later** file's labels.
* Same image name but a different file (e.g. `IMG_1234.HEIC` in two folders):
  both kept, listed in the report.
* Identical text under different names (likely the same page twice, which
  would leak across the train / validation split): reported;
  `--drop-duplicate-text` keeps only the first.
* Records without `image`, `text` or `model_type` are dropped; model types not
  in `page_taxonomy.json` are counted.
* `--out` may be one of the inputs; it is written only after every input is read.

## Local PDFs → page folders

One chart folder per PDF on disk, with a JPEG for each page:

```
core-pipeline/outputs/charts/<pdf name>/pages/1.jpg
core-pipeline/outputs/charts/<pdf name>/pages/2.jpg
```

Needs PyMuPDF in the core-pipeline venv: `pip install pymupdf`.

```bash
python ../utilities/pdfs_to_folders.py /path/to/chart-a.pdf /path/to/chart-b.pdf
python ../utilities/pdfs_to_folders.py /path/to/pdfs/
python ../utilities/pdfs_to_folders.py --list paths.txt
python ../utilities/pdfs_to_folders.py --out /path/to/folders chart.pdf
```

`paths.txt` is one local PDF path per line. A directory argument is every
PDF in that folder. `--out` defaults to `core-pipeline/outputs/charts`.

## Harvest extraction keys and date labels

`harvest_keys.py` scans `core-pipeline/outputs/charts/*` (standard library only, no venv):

```bash
python utilities/harvest_keys.py                 # DOS keys -> utilities/output/keys/
python utilities/harvest_keys.py --field provider  # provider_name, provider_credentials, electronic_signature
python utilities/harvest_keys.py --field member    # staging name, dob, member_id (+ member_labels.csv)
python utilities/harvest_keys.py --field all     # every staged field
```

- `keys_rows.csv` — every staged key/value pair, with `status`: `accepted`, `no_value` (key found, no value read — a failed pair), `rejected`, `printed`.
- `keys_summary.csv` — per key and tier: found / accepted / failed counts and examples.
- `date_labels.csv` — from the OCR text: the words before every date, counted; `known_tier` is blank when no key in `extraction/dos/keys.json` covers the label.
- `provider_labels.csv` (provider fields) — from the OCR text: the label in front of every credentialed name (`Entered By:`, `Filed by`, `Ordering provider:`), counted; `known_key` is blank when neither `provider_name` nor `electronic_signature` keys.json covers it.
- `member_labels.csv` (member fields) — from the OCR text, driven by the manifest like the member stage: where the chart's expected name (last name required), DOB (any printed format) or Member ID appears, the words in front of it (`Patient Name:`, `DOB:`, `MRN`), counted per field; `(no label)` = keyless; `known_key` is blank when that field's keys.json does not cover it. Expected members come from `--metadata` (default `core-pipeline/outputs/metadata`, `metadata_R*_B*.csv`).

`keys_rows.csv` columns follow the field: DOS rows carry `dos_from` / `dos_to`, provider rows `profile` / `member_hint`, e-signature rows `provider_name` / `designation` / `signature_date` (its value is the name, else the date).

## Tag charts → `chart_list.tags` (2026-11-07)

`tag_chart.py` adds or removes free-text tags on charts. They are stored as a JSON array in
`chart_list.tags`, and review-ui's **History** and **Accuracy** offer them as the **Tag** filter
(a chart passes with any picked tag; `(Blank)` = no tag). Name a chart by its `chart_list` id or
its chart name. Tagging leaves the chart's Last Run At alone. The column is added on first use
(or run `schema/migrations/2026-11-07.sql`).

```bash
python ../utilities/tag_chart.py 1234 --tag "Pilot"
python ../utilities/tag_chart.py 1234 1235 52743839_44976074 --tag Pilot --tag Recheck
python ../utilities/tag_chart.py --from-file charts.txt --tag Pilot     # one chart per line
python ../utilities/tag_chart.py 1234 --remove Pilot                    # or --clear
python ../utilities/tag_chart.py 1234                                   # show its tags
python ../utilities/tag_chart.py --list-tags                            # every tag, with counts
```
