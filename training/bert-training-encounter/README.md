# Encounter-type training

Trains a page-level encounter-type model: one DistilBERT encoder, one head over five
classes. Stands alone: it imports nothing from `bert-training/`, `annotation-tool/` or
`core-pipeline/`.

| Class | Pages (2026-10-25) |
|---|---|
| Outpatient (F2F) | 1,227 |
| Not Available | 1,118 |
| Inpatient | 234 |
| Outpatient (Evisit) | 120 |
| Outpatient (Home Visit) | 51 |

| File | |
|---|---|
| `train_encounter.py` | the trainer |
| `predict_encounter.py` | inference on text or a JSONL file |
| `encounter.ipynb` | Colab (or a PC) notebook that runs the above |
| `requirements.txt` | the same as `bert-training/` |
| `data/encounter_tags.jsonl` | input: a copy of `Training/encounter/encounter_tags.jsonl` |
| `data/mock_pages.jsonl` | optional input (`--with-mock`): `Training/mock/mock_pages.jsonl` |
| `output/encounter/` | output: the best epoch |

`data/` and `output/` are git-ignored (patient text, large weights). Page text is
never printed.

## Run

On **Colab**: put `train_encounter.py`, `predict_encounter.py`, `requirements.txt`
and `encounter_tags.jsonl` (+ `mock_pages.jsonl`) in one Drive folder, open
`encounter.ipynb`, choose a GPU runtime, set `BASE_PATH`, run the cells in order.

From a terminal, in this folder (install as in `bert-training/README.md`):

```bash
cp "/Volumes/Extreme SSD/Advantmed/Training/encounter/encounter_tags.jsonl" data/
python train_encounter.py --check          # counts, split, 2 steps, time estimate; saves nothing
python train_encounter.py                  # 20 epochs -> output/encounter/
python train_encounter.py --with-mock      # + data/mock_pages.jsonl, training split only
python predict_encounter.py --text "TELEHEALTH VISIT ..."
python predict_encounter.py --jsonl pages.jsonl --out predictions.csv   # no text in the CSV
```

| Option | Default | |
|---|---|---|
| `--epochs` | 20 | the best epoch by validation macro F1 is kept |
| `--inherited-weight` | 0.5 | loss weight of a label carried from another page of the visit; 0 leaves them out |
| `--with-mock [JSONL]` | off | synthetic pages, always in the training split |
| `--max-length` / `--head-fraction` | 512 / 0.85 | a long page keeps its first 85% of tokens and its last 15% |
| `--val-fraction` / `--test-fraction` | 0.15 / 0.15 | by chart; `--test-fraction 0` scores on validation |
| `--min-chars` | 20 | shorter pages are left out and counted |

## What is learned, and what is not

Each row of `encounter_tags.jsonl` carries a `basis`, which says where its label came
from:

| basis | Pages | Meaning | In the loss |
|---|---|---|---|
| `prior` | 1,112 | from the page type (a demographics sheet, a lab report → Not Available) | weight 1 |
| `inherited` | 1,086 | carried from another page of the same visit; the page itself may say nothing | weight 0.5 (`--inherited-weight`) |
| `inferred` | 236 | read from the page's content (vitals and exam → F2F) | weight 1 |
| `stated` | 198 | the page says it ("telehealth visit", "admitted") | weight 1 |
| `field` | 94 | a patient-type / visit-type field on the page | weight 1 |
| `hand-labelled` | 3 | a human set it | weight 1 |
| `default` | 21 | **`train: false`**: outpatient with no modality stated; the client default is Evisit | **never learned** |

The `default` rows are predicted and reported on their own. The client rule
"outpatient with no modality statement → Evisit" stays an explicit rule after the
model; a model that learned it would call every thin outpatient page Evisit.

The loss is class-weighted (inverse frequency over the training rows), so Home Visit
and Evisit count as much as F2F, and the best epoch is chosen by macro F1 for the same
reason.

## The split

By chart: the leading number of `chart_id` (`53688890` and `53688890_240126_2000` are
one chart), balanced by class as far as whole charts allow, test first, then
validation; every class keeps training pages. Loose pages (no chart) stand alone.
Synthetic pages are training only, so every score is a real-page score.

## The report

* **Pages** — per-class recall, precision and F1 on the held-out test charts, over the
  learnable rows (Home Visit and Evisit flagged as the small classes), and page
  accuracy by `basis` (expect `inherited` to be the hardest).
* **Visits** — the pipeline's document rule applied to the gold labels and to the
  predictions over each `visit`: every page votes, the first page twice, a tie goes to
  the first page, and Not Available wins only when every vote is Not Available. This
  is the number that matches what the pipeline stores.
* **Client-default rows** — what the model says on the `train: false` rows.

Files in `output/encounter/`: `label_space.json`, `encoder/`, `head.pt` (the model);
`metrics_summary.json`, `class_metrics_{pages,visits}.csv`,
`confusion_{pages,visits}.csv`, `predictions.csv`, `errors.csv` (no text);
`training_log.csv`, `run_config.json`, `split.csv`.

## In the pipeline (not wired yet)

The pipeline decides encounter type with the tiered rule engine in
`core-pipeline/stages/lib/encounter/` (keyword tiers, page-type priors, the document
vote). The model would add one more vote per page, used where the rules have no
stated or field evidence. How, and in what order, is in `../../docs/APPROACH.md`.

## Tests

```bash
python -m pytest tests -q        # made-up pages only; no model download
```
