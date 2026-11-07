# Deploying to the client: GitHub → Windows laptop → Azure DevOps → Ubuntu VM

Everything is developed on the Mac and pushed to **GitHub**, `training/`, `scripts/` and
`tests/` included. The client side has no git access to GitHub, so a release goes:

```
Mac ──git push──▶ GitHub ──Download ZIP──▶ Windows laptop ──push──▶ Azure DevOps ──pull──▶ Ubuntu VM
                  (everything)             prepare_for_devops.ps1   (runtime only)         (16 CPU / 64 GB)
```

## What goes where

| | GitHub | Azure DevOps / VM |
|---|---|---|
| `core-pipeline/`, `review-ui/`, `schema/`, `utilities/`, `docs/` (the kept ones) | yes | yes |
| `training/`, `scripts/`, `tests/`, `deploy/` | yes | **no** |
| `.env` files | never | made on each host from `.env.example`, never pushed |
| Big model weights (`*.safetensors`, `*.onnx`, `*.bin`, `*.pth`, NER, RapidOCR, Docling) | never | copied onto the VM by hand, never pushed |
| Small model files (`*.joblib` heads, `heads.pt`, the hw `.pkl`) | yes | yes |
| `review-ui-client/` | its own repo (ui-temp) | deployed on its own |

`devops.gitignore` is the `.gitignore` for the DevOps repo. `prepare_for_devops.ps1` copies
it in as `.gitignore`, so a stray `training/` folder can never be pushed by accident.

## 1. Windows laptop (PowerShell)

```powershell
# a. On github.com: Code → Download ZIP. Extract it, e.g. to Downloads\model-repo-main
Expand-Archive "$env:USERPROFILE\Downloads\model-repo-main.zip" "$env:USERPROFILE\Downloads" -Force

# b. Copy it into the DevOps working folder, minus training / scripts / tests.
#    The target's .env files, model weights, logs and chart data are left alone.
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\Downloads\model-repo-main\deploy\prepare_for_devops.ps1" `
    -Source "$env:USERPROFILE\Downloads\model-repo-main" `
    -Target "C:\work\advantmed-devops"

# c. Push C:\work\advantmed-devops to Azure DevOps as you do today.
```

The first time, if the DevOps repo already holds `training/`, `scripts/` or `tests/`, delete
them there once. A `.gitignore` stops new files from being added, but files already in the
repo stay until they are removed.

## 2. Ubuntu VM (bash)

```bash
cd ~/advantmed-imaging-pipeline              # the DevOps checkout
git pull                                     # from Azure DevOps

# Database: safe to run on every release (skips what is already there)
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f schema/migrate.sql

# core-pipeline
cd core-pipeline
docker compose up -d --build
curl -fsS localhost:8001/ready && curl -s localhost:8001/health | python3 -m json.tool | head -40

# review-ui
cd ../review-ui
docker compose up -d --build
curl -fsS localhost:3000/docs >/dev/null && echo review-ui up
```

`DATABASE_URL` is in `core-pipeline/.env`. To load it into the shell:
`export $(grep -E '^DATABASE_URL=' core-pipeline/.env | xargs)`.
Without `psql` on the VM, see `schema/README.md` for the Docker one-liner.

## 3. After a release

- A change that needs a re-run is named in the release note, for example
  `{"only": ["ocr_final1", "section_headers"]}`.
- Logs are under `core-pipeline/logs/` and are deleted after 7 days.
- A new `.env` setting is listed in `.env.example`. Copy the line into the VM's `.env`;
  a missing one uses the default shown there.
