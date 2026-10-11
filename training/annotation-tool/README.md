# Annotation tool

Tag page images with a model type and build `training_data.jsonl` for the
BERT page classifier. Runs on your machine only (bound to `127.0.0.1`, no
outside calls). Stands alone: it imports nothing from `bert-training/` or
`core-pipeline/`, and names come only from its own `taxonomy.json` (a copy of
(a copy of `core-pipeline/stages/lib/keyword-canon/page_taxonomy.json`); copy it again when the taxonomy
changes).

## What it does

* Opens a folder of page images (jpg, jpeg, png, tif, tiff, bmp, webp, heic,
  heif), including every sub-folder. `image_labels.csv` sits in the folder's
  root.
* Shows each image (zoom, fit to width, turn) with two searchable boxes:
  **Page type**, then **Sub-type**. Search matches the start of any word, in
  any order: `visit` finds *Office Visit*, `op rep` finds *Operative Report*.
  * Changing the page type clears the sub-type; the sub-type box lists only
    that page type's sub-types. Left empty, the sub-type is the generic one
    (the page type's own name).
  * A lab or radiology page is tagged as Laboratory Data / Radiology Report
    even when it sits inside a Progress Note: the model type is the same, and
    the pipeline assigns Progress Note / Laboratory Data at inference. Pages
    saved earlier as Progress Note / Laboratory Data still open and save.
  * The model type and codability follow and are shown: the sub-type for a
    Progress Note, otherwise the page type.
* **Save** writes the row to `image_labels.csv` (marking it "not in training
  yet") and moves to the next image. **Skip** moves on without saving. Nothing
  is written until Save.
* **Completed:** a saved page is marked completed (column `is_completed` in
  `image_labels.csv`), even if its labels did not change, and drops out of the
  list. Tick **Show completed (N)** at the top to see them again. Skip does not
  complete a page; Untag and Undo send it back to the list.
* **Add to Training JSON** (top right, with the number of pages it will add or
  update beside it) is the only thing that changes
  `training_data.jsonl`. It takes every saved row not yet in training, and any
  page whose image file changed since it was read: a page whose image is
  unchanged keeps its text and only takes the new labels; a new or changed
  image is read with OCR. Then those rows are set to `Yes`. Progress is saved
  every few pages; click again to carry on.

Opening a folder changes nothing on disk. A missing `is_training_added`
column, images with no row, and `Yes` rows with no training record are fixed
in memory and only written on the first Save or Add to Training JSON. Before the tool first writes to a folder, `image_labels.csv` is copied once
to `image_labels.backup.csv` (the original); later sessions leave that copy alone
and make no more.

Page text is patient data: the screen, the terminal and the logs never show
it. Only counts and file names are printed.

## OCR settings

One function, `ocr.py`: EXIF rotation applied, longest side shrunk to 2600 px,
greyscale, Tesseract 5, `lang eng`, `--psm 3`, line breaks kept, runs of blank
lines collapsed. Keep it identical to how the existing training text was read.

## Install

Python 3.11 or 3.12, plus Tesseract 5.

**Mac**

```bash
brew install tesseract
cd training/annotation-tool
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

**Windows (PowerShell)**

1. Install Tesseract from <https://github.com/UB-Mannheim/tesseract/wiki>.
2. Then:

```powershell
cd training\annotation-tool
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:TESSERACT_CMD = "C:\Program Files\Tesseract-OCR\tesseract.exe"
```

`TESSERACT_CMD` is needed when `tesseract` is not on `PATH` (always on Windows).

## Run

```bash
python app.py ~/Desktop/Training/processed          # open http://127.0.0.1:5055
python app.py ~/Desktop/Training/processed --add-to-training   # no screen: Add to Training JSON
python app.py                                        # asks for the folder
```

Windows: `python app.py C:\Users\me\Desktop\Training\processed`.

Keys: `t` jump to the Page type box; in a box, arrows + Enter choose from the
list, and Enter with the list closed saves; `Enter` Save, `s` / `→` Skip,
`←` previous, `+` / `-` zoom, `0` fit width, `r` turn, `u` undo the last save.

Filters: untagged, not in training yet, in training, by model type; search by
file name.

## Tests

```bash
pip install -r requirements.txt
python -m pytest tests -q
```

The tests use made-up images and CSVs only.
