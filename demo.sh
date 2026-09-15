#!/usr/bin/env bash
# One command to see the whole thing working, with no cameras and no installs.
#
#   ./demo.sh
#
# Starts a simulated fleet of SNMP cameras, backfills some history so the charts
# have something to draw, and opens the dashboard. Ctrl-C stops everything.

set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
PORT="${PORT:-8080}"
BASE_PORT="${BASE_PORT:-11610}"
CONFIG="demo-cameras.json"
DB="demo.db"

cleanup() {
  echo
  echo "stopping…"
  [[ -n "${SIM_PID:-}" ]] && kill "$SIM_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "→ starting 8 simulated cameras on udp/${BASE_PORT}-$((BASE_PORT + 7))"
"$PYTHON" tools/fake_camera.py --count 8 --base-port "$BASE_PORT" \
  --write-config "$CONFIG" >/dev/null &
SIM_PID=$!
sleep 1

# Point the generated config at a demo database and a brisk poll interval.
"$PYTHON" - "$CONFIG" "$DB" <<'PY'
import json, sys
path, db = sys.argv[1], sys.argv[2]
with open(path) as fh:
    config = json.load(fh)
config["database_path"] = db
config["poll_interval_seconds"] = 10
with open(path, "w") as fh:
    json.dump(config, fh, indent=2)
PY

echo "→ priming the database with one poll"
"$PYTHON" -m camwatch.server --config "$CONFIG" --once >/dev/null

echo "→ backfilling 48h of history so the trend chart has something to show"
"$PYTHON" tools/seed_history.py --db "$DB" --hours 48 --interval 300 >/dev/null

echo "→ dashboard: http://127.0.0.1:${PORT}/"
echo
"$PYTHON" -m camwatch.server --config "$CONFIG" --port "$PORT"
