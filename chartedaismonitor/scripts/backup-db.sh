#!/usr/bin/env bash
# Run from the chartedaismonitor directory. Requires Postgres reachable (127.0.0.1:5432).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
OUT="${1:-backup-$(date +%F).sql}"
docker compose exec -T db pg_dump -U ais_user ais >"$OUT"
echo "Wrote $OUT"
