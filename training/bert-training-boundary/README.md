# Page-pair text model (briefs 1 and 2, steps E3 / Q3)

One encoder (Bio_ClinicalBERT by default) reads the end of page A and the start of page B
(about 150 words each) and gives two probabilities:

| Head | Question | Trained on |
|---|---|---|
| `end` | Does the encounter end after page A? (page A is in an encounter) | `encounter_pairs.jsonl`, label `encounter_ends_after_n`; unsure points at half weight |
| `follows` | Does page B come directly after page A, inside one document? | `order_pairs.jsonl`, label `follows` |

The two scores are one more clue for the tree models in
`core-pipeline/stages/lib/continuity/ml/` and `core-pipeline/stages/lib/sequencing/ml/`. Those
tree models train on the laptop; this is the only part that needs a GPU.

## Files

| File | |
|---|---|
| `train_pairs.py` | the trainer: train, score the test charts, export ONNX, write a text score for every pair |
| `page_pairs.ipynb` | Colab (or a PC) notebook that runs it, checks the ONNX copy and its CPU speed, and zips the result |
| `requirements.txt` | |
| `tests/test_package.py` | the notebook parses and names files that are here |
| `data/` | `encounter_pairs.jsonl`, `order_pairs.jsonl` from `Training/boundary/` (git-ignored: page text) |
| `page-pair-training.zip` | the upload for Drive: the two scripts and the two data files (git-ignored) |

## Train on Colab

1. Unzip `page-pair-training.zip` into `MyDrive/page-pair-training/` (or copy `train_pairs.py`,
   `requirements.txt` and the two `.jsonl` files there).
2. Open `page_pairs.ipynb` in Colab, Runtime -> Change runtime type -> GPU (T4 or better).
3. Run all. The check cell prints the label counts and a time estimate. `FOLDS = 5` trains five
   more short models, grouped by chart, so every train / val pair gets a text score from a model
   that never saw it (the tree models need that; set `FOLDS = 0` to skip it).
4. The last cell writes `output/boundary-text.zip` on Drive: `model.onnx`, `tokenizer/`,
   `label_space.json`, `metrics.json`, `scores_end.csv`, `scores_follows.csv`.
   Download it and unzip to `core-pipeline/models/boundary-text/`.

Options of `train_pairs.py`: `--base-model`, `--epochs` (4), `--batch-size` (16; 8 if CUDA runs
out of memory), `--lr` (2e-5), `--words` (150 per side), `--max-length` (384), `--folds`, `--no-onnx`.

## Rules

- Split by chart (the files' `split`); test is scored once and never tuned on.
- Page text is never printed or written to the outputs: only ids, scores and metrics.
- No LayoutLMv3 (licence).
