# Annotation tool

Verify page labels against taxonomy v24 and keep `training_data.jsonl` in step
with them. Runs on your machine only (bound to `127.0.0.1`, no outside calls).
Stands alone: names come only from its own `taxonomy_v24.json`, which
`../taxonomy/build_taxonomy.py` writes from `Page_Taxonomy.xlsx`. A new page
type is a workbook edit and a re-run of that script, never a code change.

## What it does

A page carries four parallel things, not one tree:

* **L1** (16 values), with the client's definition shown under the box.
* **L2**, offered **only under the chosen L1**; `Consent Form > Radiology`
  cannot be picked. Under the section L1 (Progress Note) L2 is the *content
  section on the page*: the section tagger's choice is pre-selected and its
  dominant header and the sections found are shown as the reason. L1s with no
  L2 (Problem List, Others) hide the box.
* **L3**, shown only where the L2 has named L3s (the four Lab Report/Result
  buckets). There `Others` is valid and blank is not; everywhere else blank is
  the correct answer.
* **Specialty**, its own control, pre-filled by the detector. **Unknown** (no
  signal on the page) and **Others** (a real service line the list lacks: extend
  the lexicon) are separate buttons, and Others is counted in the header.

**Codeability is displayed, not chosen**: the derived value with the rule and
reason retag.py recorded. If the path is changed, the panel says the value was
derived for the old path and shows the taxonomy's tag for the new one; re-run
retag.py to re-derive. **Override** needs a reason, and both values are kept
(`codeability` and `codeability_override` / `_reason`). An override of a
`page_type_tag` verdict means the canon tag is wrong; of a `context` verdict,
usually that continuity grouped the page wrongly. Unrecognised is
`needs_review`, never `non_codeable`.

### Queues (the list's first box)

* **Disagreements: text vs label** — rows where `label_agrees_with_text` is
  `n`. The callout shows the label and the canon's reading of the text: `1`
  keeps the label, `2` takes the canon. Where the canon names only part of a
  path, `2` fills what it can and asks for the rest. The choice is stored in
  `disagreement_resolution`. Start here.
* **Not valid under the taxonomy** — e.g. a Lab Report/Result page with a blank L3.
* **Codeability needs_review**, **Specialty: Others**, **Codeability overridden**,
  untagged, not in training, in training, missing files.

### The CSV

All existing columns stay, including `annotator_page_type` /
`annotator_page_subtype`, the audit trail to the original labelling. The tool
adds `suggested_L1/L2/L3/specialty` (what the machine proposed, snapshotted
before the first review), the override columns, `disagreement_resolution`,
`is_training_added`, `is_completed`, `status`, `saved_at`. Before the first
write the CSV is copied once to `image_labels.v24.backup.csv`.

**Add to Training JSON** writes the labels (L1, L2, L3, path, specialty, and
the override if any, else the derived codeability) into
`training_data.jsonl`. A record keeps its text unless its image is known to
have changed; only new pages are read with OCR. The old derived `L1_train` /
`L2_train` fields are dropped: the trainer derives folding and masks itself.

### Not here yet

`canon_suggests` is a canon page-type name. Until `codeable_canon.v3.json` and
`resolve_taxonomy.py` are in the repo, the tool maps it to a path by how this
CSV's own unreviewed rows resolved that page type. New images get no
pre-filled section or specialty, because the section tagger and specialty
detector are not here either.

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

Keys: `t` jump to L1, `p` to specialty; `1` / `2` resolve a disagreement; in a
box, arrows + Enter choose from the list, and Enter with the list closed saves; `Enter` Save, `s` / `→` Skip,
`←` previous, `+` / `-` zoom, `0` fit width, `r` turn, `u` undo the last save.

`python check_taxonomy.py <folder>` prints the same queues without the screen.

## Tests

```bash
pip install -r requirements.txt
python -m pytest tests -q
```

The tests use made-up images and CSVs only.
