"""SQLite-backed history, plus the in-memory view of "right now".

Two jobs:
  * keep the latest sample per camera for instant dashboard reads
  * keep a downsampled history so capacity trends and "days until full" mean
    something

SQLite in WAL mode handles the one-writer/many-reader shape here without any
extra machinery, and keeps the whole thing to a single file you can delete.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .health import Severity, estimate_days_until_full
from .poller import CameraSample

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id      TEXT    NOT NULL,
    timestamp      REAL    NOT NULL,
    reachable      INTEGER NOT NULL,
    severity       TEXT    NOT NULL,
    sd_present     INTEGER NOT NULL,
    sd_total_bytes INTEGER NOT NULL DEFAULT 0,
    sd_used_bytes  INTEGER NOT NULL DEFAULT 0,
    sd_used_percent REAL,
    sd_write_errors INTEGER NOT NULL DEFAULT 0,
    sd_health_percent INTEGER,
    rtt_ms         REAL,
    uptime_seconds REAL,
    error          TEXT,
    payload        TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_samples_camera_time
    ON samples (camera_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_samples_time ON samples (timestamp);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id   TEXT NOT NULL,
    timestamp   REAL NOT NULL,
    from_severity TEXT,
    to_severity TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_time ON events (timestamp DESC);
"""


class Store:
    """Thread-safe enough for our use: one lock around a single connection.

    The poller writes from a worker thread while the HTTP server reads from
    request threads, and the write volume (a handful of rows per minute) does
    not justify a connection pool.
    """

    def __init__(self, path: str | Path, history_days: int = 30) -> None:
        self.path = str(path)
        self.history_days = history_days
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            # WAL lets readers proceed during the poller's write.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()
        self._latest: dict[str, CameraSample] = {}
        self._last_poll_at: float | None = None
        self._last_poll_duration: float | None = None
        self._load_latest()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- writes ------------------------------------------------------------

    def record(self, samples: Iterable[CameraSample], duration: float | None = None) -> None:
        samples = list(samples)
        if not samples:
            return
        rows = []
        transitions = []
        for sample in samples:
            previous = self._latest.get(sample.camera_id)
            if previous is not None and previous.severity != sample.severity:
                transitions.append(
                    (sample.camera_id, sample.timestamp, previous.severity,
                     sample.severity, "; ".join(sample.issues))
                )
            rows.append((
                sample.camera_id, sample.timestamp, int(sample.reachable),
                sample.severity, int(sample.sd_present), sample.sd_total_bytes,
                sample.sd_used_bytes, sample.sd_used_percent, sample.sd_write_errors,
                sample.sd_health_percent, sample.rtt_ms, sample.uptime_seconds,
                sample.error, json.dumps(sample.to_dict()),
            ))

        with self._lock:
            self._conn.executemany(
                """INSERT INTO samples (camera_id, timestamp, reachable, severity,
                       sd_present, sd_total_bytes, sd_used_bytes, sd_used_percent,
                       sd_write_errors, sd_health_percent, rtt_ms, uptime_seconds,
                       error, payload)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
            if transitions:
                self._conn.executemany(
                    """INSERT INTO events (camera_id, timestamp, from_severity,
                           to_severity, detail) VALUES (?,?,?,?,?)""",
                    transitions,
                )
            self._conn.commit()

        for sample in samples:
            self._latest[sample.camera_id] = sample
        self._last_poll_at = time.time()
        if duration is not None:
            self._last_poll_duration = duration

        # Attach the trend estimate now that this sample is in the history.
        for sample in samples:
            sample.days_until_full = estimate_days_until_full(
                self.capacity_history(sample.camera_id, hours=24 * 14)
            )

    def prune(self) -> int:
        """Drop samples older than the retention window. Returns rows removed."""
        cutoff = time.time() - self.history_days * 86400
        with self._lock:
            cursor = self._conn.execute("DELETE FROM samples WHERE timestamp < ?", (cutoff,))
            self._conn.execute("DELETE FROM events WHERE timestamp < ?", (cutoff,))
            self._conn.commit()
            return cursor.rowcount

    # -- reads -------------------------------------------------------------

    def _load_latest(self) -> None:
        """Repopulate the in-memory view after a restart, so the dashboard has
        something to show before the first poll completes."""
        with self._lock:
            rows = self._conn.execute(
                """SELECT s.payload FROM samples s
                   JOIN (SELECT camera_id, MAX(timestamp) AS ts
                         FROM samples GROUP BY camera_id) latest
                     ON s.camera_id = latest.camera_id AND s.timestamp = latest.ts"""
            ).fetchall()
        for row in rows:
            try:
                data = json.loads(row["payload"])
            except (json.JSONDecodeError, TypeError):
                continue
            sample = _sample_from_dict(data)
            if sample is not None:
                self._latest[sample.camera_id] = sample

    def latest(self) -> list[CameraSample]:
        return list(self._latest.values())

    def latest_for(self, camera_id: str) -> CameraSample | None:
        return self._latest.get(camera_id)

    def capacity_history(self, camera_id: str, hours: int = 24,
                         max_points: int = 240) -> list[tuple[float, float]]:
        """(timestamp, used_percent) pairs, oldest first, evenly downsampled."""
        since = time.time() - hours * 3600
        with self._lock:
            rows = self._conn.execute(
                """SELECT timestamp, sd_used_percent FROM samples
                   WHERE camera_id = ? AND timestamp >= ? AND sd_used_percent IS NOT NULL
                   ORDER BY timestamp ASC""",
                (camera_id, since),
            ).fetchall()
        points = [(row["timestamp"], row["sd_used_percent"]) for row in rows]
        return _downsample(points, max_points)

    def uptime_ratio(self, camera_id: str, hours: int = 24) -> float | None:
        """Fraction of samples in the window where the camera answered."""
        since = time.time() - hours * 3600
        with self._lock:
            row = self._conn.execute(
                """SELECT COUNT(*) AS total, SUM(reachable) AS up FROM samples
                   WHERE camera_id = ? AND timestamp >= ?""",
                (camera_id, since),
            ).fetchone()
        if not row or not row["total"]:
            return None
        return (row["up"] or 0) / row["total"]

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT camera_id, timestamp, from_severity, to_severity, detail
                   FROM events ORDER BY timestamp DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def fleet_capacity_history(self, hours: int = 24, buckets: int = 96
                               ) -> list[dict[str, Any]]:
        """Fleet-average used-% over time, bucketed for the trend chart."""
        now = time.time()
        since = now - hours * 3600
        with self._lock:
            rows = self._conn.execute(
                """SELECT timestamp, sd_used_percent FROM samples
                   WHERE timestamp >= ? AND sd_used_percent IS NOT NULL
                   ORDER BY timestamp ASC""",
                (since,),
            ).fetchall()
        if not rows:
            return []
        width = max(1.0, (hours * 3600) / buckets)
        totals: dict[int, list[float]] = {}
        for row in rows:
            bucket = int((row["timestamp"] - since) // width)
            totals.setdefault(bucket, []).append(row["sd_used_percent"])
        return [
            {"timestamp": since + bucket * width + width / 2,
             "used_percent": sum(values) / len(values),
             "samples": len(values)}
            for bucket, values in sorted(totals.items())
        ]

    @property
    def last_poll_at(self) -> float | None:
        return self._last_poll_at

    @property
    def last_poll_duration(self) -> float | None:
        return self._last_poll_duration


def _downsample(points: list[tuple[float, float]], limit: int
                ) -> list[tuple[float, float]]:
    """Evenly thin a series, always keeping the first and last point.

    Keeping the endpoints matters: the last point is the current value the
    sparkline's end-dot shows, and it must match the stat tile beside it.
    """
    if len(points) <= limit:
        return points
    step = len(points) / limit
    thinned = [points[int(i * step)] for i in range(limit)]
    if thinned[-1] != points[-1]:
        thinned[-1] = points[-1]
    return thinned


def _sample_from_dict(data: dict[str, Any]) -> CameraSample | None:
    """Rebuild a CameraSample from a stored payload, ignoring unknown keys so
    an older database still loads after the schema grows."""
    fields = CameraSample.__dataclass_fields__
    kwargs = {key: value for key, value in data.items() if key in fields}
    if "camera_id" not in kwargs:
        return None
    kwargs.setdefault("name", kwargs["camera_id"])
    kwargs.setdefault("host", "")
    if "tags" in kwargs and isinstance(kwargs["tags"], list):
        kwargs["tags"] = tuple(kwargs["tags"])
    try:
        return CameraSample(**kwargs)
    except TypeError:
        return None


def summarise(samples: list[CameraSample]) -> dict[str, Any]:
    """Fleet roll-up for the dashboard's tiles."""
    total = len(samples)
    online = sum(1 for s in samples if s.reachable)
    with_card = [s for s in samples if s.sd_present and s.sd_used_percent is not None]
    counts = {level: 0 for level in
              (Severity.GOOD, Severity.WARNING, Severity.SERIOUS,
               Severity.CRITICAL, Severity.UNKNOWN)}
    for sample in samples:
        counts[sample.severity] = counts.get(sample.severity, 0) + 1

    total_bytes = sum(s.sd_total_bytes for s in samples if s.sd_present)
    used_bytes = sum(s.sd_used_bytes for s in samples if s.sd_present)
    return {
        "cameras_total": total,
        "cameras_online": online,
        "cameras_offline": total - online,
        "cards_present": len(with_card),
        "cards_missing": sum(1 for s in samples if s.reachable and not s.sd_present),
        "needs_attention": sum(
            1 for s in samples
            if s.severity in (Severity.WARNING, Severity.SERIOUS, Severity.CRITICAL)
        ),
        "severity_counts": counts,
        "capacity_total_bytes": total_bytes,
        "capacity_used_bytes": used_bytes,
        "capacity_used_percent": (used_bytes / total_bytes * 100.0) if total_bytes else None,
        "average_used_percent": (
            sum(s.sd_used_percent for s in with_card) / len(with_card)
            if with_card else None
        ),
        "soonest_full_days": min(
            (s.days_until_full for s in samples if s.days_until_full is not None),
            default=None,
        ),
    }
