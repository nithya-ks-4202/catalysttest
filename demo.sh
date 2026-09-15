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
PIDFILE=".demo-simulator.pid"

cleanup() {
  echo
  echo "stopping…"
  if [[ -n "${SIM_PID:-}" ]]; then
    kill "$SIM_PID" 2>/dev/null || true
    wait "$SIM_PID" 2>/dev/null || true
  fi
  rm -f "$PIDFILE"
}
trap cleanup EXIT INT TERM

# If a previous run was killed before its trap fired, its simulator is still
# holding UDP ports. Clean that up rather than leaking one process per run.
if [[ -f "$PIDFILE" ]]; then
  STALE="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [[ -n "$STALE" ]] && kill -0 "$STALE" 2>/dev/null; then
    # Only kill it if it really is our simulator, never an unrelated PID.
    if ps -p "$STALE" -o command= 2>/dev/null | grep -q "fake_camera.py"; then
      echo "→ stopping a simulator left over from a previous run (pid $STALE)"
      kill "$STALE" 2>/dev/null || true
      # Wait for it to actually exit, so its ports are free again and this run
      # can use the default range rather than stepping past it.
      for _ in 1 2 3 4 5 6 7 8 9 10; do
        kill -0 "$STALE" 2>/dev/null || break
        sleep 0.2
      done
    fi
  fi
  rm -f "$PIDFILE"
fi

# Start from a clean demo database every run. It is throwaway data that gets
# re-seeded below, and reusing it both piles up duplicate synthetic history and
# inherits any broken WAL state left by a run that was killed outright.
rm -f "$DB" "$DB-wal" "$DB-shm"

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
#
# --auto-port steps past a busy range (a stale simulator from a previous run is
# the usual cause) and reports the range it settled on, so the config we just
# wrote and the fleet we serve always agree.
CHOSEN_PORT="$("$PYTHON" tools/fake_camera.py --count "$COUNT" \
  --base-port "$BASE_PORT" --auto-port --print-base-port \
  --write-config "$CONFIG" --write-config-only)"

if [[ "$CHOSEN_PORT" != "$BASE_PORT" ]]; then
  echo "→ udp/${BASE_PORT} was busy — using udp/${CHOSEN_PORT} instead"
fi
BASE_PORT="$CHOSEN_PORT"
echo "→ wrote $CONFIG"

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
echo "$SIM_PID" > "$PIDFILE"

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

# Same treatment for the dashboard's TCP port, so a leftover server (or any
# other app on 8080) doesn't stop the demo either.
WEB_PORT="$("$PYTHON" - "$PORT" <<'PY'
import socket, sys
port = int(sys.argv[1])
for candidate in range(port, port + 40):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", candidate))
        print(candidate)
        break
    except OSError:
        continue
    finally:
        sock.close()
else:
    sys.exit(f"no free TCP port between {port} and {port + 39}")
PY
)"
if [[ "$WEB_PORT" != "$PORT" ]]; then
  echo "→ tcp/${PORT} was busy — serving on ${WEB_PORT} instead"
fi

echo
echo "→ dashboard: http://127.0.0.1:${WEB_PORT}/"
echo
"$PYTHON" -m camwatch.server --config "$CONFIG" --port "$WEB_PORT"
