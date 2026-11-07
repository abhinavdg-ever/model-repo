# core-pipeline/outputs/ — scratch space

Nothing here is a deliverable. Results live in the database and Azure Blob; review-ui never
reads this folder.

- `charts/<chart>/` — a chart's working folder (pages, OCR JSON, staging, CSVs). Kept so a
  partial re-run (`only: [...]`) can reuse it; deleted after `OUTPUTS_RETENTION_DAYS` (7)
  days without a run, checked on the first trigger of each day (`core-pipeline/apps/worker/jobs/outputs_cleanup.py`).
- `metadata/` — manifest CSVs the manifest sweep fetched or you dropped in.
- Debug files (`*_DEBUG=true`, `<chart>/debug/`) are written inside the chart folder.

Contents are git-ignored; only this README is tracked.
