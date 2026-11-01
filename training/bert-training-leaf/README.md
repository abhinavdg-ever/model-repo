# Page-leaf model (taxonomy v29.3)

One encoder (Bio_ClinicalBERT by default) with a **leaf** head, one class per whole
path `L1 > L2 > L3`, plus a specialty head. It replaces the page-axes cascade in
`../bert-training/` (L1, then L2, then L3). The pipeline combines its top leaves with the
per-leaf keyword canon (`core-pipeline/stages/lib/keyword-canon/leaf_keywords.json`).

## Files

| File | What it is |
|---|---|
| `train_leaf.py` | the trainer (leaf + specialty heads); its model code is copied into `core-pipeline/stages/lib/page_classify/leaf_model.py` |
| `predict_leaf.py` | runs a trained folder on text files |
| `taxonomy.json` | v29.3, written by `training/taxonomy/import_taxonomy_xlsx.py`; never edit by hand |
| `requirements.txt` | the trainer's packages |

## Train on Colab

```bash
# 1. Upload this folder and training_data.jsonl (Training/processed/training_data.jsonl)
#    to Drive, then in a GPU notebook:
pip install -r requirements.txt
mkdir -p data && cp /content/drive/MyDrive/<where>/training_data.jsonl data/

# 2. A check (two steps, a time estimate, nothing saved), then the run:
python train_leaf.py --check
python train_leaf.py            # 20 epochs; best epoch saved to output/page-leaf/

# 3. Look at output/page-leaf/metrics_summary.json and per_leaf.csv
```

Useful options: `--epochs`, `--batch-size`, `--min-leaf-pages` (default 3: rarer leaves
fold into their parent's Others), `--oversample` (default 30), `--base-model`.

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
