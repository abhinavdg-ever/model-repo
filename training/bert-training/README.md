# BERT training for the page classifier

Trains a BERT text classifier that predicts `model_type` (see
`taxonomy.json`, (a copy of `core-pipeline/stages/lib/keyword-canon/page_taxonomy.json`) from the OCR
text in `training_data.jsonl`, which the annotation tool writes. Stands alone:
it imports nothing from `annotation-tool/` or `core-pipeline/`.

Everything stays in this folder:

| | Where | |
|---|---|---|
| Input | `data/training_data.jsonl` | copy it here from the annotation tool's image folder |
| Model | `output/page-family/` | the **best epoch so far by validation macro F1** |
| Resume state | `output/page-family-resume/` | weights, optimiser, scheduler, random state, the split |

`data/` and `output/` are git-ignored: the text is patient content and the
weights are large. When a run is good, copy the inference files of `output/page-family/`
(`config.json`, `model.safetensors`, `tokenizer.json`, `tokenizer_config.json`,
`skipped_classes.csv`) to `core-pipeline/models/page-family/bert/` — the
pipeline loads it from there.

Page text is never printed: only counts, class names, shapes and metrics.

## Install

Python 3.11 or 3.12. Three ways to run the same `train.py`:

| Where | Set up | Then |
|---|---|---|
| **Windows PC** | `.\setup_pc.ps1` (add `-Notebook` for Jupyter) | `python train.py …` or `pc.ipynb` |
| **Mac / Linux PC** | `./setup_pc.sh` (add `--notebook`) | `python train.py …` or `pc.ipynb` |
| **Colab** | open `colab.ipynb` | run its cells |

`setup_pc.ps1` makes `.venv`, installs the **CUDA build of PyTorch** when
`nvidia-smi` finds an NVIDIA GPU (the build is picked from your driver's CUDA
version; force one with `-Cuda cu126`, or `-Cuda cpu`), installs the rest and
prints whether the GPU is seen. `pc.ipynb` is the local twin of `colab.ipynb`:
same steps, paths in this folder, no Drive — open it with `.venv` as the kernel.

The manual steps, if you prefer:

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

**Colab**: copy this folder and `training_data.jsonl` to a Drive folder, open
`colab.ipynb`, set `BASE_PATH`, and run the cells. It only mounts Drive,
installs the requirements and runs `train.py`.

## Run

```bash
# On a new machine: load the data, two training steps, minutes-per-epoch estimate. Saves nothing.
python train.py --check

# A new run: 4 epochs (or --epochs 3 / 5). The model folder keeps the best one.
python train.py

# One more epoch: same classes, same validation pages.
python train.py --resume

# Several more.
python train.py --resume --epochs 3
```

Try the saved model on any text — the top 5 model types with probabilities
(BERT alone; the pipeline adds the keyword model and the decision ladder):

```bash
python predict.py --text "DISCHARGE SUMMARY ..."
python predict.py --file page.txt
python predict.py                      # paste, then Ctrl+D (Windows: Ctrl+Z, Enter)
```

The last cells of `colab.ipynb` and `pc.ipynb` do the same with a paste box
and a **Classify** button.

A second run without `--resume` stops rather than overwrite the saved run; use
`--restart` to start over.

| Option | Default | |
|---|---|---|
| `--data` | `data/training_data.jsonl` | the training file |
| `--output` | `output/page-family` | model folder (best epoch) |
| `--resume-dir` | `<output>-resume` beside it | resume state |
| `--model-name` | `bert-base-uncased` | any Hugging Face name or a local folder (e.g. a clinical BERT) |
| `--device` | auto | `cpu` or `cuda` to force |
| `--max-length` | 512 | tokens per page |
| `--batch-size` | 8 | lower it if the GPU runs out of memory |
| `--lr` | 2e-5 | constant after a warm-up over the first 10% of epoch 1 |
| `--epochs` | 4 for a new run, 1 with `--resume` | epochs to run now |
| `--min-pages` | 5 | classes with fewer pages are skipped (`skipped_classes.csv`) |
| `--val-fraction` | 0.2 | |
| `--min-chars` | 30 | pages with less text are left out and counted |
| `--seed` | 42 | |

Mixed precision is used only on CUDA. `--resume` reuses the model name, max
length, seed and split settings of the saved run.

## How it trains

* Label = `model_type`; every value must be in `taxonomy.json` (an unknown one
  stops the run and is named).
* Classes with fewer than `--min-pages` pages are left out — no catch-all
  class — and listed in `skipped_classes.csv`. The keyword canon covers them.
* Validation split by chart: pages of one `chart_id` never sit on both sides;
  a page with no `chart_id` is its own group. Classes are balanced as far as
  whole charts allow, and every trained class keeps at least one training page
  (a class whose pages are all in one chart then has no validation page; the
  run says so).
* Class-weighted cross-entropy, because Progress Note dominates. Judged by
  macro F1 and per-class recall, printed after every epoch.
* After the last epoch the saved folder is loaded back from disk and scored
  again, to prove the saved model works.

## Output folder

| File | |
|---|---|
| `config.json`, `model.safetensors`, tokenizer files | Hugging Face model; `id2label` / `label2id` are the model type names |
| `label_mapping.csv` | id, model_type, page_type, codability, train / val pages |
| `trained_classes.json` | the classes the model can predict |
| `skipped_classes.csv` | the ones it cannot, with their page counts |
| `category_wise_metrics.csv` | precision, recall, F1 and pages per class, for the best epoch |
| `confusion_matrix.csv` | true × predicted, best epoch |
| `training_log.csv` | loss, macro F1, accuracy and time per epoch |
| `run_config.json` | settings, versions, device, page counts, best epoch |

## Page axes (taxonomy v24): L1, L2, L3, specialty, codeability

`train_axes.py` replaces the single `model_type` head with one encoder and
five heads over `taxonomy_v24.json` (built from the client's
`Page_Taxonomy.xlsx` by `training/taxonomy/build_taxonomy.py`; no page type
name is in the code). It sits beside `train.py`, which stays because
core-pipeline still loads the old single-head model.

```
                      ┌─ L1           folded: thin L1s become OTHER
text ─ DistilBERT ────┼─ L2           masked by L1; not trained under Progress Note
        [CLS]         ├─ L3           masked by L2; only under the Lab Report/Result buckets
                      ├─ specialty
                      └─ codeability
```

### On Colab (the usual way)

1. Copy **`train_axes.py`, `predict_axes.py`, `taxonomy_v24.json`,
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

Full checklist (files, `/health`, database columns, going back): [`docs/PAGE_AXES_MODEL.md`](../../docs/PAGE_AXES_MODEL.md).

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
L1 / L1, L1 / L2 or "L1 (L2)" / L3; the whole answer in
`page_classification.axes`): L1 and L2 are the model's at 0.40 or more,
otherwise the v24 keyword canon's at 0.50; L3 is always the canon's at 0.50.
Details: `docs/PAGE_CLASSIFICATION.md` §4.6. Its inference code is a copy of
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
