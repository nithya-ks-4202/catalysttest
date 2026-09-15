#!/usr/bin/env python3
"""Backfill plausible history into the database.

A fresh install has no history, so the trend chart and the "days until full"
projection have nothing to show until it has been running for a while. This
writes synthetic past samples for the cameras already in the database, so a
demo looks alive immediately.

    python3 tools/seed_history.py --db camwatch.db --hours 48

Only touches cameras that already have a current sample - run the poller at
least once first. Existing rows are left alone; seeded rows are simply older.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def seed(db_path: str, hours: int, interval_seconds: int, seed_value: int) -> int:
    rng = random.Random(seed_value)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    latest = conn.execute(
        """SELECT s.* FROM samples s
           JOIN (SELECT camera_id, MAX(timestamp) AS ts FROM samples
                 GROUP BY camera_id) l
             ON s.camera_id = l.camera_id AND s.timestamp = l.ts"""
    ).fetchall()
    if not latest:
        print("no samples in the database yet - run the poller first", file=sys.stderr)
        return 0

    now = time.time()
    oldest_allowed = now - hours * 3600
    rows = []

    for current in latest:
        payload = json.loads(current["payload"])
        total_bytes = current["sd_total_bytes"] or 0
        end_percent = current["sd_used_percent"]
        if end_percent is None or not total_bytes:
            continue  # nothing meaningful to interpolate

        # Walk backwards from the current value at a per-camera fill rate, so
        # each card has its own slope rather than the fleet moving in lockstep.
        per_hour = rng.uniform(0.05, 0.9)
        steps = int((hours * 3600) // interval_seconds)

        for step in range(steps, 0, -1):
            timestamp = now - step * interval_seconds
            if timestamp < oldest_allowed:
                continue
            hours_back = (now - timestamp) / 3600.0
            percent = end_percent - per_hour * hours_back
            # A little noise, plus a gentle daily cycle: cameras record more
            # during the day, so the fill rate is not perfectly linear.
            percent += math.sin(timestamp / 3600.0) * 0.15 + rng.uniform(-0.08, 0.08)
            percent = max(0.5, min(99.5, percent))
            used_bytes = int(total_bytes * percent / 100.0)

            sample_payload = dict(payload)
            sample_payload["timestamp"] = timestamp
            sample_payload["sd_used_percent"] = percent
            sample_payload["sd_used_bytes"] = used_bytes

            rows.append((
                current["camera_id"], timestamp, current["reachable"],
                current["severity"], current["sd_present"], total_bytes,
                used_bytes, percent, current["sd_write_errors"],
                current["sd_health_percent"], current["rtt_ms"],
                current["uptime_seconds"], current["error"],
                json.dumps(sample_payload),
            ))

    conn.executemany(
        """INSERT INTO samples (camera_id, timestamp, reachable, severity,
               sd_present, sd_total_bytes, sd_used_bytes, sd_used_percent,
               sd_write_errors, sd_health_percent, rtt_ms, uptime_seconds,
               error, payload)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()
    conn.close()
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="camwatch.db", help="path to the database")
    parser.add_argument("--hours", type=int, default=48,
                        help="how far back to backfill (default: %(default)s)")
    parser.add_argument("--interval", type=int, default=300,
                        help="seconds between synthetic samples (default: %(default)s)")
    parser.add_argument("--seed", type=int, default=7, help="RNG seed")
    args = parser.parse_args()

    if not Path(args.db).exists():
        print(f"database not found: {args.db}", file=sys.stderr)
        return 1
    count = seed(args.db, args.hours, args.interval, args.seed)
    print(f"inserted {count} synthetic samples spanning {args.hours}h")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
