# Image preprocessing drop (quality / HW / orientation)

How the teammate `image_preprocessing` zip maps onto this repo, what was
applied, and what you still need to do on each machine.

## What the zip is

Standalone package `image_preprocessing/` — page quality, printed vs
handwritten, OSD rotation, tilt, mirror measurement. Same problem space as
**stage 1** (`ocr_quality` → `stages/quality_rotation_hw.py` +
`stages/lib/imaging/`).

| Zip path | Our path |
|---|---|
| `quality/quality_analyzer.py` | `stages/lib/imaging/quality_analyzer.py` (already ported; keep ours — it adds `quality_tag` / `score_01`) |
| `quality/quality_label_postprocess.py` | **`stages/lib/imaging/quality_label_postprocess.py`** |
| `document_type/hw_printed.py` | `stages/lib/imaging/hw_printed.py` (same ConvNeXt API) |
| `document_type/models/page_printed_handwritten_convnext_tiny.pth` | **`models/hw/handwritten_printed_convnext_tiny.pth`** (preferred — we keep this canonical name) |
| Prior weight (pre–2026-09-23) | `models/hw/handwritten_printed_convnext_tiny_backup.pth` |
| `document_type/models/metadata.json` | `models/hw/metadata.json` |
| `orientation/*` | `stages/lib/imaging/rotation.py` + `osd.py` (OSD-only coarse rotation, no geometric fallback, mirror measured not flipped) |
| `document_type/existing_classifier_adapter.py` | **not imported** — their package loads via sibling `hw_printed_rf_test.page_classifier`; we load the same `.pth` with our `hw_printed.load_model` (compatible checkpoint: `model_state_dict` + ConvNeXt-Tiny) |

## Drops received

| Zip | What changed for us |
|---|---|
| First `image_preprocessing.zip` | Same SHA as then-current tiny weights. Applied **Handwritten + High → Medium** label rule only. |
| `image_preprocessing (1).zip` (2026-09-23) | **New weight** (zip name `page_printed_handwritten_convnext_tiny.pth`, SHA `042064d6…`). Stored here as `handwritten_printed_convnext_tiny.pth`; previous file kept as `…_tiny_backup.pth`. Still pure ConvNeXt — metadata recommends *not* fusing ink handcrafted features. Adapter still references `hw_printed_rf_test` (not in zip); not required here. |

## What was applied in this repo

1. `quality_label_postprocess.py` + call from stage 1 (Handwritten + High → Medium).
2. New drop installed as `handwritten_printed_convnext_tiny.pth`; prior as
   `handwritten_printed_convnext_tiny_backup.pth`.
3. Documented in `docs/LOGIC.md`, `docs/API.md`, `docs/ARCHITECTURE.md`.

## What you do to roll out

### 1. Host / Docker weights

```bash
# On the server (compose mounts MODELS_HOST_PATH → /app/core-pipeline/models)
mkdir -p core-pipeline/models/hw
# Prefer: rename the zip's page_*.pth to the canonical name
cp /path/to/page_printed_handwritten_convnext_tiny.pth \
  core-pipeline/models/hw/handwritten_printed_convnext_tiny.pth
cp /path/to/metadata.json core-pipeline/models/hw/
# optional: keep the previous ConvNeXt as backup
# mv core-pipeline/models/hw/handwritten_printed_convnext_tiny.pth \
#    core-pipeline/models/hw/handwritten_printed_convnext_tiny_backup.pth
# then copy the new file into handwritten_printed_convnext_tiny.pth
```

Confirm: `curl -s localhost:8001/health | jq .hw_model` (path should end in
`handwritten_printed_convnext_tiny.pth`).

### 2. Pull this code + restart

```bash
git pull
cd core-pipeline && docker compose up -d --build
# or local: restart python cli.py serve
```

### 3. Re-run quality on charts that already finished OCR

Reuse OCR; refresh quality/HW; only reopen OCR when the gate flips:

```bash
curl -X POST localhost:8001/api/charts/batch-run -H 'Content-Type: application/json' \
  -d '{
    "blob_container": "imaging-pipeline",
    "blob_read_path": "Raw_Input/Run1/Batch1/DEID_PNGs",
    "blob_write_path": "Processed/Run1/Batch1",
    "force": false,
    "skip_ocr": true,
    "only": ["ocr_quality", "page_subtype", "encounter_type", "page_sequencing"],
    "workers": 2
  }'
```

Or smoke with `"sample": 5` first. Full detail: [`HOW_TO_RUN.md` §4B](HOW_TO_RUN.md).

### 4. Optional local check without Postgres

```bash
python cli.py run \
  --local-read-path /data/inbox \
  --folder-name 52743839_44976074 \
  --test-mode --through ocr_quality
# → data/folders/52743839_44976074-test/
```

## Still not needed from teammates

Their README still points at:

```
hw_printed_rf_test/page_classifier.py
```

That package is **not** inside either zip. We do not need it: the `.pth` is a
standard ConvNeXt checkpoint and loads through
`stages/lib/imaging/hw_printed.py`. If they later ship a true fused hybrid head
or different checkpoint format, re-evaluate wiring `existing_classifier_adapter`.
