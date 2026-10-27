#!/usr/bin/env bash
# Copy review-ui's committed backend and frontend into the client UI repo
# (github.com/abhinavdg-ever/ui-temp, checked out at review-ui-client/).
#
#   utilities/sync_client_ui.sh [CLIENT_DIR]      # default: review-ui-client
#
# Only what is committed at HEAD is copied, so commit review-ui first. The client
# repo's own files (docker-compose.yml, .env.example, README.md, data/) are left
# alone. Then review the diff, commit and push in CLIENT_DIR (docs/CLIENT_INSTANCE.md).
set -euo pipefail

repo="$(git -C "$(dirname "$0")/.." rev-parse --show-toplevel)"
client="${1:-$repo/review-ui-client}"

if [ ! -d "$client/.git" ]; then
  echo "not a git checkout: $client (git clone https://github.com/abhinavdg-ever/ui-temp.git $client)" >&2
  exit 1
fi

rm -rf "$client/backend" "$client/frontend"
git -C "$repo" archive HEAD review-ui/backend review-ui/frontend \
  | tar -x --strip-components=1 -C "$client"
find "$client" -name '._*' -delete

echo "copied review-ui at $(git -C "$repo" rev-parse --short HEAD) into $client"
git -C "$client" status --short | tail -n 20
