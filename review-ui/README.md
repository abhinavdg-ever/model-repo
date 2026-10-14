# Review UI

Vite + React frontend and FastAPI backend for chart OCR/imaging review.

## Modes

| `DATA_MODE` | UI label | OCR / imaging source | Page images |
|-------------|----------|----------------------|-------------|
| `local` | Local Mode | `data/folders/*/ocr` + `imaging/*.csv` | `data/folders/*/pages` |
| `production` (alias: `postgres`) | Production Mode | Postgres v1 tables | Azure Blob: Processed `chart_list.output_path` (`corrected-pages/`, `pages/`) first, then Raw_Input `blob_path` |

With a usable `DATABASE_URL`, each browser can switch between the two from the
**Local / DB** control in the top bar (a cookie, so page images follow it).
`DATA_MODE` is what a browser that has not chosen sees. Processed holds the
original `pages/` only when the pipeline wrote with `write_mode=all_files`;
otherwise originals come from Raw_Input.

```bash
# Local Mode (default)
DATA_MODE=local

# Production Mode — OCR/imaging from Postgres; images from blob_path
DATA_MODE=production
DATABASE_URL=postgresql://USER:PASSWORD@HOST:5432/imaging_outputs
BLOB_ACCOUNT_URL=https://<account>.blob.core.windows.net
# or: AZURE_STORAGE_ACCOUNT_NAME=<account>
# AZURE_TENANT_ID= / AZURE_CLIENT_ID= / AZURE_CLIENT_SECRET=  (or MI / az login)
```

Restart the backend after changing `.env`.

## Run locally

```bash
# Backend
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp ../.env.example ../.env
uvicorn app.main:app --reload --host 127.0.0.1 --port 8002

# Frontend
cd ../frontend
npm install
npm run dev
```

Docker: host ports **4000** (API) / **4001** (web). Compose reads `DATA_MODE`
from `.env` (default `local`); the top-bar switch works whenever
`DATABASE_URL` is set.

```bash
cp .env.example .env
docker compose up -d --build
```

`DATA_ROOT` defaults to `./data/folders` — core-pipeline writes there.
