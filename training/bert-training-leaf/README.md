# Page-leaf model (taxonomy v29.3)

One encoder (Bio_ClinicalBERT by default) with a **leaf** head, one class per whole
path `L1 > L2 > L3`, plus a specialty head. It replaces the page-axes cascade in
`../bert-training/` (L1, then L2, then L3). The pipeline combines its top leaves with the
per-leaf keyword canon (`core-pipeline/modules/keyword-canon/leaf_keywords.json`).

## Files

| File | What it is |
|---|---|
| `train_leaf.py` | the trainer (leaf + specialty heads); its model code is copied into `core-pipeline/modules/page_classify/leaf_model.py` |
| `predict_leaf.py` | runs a trained folder on text files |
| `taxonomy.json` | v29.3, written by `training/taxonomy/import_taxonomy_xlsx.py`; never edit by hand |
| `requirements.txt` | the trainer's packages |
| `page_leaf.ipynb` | Colab (or a PC) notebook: check, train, results per leaf, synthetic pages, inference, compare runs, zip for the pipeline |
| `check_test_pages.py`, `test_pages.jsonl` | 12 synthetic pages with the expected leaf; fails if a Lab Report page comes back Progress Note |
| `tests/test_package.py` | the notebook parses and names files that are here; every test page expects a v29.3 leaf |
| `data/training_data.jsonl` | input: a copy of `Training/processed/training_data.jsonl` (git-ignored) |
| `output/` | output (git-ignored) |

## Train on Colab

1. Upload to one Drive folder, `MyDrive/page-leaf-training/`: `train_leaf.py`, `predict_leaf.py`,
   `taxonomy.json`, `requirements.txt`, `check_test_pages.py`, `test_pages.jsonl`,
   `page_leaf.ipynb` and `training_data.jsonl` (from `Training/processed/`).
   `page-leaf-training.zip` in this folder (git-ignored) holds all eight: unzip it into Drive.
2. Open `page_leaf.ipynb` in Colab, Runtime -> Change runtime type -> GPU (T4 or better).
3. Set `RUN` in the first code cell: `biobert` (dmis-lab/biobert-base-cased-v1.2, the default)
   or `bioclinicalbert` (emilyalsentzer/Bio_ClinicalBERT). Runtime -> Run all.
4. Each run is saved to `runs/<RUN>/` on Drive after every improving epoch; the last cell
   zips it as `runs/page-leaf-<RUN>.zip`. Run the notebook again with the other `RUN` to
   compare: the "Compare the runs" cell lists every finished run.

Useful options (`--` flags of `train_leaf.py`, or the `RUNS` settings): `--epochs`, `--batch-size`
(16 on a T4; 8 if CUDA runs out of memory), `--min-leaf-pages` (default 3: rarer leaves fold
into their parent's Others), `--oversample` (default 30), `--base-model`.

## What it writes (`output/page-leaf/`)

| File | |
|---|---|
| `encoder/`, `heads.pt` | the weights |
| `label_space.json` | `kind: leaf`, the taxonomy version, the leaf classes with their L1 / L2 / L3 and codeability, `folded` (leaf → the class it trained as), specialties, and the settings |
| `metrics_summary.json` | test-split leaf macro F1, accuracy, top-5 recall, L1 accuracy |
| `per_leaf.csv` | per leaf: training pages, support, correct, predicted, F1 |

## Put it in the pipeline

Copy `output/page-leaf/` to `core-pipeline/models/page-family/page-leaf/` on each host
(weights are not in git). `/health` → `page_leaf.ready` says whether it loaded. Until a
leaf model is there, the pipeline scores leaves from the page-axes model (its L1 × L2 × L3
probabilities, marked `model_source: axes_fallback`).

## How a page's leaves are kept

Every leaf gets a probability. The pipeline keeps the best 5 above 0.01, at most 3 under
one L1, plus any leaf tied with the last kept (`top_leaves`). Then the keyword canon scores
the same page and both are combined (APPROACH.md section 3).
