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
weights are large. When a run is good, copy `output/page-family/` to
`core-pipeline/models/page-family/` — the pipeline loads it from there.

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

## Tests

Made-up text and a tiny local model; no internet, no real pages.

```bash
pip install -r requirements.txt
python -m pytest tests -q
```
