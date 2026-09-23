# Review UI

Vite + React frontend and FastAPI backend for chart OCR/imaging review.

## Modes

| `DATA_MODE` | UI label | OCR / imaging source | Page images |
|-------------|----------|----------------------|-------------|
| `local` | Local Mode | `data/folders/*/ocr` + `imaging/*.csv` | `data/folders/*/pages` |
| `production` (alias: `postgres`) | Production Mode | Postgres v1 tables | Azure Blob via `chart_list.blob_container` + `blob_path` (Processed `output_path` fallback) |

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

Restart the backend after changing `.env`. The top bar shows **Local Mode** or
**Production Mode** from `/api/config`.

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

Docker: host ports **4000** (API) / **4001** (web). Compose is **Local Mode
only** for now (`DATA_MODE=local` pinned).

```bash
cp .env.example .env
docker compose up -d --build
```

`DATA_ROOT` defaults to `./data/folders` — core-pipeline writes there.
