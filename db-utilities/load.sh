#!/usr/bin/env bash
# Load one CSV / XLSX (or a folder of them) into the database: Postgres AND Cosmos by default.
#
#   ./load.sh manifest      /path/to/metadata_R1_B1.csv
#   ./load.sh ground-truth  /path/to/ground_truth.csv
#   ./load.sh manifest      /path/to/file.csv --to postgres      # or --to cosmos
#
# Templates with the expected columns: templates/manifest_template.csv,
# templates/ground_truth_template.csv. Settings: db-utilities/.env.
# With Docker (the VM) the file is copied into ./input and loaded by the db-utilities
# image; without Docker (the Mac) it runs with .venv (created on first use).
set -euo pipefail
cd "$(dirname "$0")"
job="${1:-}"; src="${2:-}"; shift 2 || true
to="both"
if [ "${1:-}" = "--to" ]; then to="${2:?--to needs postgres, cosmos or both}"; fi
case "$job" in manifest|ground-truth) ;; *) echo "usage: ./load.sh manifest|ground-truth FILE_OR_FOLDER [--to postgres|cosmos|both]"; exit 2;; esac
[ -e "$src" ] || { echo "not found: $src"; exit 1; }
[ -f .env ] || { echo "db-utilities/.env is missing: cp .env.example .env and fill it in"; exit 1; }

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
  name="$(basename "$src")"
  rm -rf "input/$name"; cp -R "$src" "input/$name"
  docker compose run --rm db-utilities "$job" --local "/input/$name" --to "$to"
else
  if [ ! -x .venv/bin/python ]; then
    python3.12 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
  fi
  .venv/bin/python -m dbutil "$job" --local "$src" --to "$to"
fi
