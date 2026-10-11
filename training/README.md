# Training: page classifier

Two folders, each standing alone with its own requirements, inputs and outputs.
Neither imports from the other or from `core-pipeline/`.

| Folder | What it does | Input | Output |
|---|---|---|---|
| `annotation-tool/` | Tag page images with a page type and sub-type; read their text (Tesseract) into a training file | a folder of page images with `image_labels.csv` | `image_labels.csv` and `training_data.jsonl`, in that same image folder |
| `bert-training/` | Train the BERT page classifier | `bert-training/data/training_data.jsonl` | `bert-training/output/page-family/` (the model) |

Names (page type, sub-type, model type) come from each folder's own copy of
`taxonomy.json` (a copy of `core-pipeline/stages/lib/keyword-canon/page_taxonomy.json`). Full detail is in each
folder's README.

> **Patient data.** Page images, OCR text and labels are patient records.
> Nothing here prints page text. `bert-training/data/`, `bert-training/output/`
> and every `*.jsonl` under `training/` are git-ignored — keep it that way.

---

## The flow

```
images + image_labels.csv
        │  1. tag in the annotation tool (Save), press Add to Training JSON
        ▼
training_data.jsonl  (in the image folder)
        │  2. copy into bert-training/data/
        ▼
bert-training/data/training_data.jsonl
        │  3. train, one epoch at a time
        ▼
bert-training/output/page-family/
        │  4. copy into core-pipeline/models/page-family/
        ▼
core-pipeline loads it (page_subtype stage)
```

---

## 1. Annotation tool

**Install once** (Python 3.12; Tesseract for OCR):

```bash
# macOS
brew install tesseract
cd training/annotation-tool
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

```powershell
# Windows: install Tesseract from https://github.com/UB-Mannheim/tesseract/wiki
cd training\annotation-tool
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:TESSERACT_CMD = "C:\Program Files\Tesseract-OCR\tesseract.exe"
```

**Run:**

```bash
python app.py ~/Desktop/Training/processed          # opens http://127.0.0.1:5055
python app.py ~/Desktop/Training/processed --add-to-training   # Add to Training JSON, no screen
```

* Pick a **Page type** and **Sub-type** (searchable; any word matches) and
  press **Save** — or **Skip**. Saving marks the row "not in training yet".
* **Add to Training JSON** (top right; the number beside it is how many pages
  it will add or update) is the only thing that updates
  `training_data.jsonl`: existing records get their new labels without OCR;
  new pages, and pages whose image changed, are read with OCR and added.
  Closing mid-run loses almost nothing; click again to continue.
* Opening the folder changes nothing on disk until you Save.

## 2. Copy the training file

```bash
cp ~/Desktop/Training/processed/training_data.jsonl training/bert-training/data/
```

(The current file — 710 pages — is already there.)

## 3. BERT training

**Install once** (from `training/bert-training/`):

```powershell
.\setup_pc.ps1              # Windows: venv + CUDA PyTorch if an NVIDIA GPU is found + the rest
.\setup_pc.ps1 -Notebook    # ... and Jupyter, for pc.ipynb
```

```bash
./setup_pc.sh               # Mac / Linux
./setup_pc.sh --notebook
```

Both end by printing whether the GPU is seen. If PowerShell refuses the
script: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`. Manual steps
are in `bert-training/README.md`.

**Run** (from `training/bert-training/`):

```bash
python train.py --check            # new machine: 2 steps + minutes-per-epoch estimate; saves nothing
python train.py                    # a new run: 4 epochs (--epochs 3 or 5 to change)
python train.py --resume           # one more epoch — same classes, same validation pages
python train.py --resume --epochs 3
```

After each epoch read `output/page-family/category_wise_metrics.csv` (per-class
precision / recall / F1) and `training_log.csv`. The folder always holds the
best epoch by validation macro F1.

**Try the model on some text** (after training): the last cells of both
notebooks have a paste box and a Classify button, or from a terminal:

```bash
python predict.py --text "DISCHARGE SUMMARY ..."     # or --file page.txt, or paste after running it bare
```

It prints the top 5 model types with probabilities, and the page type and
codability each means. That is BERT alone — the pipeline adds the keyword
model and the decision ladder.

**PC notebook:** `pc.ipynb` — the same steps as Colab, run on your own
machine (Jupyter, or VS Code with `.venv` as the kernel), reading `data/` and
writing `output/` in this folder.

**Colab:** open `colab.ipynb`, set `BASE_PATH` to your Drive folder (put
`train.py`, `requirements.txt`, `taxonomy.json` and `training_data.jsonl`
there), run the cells. Output is written to Drive after every epoch.

| Where | Speed on the current data |
|---|---|
| This Mac, CPU | about 48 minutes per epoch |
| Colab, Tesla T4 GPU | about 12 seconds per epoch (0.21 s per step, measured) |

Model types with fewer than 5 pages are left out (`skipped_classes.csv`;
`--min-pages 10` for a stricter run);
the pipeline's keyword model covers them until more images arrive.

## 4. Put the model into the pipeline

```bash
rm -rf core-pipeline/models/page-family
cp -r training/bert-training/output/page-family core-pipeline/models/page-family
curl -s localhost:8001/health | python -m json.tool    # page_family_model.ready: true
```

Without a model there, the pipeline classifies by keywords alone and says so
in `/health`.

## Tests

Each folder has its own, using made-up images and text only:

```bash
cd training/annotation-tool && python -m pytest tests -q
cd training/bert-training && python -m pytest tests -q
```
