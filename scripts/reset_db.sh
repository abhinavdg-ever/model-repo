#!/usr/bin/env bash
# Clear the public schema and re-apply the latest schema/v1.sql + schema/v2.sql.
# Destructive — all chart/OCR/member/DOS rows are wiped.
#
# Usage:
#   ./scripts/reset_db.sh              # prompts for confirmation
#   ./scripts/reset_db.sh --yes        # no prompt
#   DATABASE_URL=postgresql://… ./scripts/reset_db.sh --yes
#
# Applies, in order:
#   schema/clear_schema.sql
#   schema/v1.sql
#   schema/v2.sql
#
# DATABASE_URL is read from the environment, else core-pipeline/.env.
# Accepts postgresql:// or postgresql+psycopg://.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
YES=0
for arg in "$@"; do
  case "$arg" in
    -y|--yes) YES=1 ;;
    -h|--help)
      sed -n '2,16p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown argument: $arg (use --yes or --help)" >&2
      exit 2
      ;;
  esac
done

if [[ -z "${DATABASE_URL:-}" && -f "$ROOT/core-pipeline/.env" ]]; then
  DATABASE_URL="$(grep -E '^[[:space:]]*DATABASE_URL=' "$ROOT/core-pipeline/.env" | tail -1 | cut -d= -f2- | tr -d '\r' | sed 's/^["'\'']//;s/["'\'']$//')"
fi

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL is not set. Export it or put it in core-pipeline/.env" >&2
  exit 1
fi

if ! command -v psql >/dev/null 2>&1; then
  echo "psql not found on PATH" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required to parse DATABASE_URL" >&2
  exit 1
fi

eval "$(
  DATABASE_URL="$DATABASE_URL" python3 - <<'PY'
import os, shlex
from urllib.parse import urlparse, urlunparse

raw = os.environ["DATABASE_URL"].strip().replace("postgresql+psycopg://", "postgresql://", 1)
u = urlparse(raw)
db = (u.path or "/").lstrip("/").split("/")[0] or "imaging_outputs"
target = urlunparse((u.scheme, u.netloc, f"/{db}", "", "", ""))
print(f"DB_NAME={shlex.quote(db)}")
print(f"TARGET_URL={shlex.quote(target)}")
PY
)"

echo "Will CLEAR public schema in database: ${DB_NAME}"
echo "  then apply clear_schema.sql → v1.sql → v2.sql"
if [[ "$YES" -ne 1 ]]; then
  read -r -p "Type the database name to confirm: " confirm
  if [[ "$confirm" != "$DB_NAME" ]]; then
    echo "Aborted." >&2
    exit 1
  fi
fi

echo "→ schema/clear_schema.sql"
psql "$TARGET_URL" -v ON_ERROR_STOP=1 -f "$ROOT/schema/clear_schema.sql" >/dev/null

echo "→ schema/v1.sql"
psql "$TARGET_URL" -v ON_ERROR_STOP=1 -f "$ROOT/schema/v1.sql" >/dev/null

echo "→ schema/v2.sql"
psql "$TARGET_URL" -v ON_ERROR_STOP=1 -f "$ROOT/schema/v2.sql" >/dev/null

echo "→ verify pipeline_stage"
psql "$TARGET_URL" -v ON_ERROR_STOP=1 -c \
  "SELECT count(*) AS stages FROM pipeline_stage;"

echo "Done. ${DB_NAME} cleared and reloaded from latest v1 + v2."
