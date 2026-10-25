# models/encounter/ — the encounter-type model

The output folder of `training/bert-training-encounter/train_encounter.py`
(Colab retrain 2026-10-25: page target, No-evidence pages left out, Evisit / Home Visit balanced; best epoch 9 of 20; test page accuracy 0.76): one DistilBERT encoder, one head over
Inpatient · Outpatient (F2F) · Outpatient (Evisit) · Outpatient (Home Visit) · Not Available · No evidence on page (never voted).

```
encounter/
├── encoder/              # DistilBERT encoder + tokenizer (model.safetensors in git LFS: git lfs pull)
├── head.pt               # the one head
├── label_space.json      # classes and training settings
└── metrics_summary.json  # the run's held-out scores
```

**In use** (2026-10-25): the encounter stage asks it for a page with no tier 1 / tier 2
evidence, and counts its answer for Evisit, Home Visit and Inpatient at >= 0.50, or >= 0.30 with one
of the setting's keywords on the page (`stages/lib/keyword-canon/encounter_canon.json` -> `model_vote`). Without the
weights the rules decide alone; `/health` -> `encounter_model.ready` says which.
`docs/APPROACH.md` §5.1.
