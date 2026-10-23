# Page-axes training

Trains the page-axes model the pipeline loads: one DistilBERT encoder, five heads
(L1, L2, L3, specialty, codeability) over `taxonomy_v29.json`. Stands alone: it
imports nothing from `annotation-tool/` or `core-pipeline/`.

| File | |
|---|---|
| `train_axes.py` | the trainer |
| `predict_axes.py` | inference on text or a JSONL file |
| `tune_thresholds.py` | the routing thresholds from a run's `predictions_val.csv` |
| `page_axes.ipynb` | Colab (or a PC) notebook that runs the above |
| `taxonomy_v29.json` | the label space (written by `../taxonomy/build_taxonomy_v29.py`) |
| `requirements.txt` | |
| `data/training_data.jsonl` | input: copy it from the annotation tool's image folder |
| `output/page-axes/` | output: the best epoch |

`data/` and `output/` are git-ignored: the text is patient content and the weights
are large. Page text is never printed: only counts, class names, shapes and metrics.

The old single-head classifier (`train.py`, `colab.ipynb`, `taxonomy.json`) was
removed on 2026-10-23; it is in git history. The pipeline keeps loading its saved
weights from `core-pipeline/models/page-family/bert/` for the raw `bert_*` columns
only.

## Install

Python 3.11 or 3.12.

**Mac / Linux**

```bash
cd training/bert-training
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

**Windows (PowerShell), NVIDIA GPU**

1. Install the CUDA build of PyTorch first. Pick your OS, pip and a CUDA
   version on <https://pytorch.org/get-started/locally/> and run the command it
   shows (it looks like `pip install torch --index-url https://download.pytorch.org/whl/cuXXX`).
2. Then the rest:

```powershell
cd training\bert-training
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
# the pytorch.org command from step 1 here
pip install -r requirements.txt
```

Confirm the GPU is seen:

```powershell
python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

`True` and your card's name means training will use it. `False` means the CPU
build of PyTorch is installed: uninstall it (`pip uninstall torch`) and repeat
step 1.

On Colab the notebook installs `requirements.txt` itself.

## Page axes (taxonomy v29): L1, L2, L3, specialty, codeability

`train_axes.py` replaces the single `model_type` head with one encoder and
five heads over `taxonomy_v29.json` (2026-10-23: built by
`training/taxonomy/build_taxonomy_v29.py` from the v29 rules file in
`training/taxonomy/v29/`; before that `taxonomy_v24.json`, built from the client's
`Page_Taxonomy.xlsx` by `training/taxonomy/build_taxonomy.py`; no page type
name is in the code).

```
                      ┌─ L1           folded: thin L1s become OTHER
text ─ DistilBERT ────┼─ L2           masked by L1; not trained under Progress Note
        [CLS]         ├─ L3           masked by L2; only under the Lab Report/Result buckets
                      ├─ specialty
                      └─ codeability
```

### On Colab (the usual way)

1. Copy **`train_axes.py`, `predict_axes.py`, `taxonomy_v29.json`,
   `requirements.txt`** (from this folder) and **`training_data.jsonl`** (from
   the annotation tool's image folder) into one Drive folder.
2. Open `page_axes.ipynb` in Colab, Runtime → Change runtime type → **GPU**.
3. Set `BASE_PATH` to that Drive folder and run the cells in order: install,
   GPU check, `--check`, train, metrics, errors, inference box.

The model lands in `BASE_PATH/page-axes/`. The default base model
`distilbert-base-uncased` is downloaded from Hugging Face on Colab. Where
Hugging Face is blocked, set `BASE_MODEL` (notebook) or `--base-model` to a
folder with vendored weights.

### From a terminal

```bash
python train_axes.py --check                       # label space, split, 2 steps, time estimate; saves nothing
python train_axes.py                               # 5 epochs → output/page-axes/
python train_axes.py --base-model /models/distilbert-base-uncased
python predict_axes.py --text "OPERATIVE REPORT ..."
python predict_axes.py --jsonl pages.jsonl --out predictions.csv     # no text in the CSV
```

`page_axes.ipynb` also runs on a PC from this folder (`data/`, `output/`).

| Option | Default | |
|---|---|---|
| `--min-l1-pages` | 11 | L1s with 10 pages or fewer fold into `OTHER` (the keyword canon routes them) |
| `--min-l2-pages` / `--min-l3-pages` | 3 / 3 | rarer L2s leave the L2 loss; rarer L3s fold into `Others` under their L2 |
| `--min-specialty-pages` | 3 | rarer specialties leave the specialty loss |
| `--max-length` / `--head-fraction` | 512 / 0.85 | a long page keeps its first 85% of tokens and its last 15% (letterhead and signature) |
| `--val-fraction` / `--test-fraction` | 0.15 / 0.15 | by group; `--test-fraction 0` scores on validation |
| `--select-heads` | `L1,L2,L3` | mean validation macro F1 of these picks the best epoch |
| `--route-model` / `--route-canon` | 0.5 / 0.25 | the abstention thresholds |

### What each head can do with today's 1,011 pages

* **L1** — 14 real classes plus `OTHER` (Immunization and Others, with 10
  pages or fewer, fold in): an L1 is trained once it has more than 10 pages.
  L2, L3 and specialty need 3 pages; today that gives 27 L2, 23 L3 (8 rare
  L3s fold into Others) and 13 specialties.
* **L2** — classes are (L1, L2) pairs, and the logits are masked to the L1's
  own L2s (gold L1 in training, predicted L1 at inference), so it only ever
  chooses among 2–5. Under Progress Note the L2 is the content section, owned
  by section tagging: those 350 pages carry no L2 loss. Only Lab Report/Result
  has real depth; expect poor macro F1 on the small parents.
* **L3** — only under the four Lab Report/Result L2s. 93 pages there have a
  blank L3, which is a missing label: they are left out until tagged in the
  annotation tool.
* **specialty** — weak: most pages are `Unknown`. Keep the keyword detector
  and document inheritance in front of it.
* **codeability** — trainable, but its label mostly comes from a rule, so the
  model partly learns to imitate the canon tag. `metrics_summary.json` counts
  where model and rule disagree; `errors.csv` lists them.

The run prints, per head, how many pages train it and why the rest are left out.

### The split is by group, not by page

Pages from one chart share letterhead, banner and footer, so a page-level
split leaks them and flatters the model. A group is:

* a chart — the leading number of `chart_id` (`53688890` and
  `53688890_240126_2000` are one chart);
* any page without a `chart_id` (phone photos, sample files, about 70% of
  pages today) — on its own.

Test is picked first, then validation, balancing L1 as far as whole groups
allow; every class keeps at least one training page. `split.csv` lists where
each page went. With today's data a few small L1s miss validation or test
entirely; the per-class tables then show them with no support.

### Output folder (`output/page-axes/`)

| File | |
|---|---|
| `encoder/`, `heads.pt`, `label_space.json` | the model: encoder + tokenizer, the five heads, every class list, fold and mask. All `predict_axes.py` needs |
| `metrics_summary.json` | macro F1 per head, full-path accuracy, hierarchical precision / recall, routes, codeability disagreements — on test |
| `category_wise_metrics.csv` | head, class, precision, recall, F1, support, training pages |
| `confusion/L1.csv`, `confusion/L2_<l1>.csv` | L1 confusion, and L2 within each L1 |
| `errors.csv`, `predictions_val.csv`, `predictions_test.csv` | gold and predicted per head, L1 confidence and top 3, route; no text |
| `training_log.csv`, `run_config.json`, `split.csv` | per-epoch scores, settings and page counts, the split |

Full-path accuracy counts a page right only when L1, L2 and L3 are all right.
Progress Note and `OTHER` are scored on L1 alone (their L2 is not the
model's). Per-head L2 and L3 scores assume the right parent; the path scores
use the predicted chain.

**Route** (`predict_axes.py`, `errors.csv`): `model` when the top L1 is at
least 0.5, leads the second by 0.1 and is not `OTHER`; `canon` when it is
`OTHER` or at least 0.25; otherwise `review`. An unsure page goes to the canon
or to review, never to its best guess.

There is no `--resume` here: a whole run is short on a Colab GPU (the old single-head BERT took about 12 seconds per epoch on a T4).

`label_space.json` also records `train_pages` (training pages per class, per
head): the pipeline asserts a specialty only when it was trained on enough
pages. `predictions_*.csv` carry `specialty_top3` for the specialty lead.

### Into the pipeline

1. Copy `encoder/`, `heads.pt` and `label_space.json` from `output/page-axes/`
   (Colab: `BASE_PATH/page-axes/`) into `core-pipeline/models/page-family/page-axes/`.
2. Pick the gates from the validation predictions and copy the suggestions
   into `core-pipeline/stages/lib/keyword-canon/page_arbitration.json` → `axes`:

   ```bash
   python tune_thresholds.py                      # output/page-axes/predictions_val.csv
   python tune_thresholds.py --predictions BASE_PATH/page-axes/predictions_val.csv --target 0.95
   ```

   It prints coverage and precision per threshold for L1, L2 and specialty,
   and suggests the lowest threshold reaching `--target` (0.90) precision. L3
   and Codable / Non-Codable are not tuned: the keyword canon decides them.
3. Restart the API; `/health` → `page_axes_model.ready` should be true.

In the pipeline the axes are the page type (`page_type` / `page_subtype` as
L1 / Others, L1 / L2 or "L1 (L2)" / L3; the whole answer in
`page_classification.axes`): L1 and L2 are the model's at 0.40 or more,
otherwise the v24 keyword canon's at 0.50; L3 is always the canon's at 0.50.
Its inference code is a copy of
this file's (`core-pipeline/stages/lib/page_classify/axes_model.py`): after
changing `encode_texts`, `build_model`, `decode`, `route`, `load_model` or
their helpers here, copy them there — `tests/test_page_axes.py` fails until
you do.

## Tests

Made-up text and a tiny local model; no internet, no real pages.

```bash
pip install -r requirements.txt
python -m pytest tests -q
```
