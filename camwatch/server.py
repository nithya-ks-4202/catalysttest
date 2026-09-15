"""HTTP API + static file server for the dashboard.

Runs the poll loop on a background thread and serves the results from memory,
so a slow or unreachable camera never delays a dashboard request.

    python3 -m camwatch.server --config cameras.json --port 8080
"""

from __future__ import annotations

import argparse
import json
import logging
import mimetypes
import posixpath
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import AppConfig, ConfigError, load_config
from .health import Severity
from .poller import poll_fleet
from .store import Store, summarise

log = logging.getLogger("camwatch")

WEB_ROOT = Path(__file__).resolve().parent.parent / "web"
# Prune once an hour; the poll loop checks this between cycles.
PRUNE_INTERVAL_SECONDS = 3600


class Monitor:
    """Owns the poll loop and the store."""

    def __init__(self, config: AppConfig, store: Store) -> None:
        self.config = config
        self.store = store
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._poll_now = threading.Event()
        self._last_prune = time.monotonic()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="poll-loop", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._poll_now.set()
        if self._thread is not None:
            self._thread.join(timeout=10)

    def trigger(self) -> None:
        """Ask the loop to poll immediately (the dashboard's refresh button)."""
        self._poll_now.set()

    def poll_once(self) -> float:
        started = time.monotonic()
        samples = poll_fleet(self.config)
        duration = time.monotonic() - started
        self.store.record(samples, duration)
        attention = sum(
            1 for s in samples
            if s.severity in (Severity.WARNING, Severity.SERIOUS, Severity.CRITICAL)
        )
        log.info("polled %d cameras in %.2fs - %d need attention",
                 len(samples), duration, attention)
        return duration

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # keep the loop alive through any single bad cycle
                log.exception("poll cycle failed")

            if time.monotonic() - self._last_prune > PRUNE_INTERVAL_SECONDS:
                try:
                    removed = self.store.prune()
                    if removed:
                        log.info("pruned %d expired samples", removed)
                except Exception:
                    log.exception("prune failed")
                self._last_prune = time.monotonic()

            self._poll_now.wait(timeout=self.config.poll_interval_seconds)
            self._poll_now.clear()


class Handler(BaseHTTPRequestHandler):
    server_version = "camwatch"
    protocol_version = "HTTP/1.1"
    monitor: Monitor  # injected on the server class

    # -- helpers -----------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, status: HTTPStatus, body: bytes, content_type: str,
              extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, default=_json_default).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8",
                   {"Cache-Control": "no-store"})

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._send_json({"error": message, "status": int(status)}, status)

    # -- routing -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        parsed = urlparse(self.path)
        path = posixpath.normpath(parsed.path)
        query = parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                self._handle_api(path, query)
            else:
                self._serve_static(path)
        except BrokenPipeError:  # browser navigated away mid-response
            pass
        except Exception as exc:
            log.exception("error handling %s", self.path)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if posixpath.normpath(parsed.path) == "/api/refresh":
            self.monitor.trigger()
            self._send_json({"ok": True, "message": "poll requested"})
            return
        self._error(HTTPStatus.NOT_FOUND, "no such endpoint")

    def _handle_api(self, path: str, query: dict[str, list[str]]) -> None:
        store = self.monitor.store
        config = self.monitor.config

        if path == "/api/fleet":
            samples = store.latest()
            order = {c.id: i for i, c in enumerate(config.cameras)}
            samples.sort(key=lambda s: order.get(s.camera_id, 1_000_000))
            hours = _int_param(query, "hours", 24, 1, 24 * 90)
            spark_points = _int_param(query, "spark", 24, 2, 200)

            cameras = []
            for sample in samples:
                data = sample.to_dict()
                history = store.capacity_history(sample.camera_id, hours=hours,
                                                 max_points=spark_points)
                data["sparkline"] = [
                    {"t": round(t, 1), "v": round(v, 2)} for t, v in history
                ]
                data["uptime_ratio"] = store.uptime_ratio(sample.camera_id, hours=hours)
                cameras.append(data)

            self._send_json({
                "generated_at": time.time(),
                "poll_interval_seconds": config.poll_interval_seconds,
                "last_poll_at": store.last_poll_at,
                "last_poll_duration": store.last_poll_duration,
                "thresholds": {
                    "capacity_warning_percent": config.thresholds.capacity_warning_percent,
                    "capacity_critical_percent": config.thresholds.capacity_critical_percent,
                },
                "summary": summarise(samples),
                "cameras": cameras,
            })
            return

        if path == "/api/history":
            hours = _int_param(query, "hours", 24, 1, 24 * 90)
            self._send_json({
                "hours": hours,
                "points": store.fleet_capacity_history(hours=hours),
            })
            return

        if path == "/api/events":
            limit = _int_param(query, "limit", 50, 1, 500)
            self._send_json({"events": store.recent_events(limit)})
            return

        if path.startswith("/api/cameras/"):
            remainder = path[len("/api/cameras/"):]
            camera_id, _, suffix = remainder.partition("/")
            sample = store.latest_for(camera_id)
            if sample is None:
                self._error(HTTPStatus.NOT_FOUND, f"unknown camera {camera_id!r}")
                return
            hours = _int_param(query, "hours", 24, 1, 24 * 90)
            if suffix in ("", "status"):
                data = sample.to_dict()
                data["uptime_ratio"] = store.uptime_ratio(camera_id, hours=hours)
                self._send_json(data)
                return
            if suffix == "history":
                points = store.capacity_history(camera_id, hours=hours, max_points=500)
                self._send_json({
                    "camera_id": camera_id,
                    "hours": hours,
                    "points": [{"t": round(t, 1), "v": round(v, 2)} for t, v in points],
                })
                return
            self._error(HTTPStatus.NOT_FOUND, "no such camera endpoint")
            return

        if path == "/api/health":
            # Liveness for an uptime checker: 503 if the poller has stalled.
            last = store.last_poll_at
            stale_after = config.poll_interval_seconds * 3 + 30
            healthy = last is not None and (time.time() - last) < stale_after
            self._send_json(
                {"ok": healthy, "last_poll_at": last, "cameras": len(config.cameras)},
                HTTPStatus.OK if healthy else HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return

        self._error(HTTPStatus.NOT_FOUND, "no such endpoint")

    def _serve_static(self, path: str) -> None:
        if path in ("/", ""):
            path = "/index.html"
        # normpath already collapsed "..", but a leading "/.." would escape;
        # resolving and re-checking the prefix is the belt-and-braces version.
        target = (WEB_ROOT / path.lstrip("/")).resolve()
        try:
            target.relative_to(WEB_ROOT.resolve())
        except ValueError:
            self._error(HTTPStatus.FORBIDDEN, "path outside web root")
            return
        if not target.is_file():
            self._error(HTTPStatus.NOT_FOUND, "not found")
            return

        content_type, _ = mimetypes.guess_type(str(target))
        body = target.read_bytes()
        self._send(HTTPStatus.OK, body, content_type or "application/octet-stream",
                   {"Cache-Control": "no-cache"})


def _int_param(query: dict[str, list[str]], name: str, default: int,
               low: int, high: int) -> int:
    values = query.get(name)
    if not values:
        return default
    try:
        return max(low, min(high, int(values[0])))
    except (TypeError, ValueError):
        return default


def _clip(text: str, width: int) -> str:
    """Trim to `width`, marking the cut with an ellipsis rather than just
    lopping characters off mid-word."""
    return text if len(text) <= width else text[: width - 1] + "…"


def _json_default(value: Any) -> Any:
    if isinstance(value, set):
        return sorted(value)
    return str(value)


def serve(config: AppConfig, host: str, port: int, open_browser: bool = False) -> None:
    store = Store(config.database_path, history_days=config.history_days)
    monitor = Monitor(config, store)

    handler = type("BoundHandler", (Handler,), {"monitor": monitor})
    ThreadingHTTPServer.allow_reuse_address = True
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True

    monitor.start()
    shown_host = "127.0.0.1" if host in ("0.0.0.0", "") else host
    print(f"camwatch: monitoring {len(config.enabled_cameras)} cameras "
          f"every {config.poll_interval_seconds}s")
    print(f"camwatch: dashboard at http://{shown_host}:{port}/")
    if open_browser:
        threading.Thread(
            target=lambda: __import__("webbrowser").open(f"http://{shown_host}:{port}/"),
            daemon=True,
        ).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\ncamwatch: stopping")
    finally:
        httpd.shutdown()
        httpd.server_close()
        monitor.stop()
        store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="camwatch", description="SNMP camera + SD card monitoring dashboard")
    parser.add_argument("--config", default="cameras.json", help="path to cameras.json")
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (use 0.0.0.0 to expose on the LAN)")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--open", action="store_true", help="open a browser on start")
    parser.add_argument("--once", action="store_true",
                        help="poll once, print a table, and exit (no server)")
    parser.add_argument("--json", action="store_true",
                        help="with --once, print JSON instead of a table")
    parser.add_argument("--no-store", action="store_true",
                        help="with --once, skip writing the sample to the database")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.once:
        return _run_once(config, as_json=args.json, store_results=not args.no_store)

    try:
        serve(config, args.host, args.port, open_browser=args.open)
    except OSError as exc:
        print(f"cannot bind {args.host}:{args.port} - {exc}", file=sys.stderr)
        return 1
    return 0


def _run_once(config: AppConfig, as_json: bool = False,
              store_results: bool = True) -> int:
    from .health import format_bytes

    samples = poll_fleet(config)
    # Persist by default, so running --once from cron still builds the history
    # the trend chart and the days-until-full projection need.
    if store_results:
        store = Store(config.database_path, history_days=config.history_days)
        try:
            store.record(samples)
        finally:
            store.close()

    if as_json:
        print(json.dumps(
            {"summary": summarise(samples), "cameras": [s.to_dict() for s in samples]},
            indent=2, default=_json_default,
        ))
    else:
        header = (f"{'CAMERA':<22}{'STATUS':<10}{'SD CARD':<24}"
                  f"{'SIZE':>10}{'USED':>7}  ISSUES")
        print(header)
        print("-" * 110)
        for sample in samples:
            used = (f"{sample.sd_used_percent:.0f}%"
                    if sample.sd_used_percent is not None else "-")
            size = (format_bytes(sample.sd_total_bytes)
                    if sample.sd_present and sample.sd_total_bytes else "-")
            print(f"{_clip(sample.name, 21):<22}{sample.severity:<10}"
                  f"{_clip(sample.sd_label or '-', 23):<24}{size:>10}{used:>7}  "
                  f"{'; '.join(sample.issues)}")
        summary = summarise(samples)
        print("-" * 110)
        print(f"{summary['cameras_online']}/{summary['cameras_total']} online, "
              f"{summary['needs_attention']} need attention")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
