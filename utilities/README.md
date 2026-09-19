# Utilities

Standalone helpers that sit beside the two services. They use
`core-pipeline/` code and `DATABASE_URL` from `core-pipeline/.env`.

## Load metadata manifests → Postgres

Reads every `metadata_Rn_Bn.csv` / `.xlsx` under a folder (or one file) and
upserts into `manifest_member_list`. Run/batch ids are parsed from the
filename (`metadata_R1_B1.csv` → `R1` / `B1`).

```bash
# Default folder: review-ui/data/metadata/
python utilities/load_metadata_manifests.py

python utilities/load_metadata_manifests.py /path/to/metadata/
python utilities/load_metadata_manifests.py /path/to/metadata_R2_B1.csv
python utilities/load_metadata_manifests.py /path/to/metadata/ --run-id R9 --batch-id B1
```

Same job via the API / existing sweeper:

```bash
cd core-pipeline
python -m jobs.manifest_sweeper --local ../review-ui/data/metadata/
# or
curl -X POST localhost:8001/api/manifest/sweep -H 'Content-Type: application/json' \
  -d '{"local_path":"/path/to/metadata"}'
```
