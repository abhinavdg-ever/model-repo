# chart-clustering

Unsupervised grouping of charts (about 550) into clusters `C01`, `C02` … plus `Others`, from each
chart's anchor pages: their **text** (Tesseract template words + layout) and their **look**
(page images from Blob Raw_Input), fused by default. Standalone: it reads what the imaging
pipeline wrote (Postgres, Blob) and imports none of its code. How it works: [METHODOLOGY.md](METHODOLOGY.md).

**Default (2026-11-17):** `--track mix` (text + visual, `--visual-weight 0.5`), clustered with
HDBSCAN, which decides how many clusters there are (no forced count).

| `--track` | Clusters on |
|---|---|
| `mix` (default) | text + visual, fused |
| `text` | text only (no images downloaded) |
| `visual` | page images only |
| `both` | text and visual run apart: `out/text`, `out/visual` |

No page text, names, DOBs or member ids are written anywhere: only generic template words,
numbers and chart ids.

---

## 1. Set up (once)

Windows Command Prompt, from the folder above this one:

```
cd chart-clustering
py -3.12 -m venv .venv
.venv\Scripts\activate.bat
pip install -r requirements.txt
copy .env.example .env
```

Ubuntu VM:

```
cd chart-clustering
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env` (ask a teammate for the values; `.env` is never committed). The names are
core-pipeline's:

| Key | For |
|---|---|
| `DATABASE_URL` | `--source db`: Tesseract text, page labels, each chart's Raw_Input folder |
| `AZURE_STORAGE_AUTH` | `entra_interactive` on a laptop (managed identity → `az login` → a browser prompt once), `managed_identity` on the VM, `key` with `AZURE_STORAGE_ACCOUNT_KEY` |
| `AZURE_STORAGE_ACCOUNT_NAME` | the storage account |
| `AZURE_STORAGE_CONTAINER` | `imaging-pipeline` when `chart_list.blob_container` is empty |
| `AZURE_CLIENT_ID` | a user-assigned managed identity (same as core-pipeline) |
| `AZURE_BLOB_CONNECTION_POOL_SIZE` | parallel image downloads, e.g. `32` |

The browser sign-in is shared with core-pipeline (`%USERPROFILE%\.advantmed\` on Windows,
`~/.advantmed/` on Ubuntu), so a machine that signed in for core-pipeline is not asked again.
Optional: `az login` before the first visual or mix run.

## 2. The chart list

`charts.txt`: one chart name per line (blank lines and `#` comments ignored). It is git-ignored
(chart names stay out of git); keep it in this folder. Charts may come from any runs and batches:
`--source db` looks each one up by name in `chart_list`.

## 3. Run

Windows Command Prompt, from `chart-clustering` with the venv active:

```
python -m chart_clustering --source db --chart-list charts.txt --out out\run_mix --image-cache out\image_cache
```

Ubuntu VM:

```
python -m chart_clustering --source db --chart-list charts.txt --out out/run_mix --image-cache out/image_cache
```

The first mix or visual run downloads the anchor pages' images into `--image-cache`. Re-runs
reuse the cache and are much faster. Keep one cache for all runs.

Variants (Command Prompt shown; on Ubuntu use `/` in the paths):

```
REM text only (no images downloaded) / visual only / both apart
python -m chart_clustering --source db --chart-list charts.txt --out out\run_text --track text
python -m chart_clustering --source db --chart-list charts.txt --out out\run_visual --track visual --image-cache out\image_cache
python -m chart_clustering --source db --chart-list charts.txt --out out\run_both --track both --image-cache out\image_cache

REM quick visual smoke test: one Progress Note first page per chart
python -m chart_clustering --source db --chart-list charts.txt --out out\smoke --track visual --notes 1 --image-cache out\image_cache

REM write the labels to chart_list.chart_cluster for review-ui's Cluster filter
python -m chart_clustering --source db --chart-list charts.txt --out out\run_mix --image-cache out\image_cache --write-db
```

`--write-db` writes the run's labels; with `--track both` the text ones.

### Where the page images come from

From Postgres `chart_list`:

| Column | Role |
|---|---|
| `blob_container` | usually `imaging-pipeline` (else `AZURE_STORAGE_CONTAINER`) |
| `blob_path` | the **Raw_Input** chart folder, e.g. `Raw_Input/Demotest/DEID_Images/<chart>` or `Raw_Input/Run2/Batch3/DEID_PNGs/<chart>` |
| `output_path` | the Processed folder; used only with `--processed-fallback` |

Order: `{blob_path}/{page name}` → a listing of the Raw_Input folder (sub-folders too) →
with `--processed-fallback`, `{output_path}/<chart>/pages/{page name}`. If downloads fail,
check `AZURE_STORAGE_AUTH`, and that `blob_path` points at the Raw_Input `DEID_Images` /
`DEID_PNGs` folder, not Processed.

### Other sources

```
REM text from Blob chart output folders (no images: use --track text)
python -m chart_clustering --source blob --container imaging-pipeline --chart-list charts.txt --paths-from-db --track text --out out\run1
python -m chart_clustering --source blob --container imaging-pipeline --prefix Processed/Run1 --prefix Processed/Run2 --track text --out out\run1

REM a local folder (core-pipeline/outputs/charts shape; images from <chart>\pages\ when present)
python -m chart_clustering --source folder --root ..\core-pipeline\outputs\charts --out out\run1
```

Tuning options: `--visual-weight 0.5`, `--notes 3`, `--anchors first_pn|last|both`,
`--min-cluster-size 5` (the fewest charts a cluster may have, **not** the number of clusters; do
not shrink it to get more clusters: that once gave 121 micro-clusters), `--min-samples 3`,
`--min-charts 3`, `--layout-weight 0.35`, `--stability-runs 5`, `--seed 42`, `--image-workers N`.

## 4. Read the result

| File | Use |
|---|---|
| `run_manifest.json` | track, settings, cluster sizes, silhouette, stability (target ≥ 0.8), images found |
| `cluster_cards.md` | for a person: name each cluster |
| `chart_clusters.csv` | every chart's cluster and membership |
| `cluster_summary.csv` | per-cluster aggregates |
| `features.npz` | the clustered matrix (plots, re-use) |
| `missing_charts.txt` | listed charts not found (if any) |

## 5. Load a CSV into chart_list later

This uses `utilities/load_chart_clusters.py` in the imaging-pipeline repo (commands from its root).
Any CSV with `chart_name` and `cluster` columns (e.g. after renaming C01 to a real name) loads
into `chart_list.chart_cluster` (schema/migrations/2026-11-15.sql; the loader adds the column if
missing). Last Run At is kept.

Windows Command Prompt:

```
cd core-pipeline && .venv\Scripts\activate.bat
python ..\utilities\load_chart_clusters.py ..\chart-clustering\out\run_mix\chart_clusters.csv --dry-run
python ..\utilities\load_chart_clusters.py ..\chart-clustering\out\run_mix\chart_clusters.csv
```

Ubuntu VM:

```
cd core-pipeline && source .venv/bin/activate
python ../utilities/load_chart_clusters.py ../chart-clustering/out/run_mix/chart_clusters.csv --dry-run
python ../utilities/load_chart_clusters.py ../chart-clustering/out/run_mix/chart_clusters.csv
```

Cluster numbers are per run: a new run can renumber them, so reload after each run you keep.

## 6. Layout

```
chart_clustering/
  cli.py       the command line (python -m chart_clustering)
  sources.py   db / blob / folder readers -> Chart; Blob sign-in (core-pipeline's modes)
  ingest.py    Chart and Page; the prelim.txt split; final.csv labels
  anchors.py   the Progress Notes' first pages, the last page
  features.py  template words, layout numbers
  images.py    anchor-page images from Raw_Input into the cache
  visual.py    the page-image fingerprint
  cluster.py   text / visual matrices, mix fuse, HDBSCAN, Others, silhouette, stability
  report.py    the output files
  db_write.py  --write-db (chart_list.chart_cluster)
  vocab.py     the generic words a card may show
```

Tests (from this folder, venv active): `pip install pytest`, then `python -m pytest tests -q`.
No database, Blob or images needed: the tests build synthetic charts and pages.

## 7. First run checklist

1. [ ] venv + `pip install -r requirements.txt`
2. [ ] `.env` filled in (database, storage account, `AZURE_STORAGE_AUTH`)
3. [ ] `az login` if needed
4. [ ] `charts.txt` in this folder
5. [ ] the mix run above, with `--image-cache`
6. [ ] open `out/run_mix/cluster_cards.md` and name the clusters
7. [ ] optional: `--write-db` for review-ui's Cluster filter
