# Annotation tool

Verify page labels against taxonomy **v29.2** and keep `training_data.jsonl` in
step with them. Runs on your machine only (bound to `127.0.0.1`, no outside
calls). Names come only from its own `taxonomy.json`, a byte-for-byte copy
of `../taxonomy/taxonomy.json` (a test checks the two are equal). A
`label_sets.json` in the label folder (or `--label-sets`) is cross-checked
against it at start-up and every difference printed. The keyword canon
(`../taxonomy/page_canon.json`, the pipeline's copy) is only a keyword source: each
canon path is kept only as far as it is valid under the taxonomy.

## What it does

A page carries four parallel things, not one tree:

* **L1** (17 values in v29.2), with the client's definition shown under the box.
* **L2**, offered **only under the chosen L1**; `Consent Form > Radiology`
  cannot be picked. Under the section L1 (Progress Note) L2 is the *content
  section on the page*: the section tagger's choice is pre-selected and its
  dominant header and the sections found are shown as the reason. L1s with no
  L2 (Problem List, Others) hide the box.
* **L3**, shown only where the L2 has named L3s (the four Lab Report
  buckets). Blank is valid there too when the taxonomy lists the L2 path on
  its own (v29.2 does: "Lab Report > Diagnostic Study" with no named study);
  everywhere else blank is the correct answer.
* **Specialty**, its own control, pre-filled by the detector. **Unknown** (no
  signal on the page) and **Others** (a real service line the list lacks: extend
  the lexicon) are separate buttons, and Others is counted in the header.

* **Encounter type** (Inpatient / Outpatient (F2F) / Outpatient (Evisit) /
  Outpatient (Home Visit) / Not Available, read from `encounter_canon.json`
  `classes`), shown with who decided it and on what basis. A change is saved
  as a human decision: `reviewer=human`, `basis=hand-labelled` and, in the
  JSONL, `train=true`, in both `encounter_tags.jsonl` and
  `encounter_review.csv`. Evidence, confidence and visit are left as they were.

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
* **Claude-reviewed** — rows whose `label_agrees_with_text` is
  `claude-reviewed`. The column's values are `''`, `y`, `n`, `hand-labelled`,
  `unreviewed`, `claude-reviewed`: a Save that leaves L1/L2/L3/specialty as
  they were keeps the value; a Save that changes one sets `y`.
* **Codeability needs_review**, **Specialty: Others**, **Codeability overridden**,
  untagged, not in training, in training, missing files.

### The CSV

All existing columns stay, including `annotator_page_type` /
`annotator_page_subtype`, the audit trail to the original labelling. The tool
adds `suggested_L1/L2/L3/specialty` (what the machine proposed, snapshotted
before the first review), the override columns, `disagreement_resolution`,
`is_training_added`, `is_completed`, `status`, `saved_at`. Before the first
write the CSV is copied once to `image_labels.tool.backup.csv`.

**Only what you change is rewritten.** Opening writes nothing. A Save rewrites
the saved row only: every other row of `image_labels.csv`, every other record
of `training_data.jsonl` and every other line of the two encounter files is
written back as the exact bytes it was read from (same quoting, same CRLF or
LF, same place). On the first Save the tool's columns are appended to the
header and appear as empty cells on untouched rows; an empty
`is_training_added` is worked out again on opening. Every write is a
temporary file plus an atomic replace.

**Add to Training JSON** writes the labels (L1, L2, L3, path, specialty, and
the override if any, else the derived codeability) into
`training_data.jsonl`. A record keeps its text unless its image is known to
have changed; only new pages are read with OCR. A record whose labels change
loses the old derived `L1_train` / `L2_train` fields (the trainer derives
folding and masks itself); a record whose labels already match is not touched.

### Migrating old label names

`../taxonomy/rules/name_map.json` renames the 23 Oct v29 names
(`Lab Report/Result > Laboratory` → `Lab Report > Lab Result`, …). Only rows
whose path is not valid under v29.2 are looked at, so on v29.2 data this is a
no-op. `L2_split` names (`Progress Note > Lab Results`,
`Patient Communication > Provider Letter`, …) and an `L3_extra` value that is
not one valid name are never guessed: those rows are listed for a person.

```bash
python app.py <label folder> --migrate --dry-run   # counts, renames, rows to decide; writes nothing
python app.py <label folder> --migrate             # apply the renames (and their training records)
```

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

The label folder holds `image_labels.csv` and `training_data.jsonl`. On the
SSD layout everything else is found from it:

| What | Where (default) | Flag |
|---|---|---|
| labels, training file, `label_sets.json` | `Training/processed/` | the folder argument, `--label-sets` |
| page images | the folder (and sub-folders), then `Training/ocr-jpeg/` by stem: an `image` key ending `.HEIC` opens `ocr-jpeg/<stem>.jpg` | `--images` |
| encounter tags | `encounter_tags.jsonl` in the folder, else `Training/encounter/encounter_tags.jsonl` | `--encounter-tags` |
| encounter review | `Training/processed/encounter_review.csv` | `--encounter-review` |
| encounter classes | `encounter_canon.json` in the folder, else `../taxonomy/rules/encounter_canon.json` | `--encounter-canon` |
| taxonomy | `taxonomy.json` beside the tool | `--taxonomy` |

```bash
python app.py "/Volumes/Extreme SSD/Advantmed/Training/processed"     # open http://127.0.0.1:5055
python app.py "/Volumes/Extreme SSD/Advantmed/Training/processed" --add-to-training   # no screen
python app.py <folder> --images <folder of page images>               # images somewhere else
python app.py                                                          # asks for the folder
```

The old layout (images and `image_labels.csv` in one folder) still works with
no flags. Windows: `python app.py E:\Advantmed\Training\processed`.

Keys: `t` jump to L1, `p` to specialty; `1` / `2` resolve a disagreement; in a
box, arrows + Enter choose from the list, and Enter with the list closed saves; `Enter` Save, `s` / `→` Skip,
`←` previous, `+` / `-` zoom, `0` fit width, `r` turn, `u` undo the last save.

`python check_taxonomy.py <folder>` prints the label space (L1 count, L2 count
per L1, L3 count per Lab Report L2), whether `label_sets.json` agrees, the
queues and any old names still stored, without the screen; it exits 1 if a
row is not valid, `label_sets.json` disagrees, or an old name remains.

## Tests

```bash
pip install -r requirements.txt
python -m pytest tests -q
```

The tests use made-up images, CSVs and encounter files only. Tests that need
Flask skip when it is not installed.
