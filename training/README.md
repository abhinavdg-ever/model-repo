# Training: page classifier and encounter type

Three folders, each standing alone with its own requirements, inputs and outputs.
None imports from another or from `core-pipeline/`. The data lives outside the
repo, in `/Volumes/Extreme SSD/Advantmed/Training/` (`processed/` labels and training
file, `ocr-jpeg/` page images, `encounter/` encounter tags, `mock/` synthetic pages,
`reference/` notes and the 2026-10-25 Claude Code pack).

| Folder | What it does | Input | Output |
|---|---|---|---|
| `annotation-tool/` | Tag page images with a page type and sub-type; read their text (Tesseract) into a training file | a folder of page images with `image_labels.csv` | `image_labels.csv` and `training_data.jsonl`, in that same image folder |
| `bert-training/` | Train the page-axes model (L1, L2, L3, specialty, codeability) | `bert-training/data/training_data.jsonl` | `bert-training/output/page-axes/` (the model) |
| `bert-training-encounter/` | Train the encounter-type model (Inpatient, F2F, Evisit, Home Visit, Not Available) | `bert-training-encounter/data/encounter_tags.jsonl` | `bert-training-encounter/output/encounter/` (the model) |

Labels are L1 > L2 > L3, plus specialty, plus a derived codeability.

* **Trainer: taxonomy v29.2** (2026-10-25). `taxonomy/taxonomy.json` (= the
  copy in `bert-training/`) is the pack's label space, copied as is. Its rules
  version (context rules, page_type_overrides, merges), `label_sets.json`,
  `specialty.json`, `sections.json`, `encounter_canon.json` and the labelling
  scripts are copied, for reference, into `taxonomy/rules/`;
  `taxonomy/rules/name_map.json` maps the 23 Oct names to v29.2.
* **The approach** (page classification, continuity, encounter): `docs/APPROACH.md`.

The old single-head trainer (`train.py`) was removed on 2026-10-23; it is in git
history. Full detail is in each folder's README.

> **Patient data.** Page images, OCR text and labels are patient records.
> Nothing here prints page text. `bert-training*/data/`, `bert-training*/output/`
> and every `*.jsonl` under `training/` are git-ignored — keep it that way (the one
> exception is the synthetic `bert-training/test_pages.jsonl`).


**The canon** (`taxonomy/page_canon.json`, about 295 canon page types) is a copy of
the pipeline's `core-pipeline/stages/lib/keyword-canon/page_canon.json`: each canon
page type with its path in the taxonomy's names, its codeability tag and its
lexicon. The pipeline's copy is the one to edit (paths are re-derived with
`python -m stages.lib.page_classify.canon_convert` from `core-pipeline/`); a repo
test fails when the two copies differ. `python taxonomy/canon.py score
<training_data.jsonl>` scores every page against it and reports agreement with the
labels. The lexicons are starting lists: tune them on labelled pages.

---

## The flow

```
images + image_labels.csv
        │  1. tag in the annotation tool (Save), press Add to Training JSON
        ▼
training_data.jsonl  (in the image folder)
        │  2. copy into bert-training/data/ (or a Drive folder, for Colab)
        ▼
bert-training/data/training_data.jsonl
        │  3. train_axes.py / page_axes.ipynb
        ▼
bert-training/output/page-axes/
        │  4. copy into core-pipeline/models/page-family/page-axes/
        ▼
core-pipeline loads it (page classification stage)
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

* Start with the **Disagreements** queue (`1` keep label, `2` take the canon),
  then confirm **L1**, **L2** (masked by L1), **L3** (Lab Report/Result only)
  and **Specialty**, and press **Save** — or **Skip**. Codeability is shown,
  derived; override it only with a reason.
* **Add to Training JSON** (top right; the number beside it is how many pages
  it will add or update) is the only thing that updates
  `training_data.jsonl`: existing records get their new labels without OCR;
  new pages, and pages whose image changed, are read with OCR and added.
  Closing mid-run loses almost nothing; click again to continue.
* Opening the folder changes nothing on disk until you Save.

## 2. Copy the training file

```bash
T="/Volumes/Extreme SSD/Advantmed/Training"
cp "$T/processed/training_data.jsonl" "$T/mock/mock_pages.jsonl" training/bert-training/data/
cp "$T/encounter/encounter_tags.jsonl" "$T/mock/mock_pages.jsonl" training/bert-training-encounter/data/
```

## 3. Train the page-axes model

On **Colab** (the usual way): put `train_axes.py`, `predict_axes.py`,
`taxonomy.json`, `requirements.txt`, `check_test_pages.py`, `test_pages.jsonl`
and `training_data.jsonl` (+ `mock_pages.jsonl` to use the synthetic pages) in one Drive
folder, open `bert-training/page_axes.ipynb`, choose a GPU runtime, set `BASE_PATH`
and run the cells in order (`--check` first, then the run). On a PC, from
`training/bert-training/`:

```bash
python train_axes.py --check        # label space, split, 2 steps, time estimate; saves nothing
python train_axes.py                # 20 epochs -> output/page-axes/
python predict_axes.py --text "OPERATIVE REPORT ..."
```

Install, options, the group split and the output files: `bert-training/README.md`.

## 3b. Train the encounter model

Same way, from `bert-training-encounter/` (`encounter.ipynb` on Colab, with
`train_encounter.py`, `predict_encounter.py`, `requirements.txt`, `encounter_tags.jsonl`):

```bash
python train_encounter.py --check   # counts, split, 2 steps, time estimate; saves nothing
python train_encounter.py           # 20 epochs -> output/encounter/
```

Detail: `bert-training-encounter/README.md`. The pipeline does not load it yet: see
`docs/APPROACH.md` for how it would join the encounter engine.

## 4. Put the model into the pipeline

Copy `encoder/`, `heads.pt` and `label_space.json` from `output/page-axes/` into
`core-pipeline/models/page-family/page-axes/`, tune the gates with
`tune_thresholds.py`, restart the API and check `/health` →
`page_axes_model.ready: true`. A model trained on another taxonomy version than
`page_arbitration.json` → `axes.taxonomy_version` expects is refused at load.

## Tests

Each folder has its own, using made-up images and text only:

```bash
cd training/annotation-tool && python -m pytest tests -q
cd training/bert-training && python -m pytest tests -q
cd training/bert-training-encounter && python -m pytest tests -q
```
