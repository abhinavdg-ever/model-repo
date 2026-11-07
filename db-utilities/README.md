# db-utilities

One-off jobs that load reference data into the shared database. A separate deployment:
its own code, Docker image and `.env`. It is not part of core-pipeline or review-ui and
shares only the tables (`schema/schema.sql`).

| Job | Loads | Into |
|---|---|---|
| `manifest` | batch manifests (`metadata_R1_B1.csv`, .xlsx): a file, a folder, or a Blob prefix | `manifest_member_list` (upsert per record id + member) |
| `metadata-extra` | fills the batch CSVs from the client's `metadata_extra.csv` dump; `--load` then loads them | the CSV files, then `manifest_member_list` |
| `ground-truth` | the client's page ground truth (CSV / XLSX, a file or folder) | `page_ground_truth` (+ a `bulk_upload` entry per page) |
| `sequence` | only the Sequence column of a ground-truth sheet | `page_ground_truth.page_sequence` |

Every job upserts, so running it again is safe. A manifest can be loaded before or after
its charts run: `record_id` is the chart name, and member verification reads the table.

## Quick load: one command (Postgres and Cosmos)

```bash
# Mac / Ubuntu VM
./load.sh manifest     /path/to/metadata_R1_B1.csv
./load.sh ground-truth /path/to/ground_truth.csv
./load.sh manifest     /path/to/metadata_R1_B1.csv --to postgres     # or --to cosmos
```

```powershell
# Windows (PowerShell)
.\load.ps1 manifest     C:\data\metadata_R1_B1.csv
.\load.ps1 ground-truth C:\data\ground_truth.csv
.\load.ps1 manifest     C:\data\metadata_R1_B1.csv -To postgres
```

- Writes to **both Postgres and Cosmos** by default (`--to` / `-To` picks one). Without it,
  `DB_BACKEND` in `.env` decides.
- A file, an `.xlsx`, or a folder of them. With Docker running it copies the file into
  `./input` and uses the image; otherwise it uses `.venv` (made on first use).
- Column templates: `templates/manifest_template.csv` (one row per member: `recordId` is the
  chart name) and `templates/ground_truth_template.csv` (one row per page: `Chart_Name`, `Id` =
  page number or file stem). Only the columns a sheet has are written.
- `.env` needs `DATABASE_URL` and the `COSMOS_*` values (the same as `core-pipeline/.env`).

## Docker (the VM)

```bash
# Ubuntu VM, once
cd ~/advantmed-imaging-pipeline/db-utilities
cp .env.example .env            # DATABASE_URL and AZURE_STORAGE_* as in core-pipeline/.env
docker compose build

# each load: put the file in ./input (read-only at /input), then
docker compose run --rm db-utilities manifest --local /input/metadata_R1_B1.csv
docker compose run --rm db-utilities manifest --local /input/                 # every file in the folder
docker compose run --rm db-utilities manifest --blob-prefix Manifests/R1/     # straight from Blob
docker compose run --rm db-utilities metadata-extra --metadata-dir /input --load
docker compose run --rm db-utilities ground-truth --local /input/ground_truth.xlsx
docker compose run --rm db-utilities sequence /input/ground_truth.csv --dry-run
```

`/input` is read-only, so `metadata-extra` without `--dry-run` needs a writable mount:
`docker compose run --rm -v "$PWD/input:/input" db-utilities metadata-extra --metadata-dir /input --load`.

```powershell
# Windows (PowerShell), Docker Desktop
cd db-utilities
Copy-Item .env.example .env
docker compose build
docker compose run --rm db-utilities manifest --local /input/metadata_R1_B1.csv
docker compose run --rm db-utilities ground-truth --local /input/ground_truth.xlsx
```

## Without Docker (Mac)

```bash
cd db-utilities
python3.12 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
cp .env.example .env
python -m dbutil manifest --local "/path/to/metadata_R1_B1.csv"
python -m dbutil ground-truth --local "/path/to/ground_truth.xlsx"
python -m dbutil                                    # lists the jobs
```

Each job prints a JSON summary: files read, rows inserted / updated / skipped, charts.
