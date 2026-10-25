# models/page-family/page-axes/

Step by step (what to copy, replace and check): [docs/APPROACH.md §7.4](../../../../docs/APPROACH.md#74-from-colab-to-the-pipeline).

```
models/page-family/
└── page-axes/    this folder: the page-axes model (PAGE_AXES_MODEL_DIR); decides page_type / page_subtype
models/page-family-backup/2026-10-16/   the old single-head BERT — retired, backup only
```

The page-axes model goes here: the output folder of
`training/bert-training/train_axes.py` (`output/page-axes/`, or
`BASE_PATH/page-axes/` on Colab). Copy these from it:

```
page-axes/
├── encoder/            # Bio_ClinicalBERT encoder + tokenizer (config.json, model.safetensors 433 MB, tokenizer files)
├── heads.pt            # the four heads: L1, L2, L3, specialty (a five-head model with codeability still loads)
└── label_space.json    # every class list, mask and training count the model was trained with
```

The metrics files (`metrics_summary.json`, `predictions_*.csv`, `errors.csv`, …)
are not needed here.

`PAGE_AXES_MODEL_DIR` (default `models/page-family/page-axes`) points at this
folder. The weights stay local (`encoder/model.safetensors`
and `heads.pt` are git-ignored); copy them onto each host. Until they are here,
`/health` shows `page_axes_model.ready: false` and the keyword canon (`page_canon.json`) decides
`page_type` / `page_subtype` alone (`page_classification.axes` → `route` null).

The model must be trained on the taxonomy version named in
`stages/lib/keyword-canon/page_arbitration.json` → `axes.taxonomy_version`
(v29.2); another version is refused at load, naming both, and the canon decides.
Installed 2026-10-25: the v29.2 Bio_ClinicalBERT run (docs/APPROACH.md §7.1); the
DistilBERT model before it is in `models/page-family-backup/2026-10-25-distilbert/`.
