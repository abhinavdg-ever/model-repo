# VM checks

Two checklists for the Docker VM:

- **Part A** finds out why the VM scores lower than the Windows laptop (plain uvicorn)
  on the same files.
- **Part B** confirms the v004 extraction models are in place and running.

Run every VM command from `core-pipeline/` on the VM. Each laptop command is the
PowerShell equivalent, run from `core-pipeline\` with the venv active. Paste both outputs
side by side; any line that differs is a candidate cause.

---

## Part A — VM vs laptop accuracy

Most likely causes first. Stop when you find the gap.

### A1. Is the VM running the current code?

The code and the keyword canons (`stages/lib/keyword-canon/`) are copied into the image
at build time. `git pull` + `docker compose up -d` without `--build` keeps the old image.

```bash
# VM
git log -1 --oneline
docker image inspect advantmed/core-pipeline:latest --format '{{.Created}}'
docker compose exec api sh -c 'ls -l --time-style=+%F_%T stages/lib/keyword-canon/*.json | head'
```

```powershell
# laptop
git log -1 --oneline
```

- Commits differ → `git pull` on the VM.
- The image is older than the last pull → rebuild:

  ```bash
  docker compose up -d --build
  ```

### A2. Which models actually loaded?

Weights are not in the image or in git. A missing one does not stop a run: the stage
falls back to a weaker engine.

```bash
# VM
curl -s localhost:8001/health | python3 -m json.tool
```

```powershell
# laptop
curl.exe -s localhost:8001/health | python -m json.tool
```

Compare these keys. Each should say `"ready": true` on both machines, with the same
`engine` / `method`:

| Key | If not ready on the VM |
|---|---|
| `hw_model` | handwriting check uses the RandomForest backup |
| `blank_junk_model` | blank/junk vote runs on keywords alone (`engine: keywords`) |
| `page_family_model` | page type falls back to keywords |
| `rapidocr_models` | no Final OCR 1 |
| `docling_final1` | no Final OCR 1 |
| `member_ner` | no wrong-member check, so nothing can be Rejected |
| `extraction` | no key/value extraction (reason names the missing folder) |
| `azure_document_intelligence` | no Final OCR 2; handwritten pages get no pass-2 verdict |
| `dos_llm` | DOS is rules-only (compose turns it on by default) |

Then list the model folders on both machines:

```bash
# VM
docker compose exec api sh -c 'for d in models/*/ models/*/*/; do echo "$d $(ls "$d" | wc -l)"; done'
docker compose exec api sh -c 'ls -la models/hw models/blank-junk models/blank-junk/bert models/page-family/bert models/ner models/layout_heron models/semantic-model models/rapidocr'
```

```powershell
# laptop
Get-ChildItem models -Recurse -File | Group-Object DirectoryName | Select-Object Count, Name
```

Anything the laptop has that the VM lacks: copy it into the VM's `core-pipeline/models/`.
That folder is mounted into the container, so a restart is enough, not a rebuild:

```bash
docker compose restart api
```

Most often missing: `blank-junk/bert/`, `page-family/bert/model.safetensors`, `ner/`,
`hw/handwritten_printed_convnext_tiny.pth`, `semantic-model/`, `layout_heron/`.

### A3. Same library versions?

`docling>=2.0.0` has no upper bound and `requirements-basic.txt` pins nothing, so every
Docker build installs the newest releases. A different Docling version gives different
Final OCR 1 text, which changes section headers, DOS, member and page type.

```bash
# VM
docker compose exec api pip freeze > vm_freeze.txt
grep -iE '^(docling|docling-|rapidocr|onnxruntime|torch|transformers|gliner|sentence-transformers|lightgbm|xgboost|scikit-learn|opencv|pytesseract|numpy|pandas)' vm_freeze.txt
```

```powershell
# laptop
pip freeze > laptop_freeze.txt
Select-String -Path laptop_freeze.txt -Pattern '^(docling|rapidocr|onnxruntime|torch|transformers|gliner|sentence-transformers|lightgbm|xgboost|scikit-learn|opencv|pytesseract|numpy|pandas)'
```

Any version that differs, especially `docling*`, is a likely cause. The fix is to pin the
requirements files to the laptop's versions and rebuild. Paste both lists back and the
pins can be written for you.

### A4. Same Tesseract?

Preliminary OCR feeds blank/junk and page type, and the page-type model was trained on
Tesseract text.

```bash
# VM
docker compose exec api tesseract --version
docker compose exec api tesseract --list-langs
docker compose exec api sh -c 'ls -la /usr/share/tesseract-ocr/*/tessdata/'
```

```powershell
# laptop
& $env:TESSERACT_CMD --version
& $env:TESSERACT_CMD --list-langs
Get-ChildItem "C:\Program Files\Tesseract-OCR\tessdata\*.traineddata" | Select-Object Name, Length
```

A different major/minor version, or a different `eng.traineddata` size (fast vs best),
means different OCR text.

### A5. Same settings?

```bash
# VM
docker compose exec api env | sort | grep -E 'ENABLED|_MODEL|MODEL_|_PATH|_DIR|SKIP_OCR|WORKERS|OMP_|DI_FEATURES|DEPLOYMENT' 
```

```powershell
# laptop
Get-Content .env | Select-String -Pattern 'ENABLED|_MODEL|MODEL_|_PATH|_DIR|SKIP_OCR|WORKERS|OMP_|DI_FEATURES|DEPLOYMENT'
```

Look for a switch that differs: `DOS_LLM_ENABLED`, `MEMBER_NER_ENABLED`, `MEMBER_NER_MODEL_ID`,
`SKIP_OCR`, `AZURE_DI_FEATURES`, `AZURE_OPENAI_DEPLOYMENT`, or a model path pointing somewhere else.

### A6. Same ground truth, same run?

The accuracy view scores each machine against its **own** database. It also only counts
files last run on or after the `MIN_LAST_RUN` date.

```bash
# VM, and the same on the laptop against its DATABASE_URL
psql "$DATABASE_URL" -c "SELECT count(*) AS rows, count(DISTINCT chart_name) AS charts, max(updated_at) FROM page_ground_truth;"
psql "$DATABASE_URL" -c "SELECT chart_name, count(*) FROM page_ground_truth GROUP BY 1 ORDER BY 1;"
```

- Counts differ → the VM's labels are older or partial. Reload the same ground-truth
  sheet on the VM.
- Also confirm the VM run did not reuse old OCR (`skip_ocr: true` or `SKIP_OCR=true`)
  from before the latest code.

### A7. Narrow it to a stage

If A1–A6 all match, compare one chart page by page. In the review UI, open the same
chart on both machines and look at which column differs first:

| First difference | Points to |
|---|---|
| Orientation / Handwritten | stage 1: `hw_model`, OpenCV |
| Blank / Junk | Tesseract (A4), or `blank_junk_model` |
| OCR text | Docling version (A3), `rapidocr_models` |
| Page type | `page_family_model` |
| Member / DOS / signature | `member_ner`, `dos_llm`, `extraction` |

---

## Part B — v004 extraction models on the VM

Do this **after** the v004 code is pushed and pulled.

### B1. Put the files in place

Copy the two folders from the laptop into the VM's mounted models folder. Keep the
existing v002 files: provider name still uses `ranker_provider_name.txt`.

```
core-pipeline/models/kv-extraction/
├── manifest.json, features.json, thresholds.json, ranker_provider_name.txt, …   (v002, keep)
├── KV_v004/        features.json manifest.json metrics.json thresholds.json
│                   ranker_dob.txt ranker_dos.txt ranker_electronic_signature.txt
│                   ranker_member_id.txt ranker_name.txt ranker_page_no.txt        (10 files)
└── Heading_v004/   features.json manifest.json metrics.json thresholds.json
                    level_heading_heron.txt ranker_heading_heron.txt
                    vocab_heading_heron.json                                       (7 files)
```

### B2. Check the copies are byte-identical

```bash
# VM
cd models/kv-extraction && sha256sum KV_v004/* Heading_v004/* ranker_provider_name.txt; cd -
```

```powershell
# laptop
Get-FileHash models\kv-extraction\KV_v004\*, models\kv-extraction\Heading_v004\*, models\kv-extraction\ranker_provider_name.txt | Select-Object Hash, Path
```

Every hash must match. A truncated or re-encoded `ranker_*.txt` loads but scores wrongly.
Copying through an editor or a chat window can turn LF into CRLF, so copy the files as files.

### B3. Rebuild and restart

The loader and the version pin are code, so they need a rebuild. The model folders are
mounted, so they do not.

```bash
git pull
docker compose up -d --build
docker compose logs --tail=80 api | grep -iE 'extraction|v004|v002|kv'
```

### B4. Confirm v004 is what runs

```bash
curl -s localhost:8001/health | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin)['extraction'], indent=2))"
```

Expect `"ready": true` and `"model_version": "v004"`. When not ready, `reason` names the
missing folder or file.

```bash
docker compose exec api python -c "
from stages.lib.extraction.training.model import load_version
v = load_version('v004')
print(sorted(v.fields))
"
```

Expect `dob, dos, electronic_signature, heading_heron, member_id, name, page_no` from v004,
plus `provider_name` from v002.

### B5. Run one chart and check the output

Use a chart that has ground truth and that the laptop has already run on v004.

```bash
curl -X POST localhost:8001/api/charts/run -H 'Content-Type: application/json' \
  -d '{"input_type":"blob","container_name":"imaging-pipeline",
       "input_path":"<same input_path as the laptop>","chart_name":"<chart>",
       "output_path":"<same output_path>","skip_ocr":true}'
```

`skip_ocr: true` reuses the OCR already on disk, so only the non-OCR stages rerun and
there is no stage-5 bill.

When it finishes:

- `docker compose logs api | grep 'extraction'` shows `(model v004)` for the chart.
- The chart's extraction staging JSON (`model_version` field) says `v004`.
- In the review UI, Member Name, DOB, Member ID, DOS, Signature, Page No and Provider Name
  are filled. Provider Name blank on every page means the v002 provider ranker did not load.
- The accuracy view matches the laptop's for that chart. If it does not, go back to Part A:
  the remaining gap is environment, not the model.

### B6. What to paste back if something is off

- The `extraction` block from `/health`.
- The output of B2 (both machines).
- The `load_version('v004')` field list from B4.
- `docker compose logs --tail=200 api` from the run.
