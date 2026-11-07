# models/page-family/page-leaf/ — the page-leaf model

The output folder of `training/bert-training-leaf/train_leaf.py` (Colab 2026-11-02, Bio_ClinicalBERT,
taxonomy v29.3, best epoch 18 of 20): one encoder, a leaf head (113 classes, one per whole path
L1 > L2 > L3; 22 rarer leaves folded into their parent's Others, `label_space.json -> folded`) and a
specialty head (29). Test charts: leaf macro F1 0.47, accuracy 0.58, top-5 recall 0.76, L1 0.81.

```
page-leaf/
├── encoder/              # encoder + tokenizer (model.safetensors not in git: copy onto each host)
├── heads.pt              # leaf + specialty heads
├── label_space.json      # leaf classes, folds, specialties, settings
├── metrics_summary.json  # held-out scores
└── per_leaf.csv          # per leaf: training pages, test support, F1
```

`module/page_classify/leaf.py` loads it (`/health` -> `page_leaf.ready`); without it the
page-axes model stands in. A leaf with no class of its own (folded, or never in the training
data) takes the model rank of the class it trained as when the keywords rank it.
