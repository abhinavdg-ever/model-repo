# models/encounter/ — the encounter-type model

The output folder of `training/bert-training-encounter/train_encounter.py`
(Colab run 2026-10-25, best epoch 4 of 20): one DistilBERT encoder, one head over
Inpatient · Outpatient (F2F) · Outpatient (Evisit) · Outpatient (Home Visit) · Not Available.

```
encounter/
├── encoder/              # DistilBERT encoder + tokenizer (model.safetensors is git-ignored: copy it onto each host)
├── head.pt               # the one head
├── label_space.json      # classes and training settings
└── metrics_summary.json  # the run's held-out scores
```

**In use** (2026-10-25): the encounter stage asks it for a page with no tier 1 / tier 2
evidence, and counts its answer for Evisit and Home Visit at >= 0.60 and Inpatient at
>= 0.70 (`stages/lib/keyword-canon/encounter_canon.json` -> `model_vote`). Without the
weights the rules decide alone; `/health` -> `encounter_model.ready` says which.
`docs/APPROACH.md` §5.1.
