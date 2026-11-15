# chart-clustering (first cut)

Unsupervised segments of a set of charts (about 550) from each chart's **first Progress Note
page** and **last page**: TF-IDF on template words → LSA → HDBSCAN; charts that fit no group are
one **Others** cluster. Standalone: it reads charts from the database, Azure Blob or a local
folder and imports nothing from core-pipeline. The why and how: [APPROACH.md](APPROACH.md).

## Set up

Windows Command Prompt:

```
cd chart-clustering
py -3.12 -m venv .venv
.venv\Scripts\activate.bat
pip install -r requirements.txt
pip install "psycopg[binary]"                 (only for --source db)
pip install azure-storage-blob azure-identity (only for --source blob)
copy .env.example .env                        (then fill in .env)
```

Ubuntu VM:

```
cd chart-clustering
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install "psycopg[binary]" azure-storage-blob azure-identity
cp .env.example .env                          # then fill in .env
```

## Run

From the database (Tesseract text + the pipeline's page labels):

```
python -m chart_clustering --source db --out out\run1
python -m chart_clustering --source db --chart-list charts.txt --out out\run1
```

From Blob (the chart output folders, e.g. `Processed/Run1/<chart>/ocr/<chart>_prelim.txt`):

```
python -m chart_clustering --source blob --container imaging-pipeline --prefix Processed/Run1 --out out\run1
```

**A set of charts across runs / batches** (`charts.txt`, one chart name per line):

```
python -m chart_clustering --source db --chart-list charts.txt --out out\run1
python -m chart_clustering --source blob --container imaging-pipeline --chart-list charts.txt --paths-from-db --out out\run1
python -m chart_clustering --source blob --container imaging-pipeline --chart-list charts.txt --prefix Processed/Run1 --prefix Processed/Run2 --out out\run1
```

- `db` looks every chart up by name, whatever run or batch it came from.
- `blob` fetches each listed chart directly (no listing): first at its own output path from
  `chart_list` (`--paths-from-db`, needs DATABASE_URL), then under each `--prefix` in turn.
- Listed charts that are not found are written to `missing_charts.txt` in the output folder.

From a local folder (`core-pipeline/outputs/charts` shape):

```
python -m chart_clustering --source folder --root ..\core-pipeline\outputs\charts --out out\run1
```

(Ubuntu: same commands with `/` in paths.)

Useful options: `--notes 3` (first pages of up to 3 Progress Notes per chart), `--anchors first_pn|last|both` (default both), `--min-cluster-size 5`,
`--min-samples 3`, `--min-charts 3`, `--layout-weight 0.35`, `--stability-runs 5`, `--seed 42`.

## Read the result

1. `out/run1/run_manifest.json`: clusters and sizes, silhouette, stability (target ≥ 0.8).
2. `out/run1/cluster_cards.md`: per cluster its template words, layout averages and example
   charts; write a name for each.
3. `out/run1/chart_clusters.csv`: every chart's cluster.

No page text, names, DOBs or member ids are written anywhere.

## Load the clusters into chart_list (review-ui's Cluster filter)

`chart_clusters.csv` is the chart -> cluster file. Load it into `chart_list.chart_cluster`
(schema/migrations/2026-11-15.sql; the loader adds the column if missing). Last Run At is kept.

Windows Command Prompt:

```
cd core-pipeline && .venv\Scripts\activate.bat
python ..\utilities\load_chart_clusters.py ..\chart-clustering\out\run1\chart_clusters.csv --dry-run
python ..\utilities\load_chart_clusters.py ..\chart-clustering\out\run1\chart_clusters.csv
```

Ubuntu VM:

```
cd core-pipeline && source .venv/bin/activate
python ../utilities/load_chart_clusters.py ../chart-clustering/out/run1/chart_clusters.csv --dry-run
python ../utilities/load_chart_clusters.py ../chart-clustering/out/run1/chart_clusters.csv
```

Or in one go: add `--write-db` to the clustering run. Any CSV with `chart_name` and `cluster`
columns works (e.g. after renaming C01 to a real name). Cluster numbers are per run: a new run
can renumber them, so reload after each run you keep.

## Layout

```
chart_clustering/
  sources.py   folder / db / blob readers -> Chart
  ingest.py    Chart and Page; the prelim.txt split; final.csv labels
  anchors.py   first Progress Note page, last page
  features.py  template words, layout numbers
  cluster.py   TF-IDF -> SVD -> HDBSCAN; Others; silhouette; stability
  report.py    the output files
  vocab.py     the generic words a card may show
  cli.py       the command line
```

Test: `python -m pytest ../tests/test_chart_clustering.py -q` (from this folder, with the
repo's test environment).
