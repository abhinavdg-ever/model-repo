# Installing the page-axes model

What to copy, replace and check when a trained page-axes model (L1, L2, L3,
specialty, codeability) goes into the pipeline. How the pipeline uses it:
[PAGE_CLASSIFICATION.md §4.6](PAGE_CLASSIFICATION.md#46-page-axes-v24--the-page-type-written).
How to train it: [`training/bert-training/README.md`](../training/bert-training/README.md).

The axes **are the page type**. The page-classification stage writes them as
`page_type` / `page_subtype`:

| Levels decided | `page_type` | `page_subtype` |
|---|---|---|
| L1 only | L1 | L1 |
| L1, L2 | L1 | L2 |
| L1, L2, L3 | L1 (L2) | L3 |

e.g. `Lab Report/Result (Radiology)` / `CT Scan Report`. The whole answer
(specialty, codeability, confidences, who decided each level, the model's raw
prediction) is in the one JSON column `page_classification.axes`, and in the
last columns of `imaging/<chart>_codeable.csv`.

---

## 1. Where things are today

| Path | What | Status |
|---|---|---|
| `core-pipeline/models/page-family/page-axes/` | where the page-axes model goes (`PAGE_AXES_MODEL_DIR`) | **placeholder** — only `README.md` |
| `core-pipeline/models/page-family-backup/2026-10-16/` | the old single-head BERT (was `page-family/bert/`) | **retired — backup only, not used** |
| `core-pipeline/stages/lib/keyword-canon/page_arbitration.json` → `axes` | the gates | L1 / L2 0.40, canon 0.50, L3 canon 0.50 |
| `core-pipeline/stages/lib/keyword-canon/page_canon_v24.json` | the v24 keyword canon (fallback, and all of L3) | rebuilt 2026-10-16 by `build_canon.py` |

Until the model is installed the v24 keyword canon decides every level alone
(`axes.route` is null).

---

## 2. Copy the trained model in

After training, the output folder is `training/bert-training/output/page-axes/`
(local) or `BASE_PATH/page-axes/` in Drive (Colab).

**Copy these three, and nothing else:**

```
output/page-axes/                          core-pipeline/models/page-family/page-axes/
├── encoder/          ───────────────►     ├── encoder/
├── heads.pt          ───────────────►     ├── heads.pt
├── label_space.json  ───────────────►     ├── label_space.json
├── metrics_summary.json   (leave)         └── README.md   (keep — the placeholder note)
├── category_wise_metrics.csv (leave)
├── predictions_val.csv    (leave — step 4 reads it from here)
├── predictions_test.csv / errors.csv / confusion/ / training_log.csv /
│   run_config.json / split.csv   (leave)
```

```bash
# macOS / Linux, from the repo root
SRC=training/bert-training/output/page-axes          # or the folder downloaded from Drive
DST=core-pipeline/models/page-family/page-axes
rm -rf "$DST/encoder" "$DST/heads.pt" "$DST/label_space.json"   # the previous model, if any
cp -R "$SRC/encoder" "$SRC/heads.pt" "$SRC/label_space.json" "$DST/"
find "$DST" -name '._*' -delete                       # AppleDouble files from the external drive
ls "$DST"                                             # README.md  encoder  heads.pt  label_space.json
```

```powershell
# Windows (PowerShell)
$SRC = "training\bert-training\output\page-axes"
$DST = "core-pipeline\models\page-family\page-axes"
Remove-Item -Recurse -Force "$DST\encoder", "$DST\heads.pt", "$DST\label_space.json" -ErrorAction SilentlyContinue
Copy-Item -Recurse "$SRC\encoder", "$SRC\heads.pt", "$SRC\label_space.json" $DST
```

Replace all three together. `heads.pt` only works with the `encoder/` and
`label_space.json` from the same run; a mix loads but predicts nonsense, or
fails on a shape mismatch.

The weights are not committed: `.gitignore` drops
`encoder/model.safetensors` and `heads.pt`, while the small files (configs,
tokenizer, `label_space.json`) can be. Copy the weights onto every host that
runs the pipeline. In Docker they arrive through the
models mount (`MODELS_HOST_PATH`, default `./models`), so no rebuild is needed.

A different folder works too: set `PAGE_AXES_MODEL_DIR` in `core-pipeline/.env`.

---

## 3. Check it loads

Restart the API (`python cli.py serve`, or `docker compose restart`), then:

```bash
curl -s localhost:8001/health | python -m json.tool | grep -A8 page_axes_model
```

Expect `"ready": true`, `"taxonomy_version": "v24"`. The startup banner prints
`page-axes model  OK — v24, N L1 classes, …`. `page-family model` reads off:
that is the retired BERT, and expected.

| `/health` says | Fix |
|---|---|
| `… (heads.pt missing); canon only` | a file was not copied — §2 |
| `trained on taxonomy v23; … expects v24` | retrain on `taxonomy_v24.json`, or change `axes.taxonomy_version` only if the pipeline's canon moved to that version too |
| `transformers / torch not installed` | `pip install -r requirements-models.txt` in the core-pipeline venv |

---

## 4. Set the gates from this model

The 0.40 / 0.50 defaults are starting values. Tune them on this model's
validation predictions:

```bash
cd training/bert-training
python tune_thresholds.py --predictions output/page-axes/predictions_val.csv
```

It prints coverage and precision per threshold for L1, L2 and specialty and
suggests a value for each (the lowest reaching 90% precision
on 10+ pages; `--target 0.95` for stricter). Copy the ones you accept into
`core-pipeline/stages/lib/keyword-canon/page_arbitration.json` → `axes`:

| Key | Gate | Today |
|---|---|---|
| `route_model` | model decides L1 at or above this … | 0.40 |
| `route_margin` | … and this far ahead of its second choice | 0.10 |
| `l2_min` | model decides L2 | 0.40 |
| `canon_min` | canon decides L1 / L2 when the model did not | 0.50 |
| `l3_canon_min` | canon decides L3 (always the canon) | 0.50 |
| `specialty_min`, `specialty_lead`, `specialty_min_train_pages` | model asserts a specialty | 0.70, 0.20, 10 |

The file reloads on change; no restart. A `null` suggestion means no
threshold was precise enough: keep the value, and the canon covers the rest.

---

## 5. The database (once per database)

`schema/v1.sql` has the column. A database created from it before 2026-10-16
lacks it, and the page-classification stage then fails on insert. Add it (or
recreate: `clear_schema.sql`, then `v1.sql`):

```sql
ALTER TABLE page_classification ADD COLUMN IF NOT EXISTS axes JSONB;
```

The existing columns are reused: `page_type`, `page_subtype` and `model_type`
(L1 > L2 > L3) take the v24 names, `classification_category` the canon's
codeability (Codable / Non-Codable / Discharge — never the model's), `confidence` L1's confidence, and `decided_by` reads
`axes:<L1>/<L2>/<L3>` (e.g. `axes:model/model/canon`).

---

## 6. See it work

Run the classification stage on a chart (no new OCR):

```bash
curl -X POST localhost:8001/api/charts/run -H 'Content-Type: application/json' \
  -d '{"input_type":"local","input_path":"/data/inbox","chart_name":"<chart>","only":["page_subtype"],"skip_ocr":true}'
```

Then read `imaging/<chart>_codeable.csv`, or:

```sql
SELECT p.page_name, c.page_type, c.page_subtype, c.decided_by,
       c.axes->>'specialty'   AS specialty,
       c.axes->>'codeability' AS codeability,
       c.axes->>'sources'     AS sources,
       c.axes->>'route'       AS route
FROM page_classification c JOIN page_list p ON p.id = c.page_id
WHERE c.chart_id = <id> ORDER BY p.page_number;
```

`sources` reads L1 / L2 / L3 / specialty / codeability: `model`, `canon`, or
`-` (nothing cleared its gate). With the model installed most pages should show
`model` first; `canon/canon/canon/…` everywhere means the model is not loading
(§3). `axes->'prediction'` holds the model's raw answer, so a rejected value
can still be audited.

To see the downstream stages pick the new names up, run the rest of the chain
from there (`"only": ["page_subtype", "continuity", "encounter_type",
"imaging_final"]`). They match v24 names — see
[PAGE_CLASSIFICATION.md §4.6](PAGE_CLASSIFICATION.md#46-page-axes-v24--the-page-type-written),
"Downstream, in v24 names".

---

## 7. When the labels change

1. Retrain (`train_axes.py`) and repeat §2–§4.
2. Rebuild the keyword canon from the new labels. It writes all three copies,
   including the pipeline's:

   ```bash
   cd training/taxonomy
   python build_canon.py --check   # lists the copies that are out of date
   python build_canon.py           # rewrites them
   ```

---

## 8. Going back

- **Remove the page-axes model:** delete `encoder/`, `heads.pt` and
  `label_space.json` from `models/page-family/page-axes/` (keep `README.md`).
  The v24 canon decides alone again.
- **The old BERT** in `models/page-family-backup/2026-10-16/` is a backup and
  is not read. Copying it to `models/page-family/bert/` only fills the ladder's
  raw `bert_*` columns again; it no longer decides `page_type`.
