# Chart-type clustering trainer

Design: [`docs/specials/CHART_CLUSTERING_IMPLEMENTATION.md`](../../docs/specials/CHART_CLUSTERING_IMPLEMENTATION.md).
Input: **one folder** in the same format as `core-pipeline/outputs/charts` (one subfolder per
chart, as the pipeline writes it). Only `ocr/<chart>_prelim.txt` is required per chart;
`imaging/*_final.csv` and page images are used when present. The folder is only read.

| Step | Script | Status |
|---|---|---|
| 1. Folder → one row per page, no text kept | `ingest_folder.py` | **built** |
| 2. Page skeletons (generic template words only) | `build_skeletons.py` | next |
| 3. Note-look codebook | `build_codebook.py` | planned |
| 4. Chart types (HDBSCAN) + naming sheets | `build_catalogue.py` | planned |
| 5. Scorecard | `evaluate.py` | planned |

```bash
# Mac / Ubuntu VM — from the repo root, any venv with Python 3.12
cd training/chart-clustering
python ingest_folder.py --root ../../core-pipeline/outputs/charts      # or any folder in that format
python -m pytest tests -q
```

```powershell
# Windows (PowerShell)
cd training\chart-clustering
python ingest_folder.py --root ..\..\core-pipeline\outputs\charts
python -m pytest tests -q
```

Output goes to `work/` (git-ignored): `pages.csv` and `charts.csv`. Charts still need a
pipeline run first if their folder has only page images (no `ocr/` yet): run them through
the pipeline, then point `--root` at `core-pipeline/outputs/charts`, which keeps a chart folder
for 7 days after its last run.
