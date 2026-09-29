# Utilities

Standalone helpers that use the **core-pipeline** venv and `DATABASE_URL`
from `core-pipeline/.env`. There is no separate utilities requirements file.

```bash
cd core-pipeline
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1

python ../utilities/load_metadata_manifests.py
python ../utilities/load_metadata_manifests.py /path/to/metadata/
python ../utilities/load_metadata_manifests.py /path/to/metadata_R2_B1.csv
python ../utilities/load_metadata_manifests.py /path/to/metadata/ --run-id R9 --batch-id B1
```

## Load metadata manifests → Postgres

Reads every `metadata_Rn_Bn.csv` / `.xlsx` under a folder (or one file) and
upserts into `manifest_member_list`. Run/batch ids are parsed from the
filename (`metadata_R1_B1.csv` → `R1` / `B1`).

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

Same job via the API / sweeper:

```bash
python -m jobs.manifest_sweeper --local ../review-ui/data/metadata/

curl -X POST localhost:8001/api/manifest/sweep -H 'Content-Type: application/json' \
  -d '{"local_path":"/path/to/metadata"}'
```
