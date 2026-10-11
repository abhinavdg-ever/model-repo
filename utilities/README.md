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
review-ui/data/folders/<pdf name>/pages/1.jpg
review-ui/data/folders/<pdf name>/pages/2.jpg
```

Needs PyMuPDF in the core-pipeline venv: `pip install pymupdf`.

```bash
python ../utilities/pdfs_to_folders.py /path/to/chart-a.pdf /path/to/chart-b.pdf
python ../utilities/pdfs_to_folders.py /path/to/pdfs/
python ../utilities/pdfs_to_folders.py --list paths.txt
python ../utilities/pdfs_to_folders.py --out /path/to/folders chart.pdf
```

`paths.txt` is one local PDF path per line. A directory argument is every
PDF in that folder. `--out` defaults to `review-ui/data/folders`.

Same job via the API / sweeper:

```bash
python -m jobs.manifest_sweeper --local ../review-ui/data/metadata/

curl -X POST localhost:8001/api/manifest/sweep -H 'Content-Type: application/json' \
  -d '{"local_path":"/path/to/metadata"}'
```
