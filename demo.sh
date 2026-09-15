#!/usr/bin/env bash
# One command to see the whole thing working, with no cameras and no installs.
#
#   ./demo.sh
#
# Starts a simulated fleet of SNMP cameras, backfills some history so the charts
# have something to draw, and serves the dashboard. Ctrl-C stops everything.

set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
PORT="${PORT:-8080}"
BASE_PORT="${BASE_PORT:-11610}"
COUNT="${COUNT:-8}"
CONFIG="demo-cameras.json"
DB="demo.db"

cleanup() {
  echo
  echo "stopping…"
  if [[ -n "${SIM_PID:-}" ]]; then
    kill "$SIM_PID" 2>/dev/null || true
    wait "$SIM_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "error: '$PYTHON' not found. Install Python 3.9+, or set PYTHON=/path/to/python3" >&2
  exit 1
fi
"$PYTHON" - <<'PY' || exit 1
import sys
if sys.version_info < (3, 9):
    sys.exit(f"error: Python 3.9+ required, found {sys.version.split()[0]}")
PY

# Write the config synchronously first. Doing this inside the backgrounded
# simulator would race: the next step reads the file, and on a cold start the
# simulator may not have written it yet.
echo "→ writing $CONFIG"
"$PYTHON" tools/fake_camera.py --count "$COUNT" --base-port "$BASE_PORT" \
  --write-config "$CONFIG" --write-config-only >/dev/null

# Point it at a demo database and a brisk poll interval.
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

echo "→ starting $COUNT simulated cameras on udp/${BASE_PORT}-$((BASE_PORT + COUNT - 1))"
"$PYTHON" tools/fake_camera.py --count "$COUNT" --base-port "$BASE_PORT" >/dev/null &
SIM_PID=$!

# Wait until a camera actually answers, rather than guessing with a sleep.
echo "→ waiting for the simulated fleet to come up"
for attempt in $(seq 1 40); do
  if ! kill -0 "$SIM_PID" 2>/dev/null; then
    echo "error: the simulator exited. Re-run without '>/dev/null' to see why:" >&2
    echo "  $PYTHON tools/fake_camera.py --count $COUNT --base-port $BASE_PORT" >&2
    exit 1
  fi
  if "$PYTHON" - "$BASE_PORT" <<'PY'
import sys, pathlib
sys.path.insert(0, str(pathlib.Path.cwd()))
from camwatch import mibs
from camwatch.snmp import Session, SnmpConfig
try:
    with Session(SnmpConfig(host="127.0.0.1", port=int(sys.argv[1]),
                            community="public", timeout=0.5, retries=0)) as s:
        sys.exit(0 if s.get_one(mibs.SYS_NAME) else 1)
except Exception:
    sys.exit(1)
PY
  then
    break
  fi
  if [[ "$attempt" -eq 40 ]]; then
    echo "error: simulated cameras never answered on udp/$BASE_PORT" >&2
    echo "  (something else may be using that port — try BASE_PORT=21610 ./demo.sh)" >&2
    exit 1
  fi
  sleep 0.25
done

echo "→ priming the database with one poll"
"$PYTHON" -m camwatch.server --config "$CONFIG" --once >/dev/null

echo "→ backfilling 48h of history so the trend chart has something to show"
"$PYTHON" tools/seed_history.py --db "$DB" --hours 48 --interval 300 >/dev/null

echo
echo "→ dashboard: http://127.0.0.1:${PORT}/"
echo
"$PYTHON" -m camwatch.server --config "$CONFIG" --port "$PORT"
