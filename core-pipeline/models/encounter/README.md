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

**Not wired into the pipeline yet.** The encounter stage still decides with the rule
engine alone (`stages/lib/encounter/`). How the model's vote joins the rules is open:
`docs/APPROACH.md` §5 and §8.
