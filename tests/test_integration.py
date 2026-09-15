"""End-to-end tests: real UDP sockets, the real client, the simulator as agent.

These are the tests that would have caught a BER bug the unit tests miss,
because every byte goes over a loopback socket and back.
"""

import json
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from camwatch import mibs, snmp  # noqa: E402
from camwatch.config import AppConfig, CameraConfig, Thresholds, load_config  # noqa: E402
from camwatch.health import Severity  # noqa: E402
from camwatch.poller import poll_camera, poll_fleet  # noqa: E402
from camwatch.store import Store, summarise  # noqa: E402
from tools.fake_camera import FakeCamera, Fleet, Personality, build_fleet  # noqa: E402

# Ports well away from the defaults so a running demo doesn't collide.
BASE_PORT = 31610


class SimulatorHarness:
    """Runs a Fleet on a background thread for the duration of a test."""

    def __init__(self, personalities, base_port):
        self.cameras = [
            FakeCamera(p, base_port + i) for i, p in enumerate(personalities)
        ]
        self.fleet = Fleet(self.cameras)
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        self.fleet.start()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def _serve(self):
        while not self._stop.is_set():
            self.fleet.poll(0.05)

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self.fleet.close()

    def camera_config(self, index, **overrides):
        settings = dict(
            id=f"cam-{index + 1:02d}",
            name=self.cameras[index].personality.name,
            host="127.0.0.1",
            port=self.cameras[index].port,
            community="public",
            timeout=1.0,
            retries=1,
        )
        settings.update(overrides)
        return CameraConfig(**settings)


class TestSnmpOverTheWire(unittest.TestCase):
    port = BASE_PORT

    @classmethod
    def setUpClass(cls):
        cls.harness = SimulatorHarness([Personality("Test Camera")], cls.port)
        cls.harness.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.harness.__exit__(None, None, None)

    def session(self, **overrides):
        settings = dict(host="127.0.0.1", port=self.port, community="public",
                        timeout=1.0, retries=1)
        settings.update(overrides)
        return snmp.Session(snmp.SnmpConfig(**settings))

    def test_get_single_oid(self):
        with self.session() as s:
            bind = s.get_one(mibs.SYS_NAME)
            self.assertIsNotNone(bind)
            self.assertEqual(bind.as_text(), "Test Camera")

    def test_get_multiple_oids_preserves_order(self):
        with self.session() as s:
            binds = s.get(mibs.SYS_NAME, mibs.SYS_DESCR, mibs.SYS_UPTIME)
            self.assertEqual(len(binds), 3)
            self.assertEqual(binds[0].oid_str, mibs.SYS_NAME)
            self.assertEqual(binds[2].oid_str, mibs.SYS_UPTIME)

    def test_timeticks_decode(self):
        with self.session() as s:
            ticks = s.get_one(mibs.SYS_UPTIME).as_int()
            self.assertGreater(ticks, 0)

    def test_missing_oid_returns_none(self):
        with self.session() as s:
            self.assertIsNone(s.get_one("1.3.6.1.2.1.99.99.99.0"))

    def test_walk_returns_whole_subtree(self):
        with self.session() as s:
            descrs = [b.as_text() for b in s.walk(mibs.HR_STORAGE_DESCR)]
        self.assertEqual(len(descrs), 3)
        self.assertIn("/mnt/sdcard (SD Card)", descrs)

    def test_walk_stops_at_subtree_boundary(self):
        root = snmp.ber.parse_oid(mibs.HR_STORAGE_TABLE)
        with self.session() as s:
            for bind in s.walk(mibs.HR_STORAGE_TABLE):
                self.assertEqual(bind.oid[:len(root)], root)

    def test_walk_v1_uses_getnext(self):
        with self.session(version=snmp.VERSION_V1) as s:
            descrs = [b.as_text() for b in s.walk(mibs.HR_STORAGE_DESCR)]
        self.assertEqual(len(descrs), 3)

    def test_walk_honours_limit(self):
        with self.session() as s:
            binds = list(s.walk(mibs.HR_STORAGE_TABLE, limit=5))
        self.assertEqual(len(binds), 5)

    def test_wrong_community_times_out(self):
        with self.session(community="wrong") as s:
            with self.assertRaises(snmp.SnmpTimeout):
                s.get(mibs.SYS_NAME)

    def test_unreachable_port_raises(self):
        with self.session(port=self.port + 900) as s:
            with self.assertRaises(snmp.SnmpError):
                s.get(mibs.SYS_NAME)

    def test_rtt_is_recorded(self):
        with self.session() as s:
            s.get(mibs.SYS_NAME)
            self.assertIsNotNone(s.last_rtt_ms)
            self.assertGreaterEqual(s.last_rtt_ms, 0)

    def test_request_ids_advance(self):
        """Distinct request IDs are what let us discard stale datagrams."""
        with self.session() as s:
            s.get(mibs.SYS_NAME)
            first = s._request_id
            s.get(mibs.SYS_NAME)
            self.assertEqual(s._request_id, first + 1)


class TestPollerAgainstSimulator(unittest.TestCase):
    port = BASE_PORT + 20

    PERSONALITIES = [
        Personality("Healthy Cam", start_used_fraction=0.30, fill_rate_per_min=0),
        Personality("Warning Cam", start_used_fraction=0.80, fill_rate_per_min=0),
        Personality("Critical Cam", start_used_fraction=0.95, fill_rate_per_min=0),
        Personality("No Card Cam", sd_present=False),
        Personality("Wearing Cam", start_used_fraction=0.50, write_errors=99,
                    fill_rate_per_min=0),
        Personality("Offline Cam", reachable=False),
        Personality("Rebooted Cam", start_used_fraction=0.20, uptime_seconds=60,
                    fill_rate_per_min=0),
    ]

    @classmethod
    def setUpClass(cls):
        cls.harness = SimulatorHarness(cls.PERSONALITIES, cls.port)
        cls.harness.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.harness.__exit__(None, None, None)

    def poll(self, index, **overrides):
        return poll_camera(self.harness.camera_config(index, **overrides),
                           Thresholds())

    def test_healthy_camera(self):
        sample = self.poll(0)
        self.assertTrue(sample.reachable)
        self.assertTrue(sample.sd_present)
        self.assertEqual(sample.severity, Severity.GOOD)
        self.assertEqual(sample.sd_source, "hrStorage")
        self.assertAlmostEqual(sample.sd_used_percent, 30.0, delta=1.0)
        self.assertAlmostEqual(sample.sd_total_bytes / (1024 ** 3), 64, delta=0.1)

    def test_warning_camera(self):
        self.assertEqual(self.poll(1).severity, Severity.WARNING)

    def test_critical_camera(self):
        sample = self.poll(2)
        self.assertEqual(sample.severity, Severity.CRITICAL)
        self.assertTrue(any("full" in issue for issue in sample.issues))

    def test_missing_card(self):
        sample = self.poll(3)
        self.assertTrue(sample.reachable)      # the camera answered...
        self.assertFalse(sample.sd_present)    # ...but there is no card
        self.assertEqual(sample.severity, Severity.CRITICAL)

    def test_write_errors_detected_from_allocation_failures(self):
        sample = self.poll(4)
        self.assertEqual(sample.sd_write_errors, 99)
        self.assertEqual(sample.severity, Severity.SERIOUS)

    def test_offline_camera(self):
        sample = self.poll(5)
        self.assertFalse(sample.reachable)
        self.assertEqual(sample.severity, Severity.CRITICAL)
        self.assertIsNotNone(sample.error)

    def test_recent_reboot(self):
        sample = self.poll(6)
        self.assertEqual(sample.severity, Severity.WARNING)
        self.assertLess(sample.uptime_seconds, 900)

    def test_vendor_oids_detect_read_only(self):
        """The vendor status OID must be able to override a healthy-looking card."""
        sample = self.poll(0, sd_status_oid="1.3.6.1.4.1.99999.1.2.2.0",
                           sd_health_oid="1.3.6.1.4.1.99999.1.2.3.0")
        self.assertFalse(sample.sd_read_only)  # this camera reports "ok"
        self.assertIsNotNone(sample.sd_health_percent)

    def test_explicit_storage_index(self):
        sample = self.poll(0, sd_storage_index=2)  # the root filesystem
        self.assertTrue(sample.sd_present)
        self.assertIn("root", sample.sd_label)

    def test_bad_explicit_index_reports_no_card(self):
        sample = self.poll(0, sd_storage_index=99)
        self.assertFalse(sample.sd_present)
        self.assertEqual(sample.severity, Severity.CRITICAL)

    def test_poll_fleet_returns_config_order(self):
        config = AppConfig(
            cameras=[self.harness.camera_config(i)
                     for i in range(len(self.PERSONALITIES))],
            thresholds=Thresholds(),
        )
        samples = poll_fleet(config)
        self.assertEqual(len(samples), len(self.PERSONALITIES))
        self.assertEqual([s.camera_id for s in samples],
                         [c.id for c in config.cameras])

    def test_poll_fleet_survives_a_dead_camera(self):
        """One unreachable camera must not fail the whole sweep."""
        config = AppConfig(
            cameras=[self.harness.camera_config(5), self.harness.camera_config(0)],
            thresholds=Thresholds(),
        )
        samples = poll_fleet(config)
        self.assertEqual(len(samples), 2)
        self.assertFalse(samples[0].reachable)
        self.assertTrue(samples[1].reachable)


class TestPacketLoss(unittest.TestCase):
    """Retries must ride out a lossy link rather than reporting a false outage."""

    port = BASE_PORT + 40

    def test_retry_recovers_from_drops(self):
        harness = SimulatorHarness([Personality("Flaky Cam")], self.port)
        harness.fleet.drop_rate = 0.5
        with harness:
            successes = 0
            for _ in range(12):
                sample = poll_camera(
                    harness.camera_config(0, timeout=0.5, retries=4), Thresholds())
                if sample.reachable:
                    successes += 1
            # With 50% loss and 5 attempts, the odds of a whole poll failing are
            # ~3%, so a healthy majority must still get through.
            self.assertGreater(successes, 8, f"only {successes}/12 polls succeeded")


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "test.db")
        self.store = Store(self.db, history_days=30)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def make_sample(self, camera_id="cam-01", percent=50.0, timestamp=None,
                    severity=Severity.GOOD, reachable=True):
        from camwatch.poller import CameraSample
        return CameraSample(
            camera_id=camera_id, name="Test", host="10.0.0.1:161",
            timestamp=timestamp if timestamp is not None else time.time(),
            reachable=reachable, sd_present=True, sd_total_bytes=64 * 1024 ** 3,
            sd_used_bytes=int(64 * 1024 ** 3 * percent / 100), sd_used_percent=percent,
            severity=severity,
        )

    def test_record_and_read_back(self):
        self.store.record([self.make_sample()])
        latest = self.store.latest()
        self.assertEqual(len(latest), 1)
        self.assertEqual(latest[0].camera_id, "cam-01")

    def test_history_is_ordered_oldest_first(self):
        now = time.time()
        samples = [self.make_sample(percent=40 + i, timestamp=now - (10 - i) * 3600)
                   for i in range(10)]
        self.store.record(samples)
        history = self.store.capacity_history("cam-01", hours=24)
        self.assertEqual(len(history), 10)
        self.assertLess(history[0][0], history[-1][0])
        self.assertAlmostEqual(history[0][1], 40.0, places=1)

    def test_downsample_keeps_endpoints(self):
        now = time.time()
        samples = [self.make_sample(percent=i % 100, timestamp=now - (500 - i) * 60)
                   for i in range(500)]
        self.store.record(samples)
        history = self.store.capacity_history("cam-01", hours=24, max_points=50)
        self.assertLessEqual(len(history), 50)
        full = self.store.capacity_history("cam-01", hours=24, max_points=10_000)
        self.assertEqual(history[-1], full[-1])   # the current value must survive
        self.assertEqual(history[0], full[0])

    def test_severity_transitions_become_events(self):
        self.store.record([self.make_sample(severity=Severity.GOOD)])
        self.store.record([self.make_sample(severity=Severity.CRITICAL)])
        events = self.store.recent_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["from_severity"], Severity.GOOD)
        self.assertEqual(events[0]["to_severity"], Severity.CRITICAL)

    def test_no_event_when_severity_is_unchanged(self):
        self.store.record([self.make_sample(severity=Severity.GOOD)])
        self.store.record([self.make_sample(severity=Severity.GOOD)])
        self.assertEqual(self.store.recent_events(), [])

    def test_prune_drops_old_rows_only(self):
        now = time.time()
        self.store.record([self.make_sample(timestamp=now - 40 * 86400)])
        self.store.record([self.make_sample(timestamp=now)])
        removed = self.store.prune()
        self.assertEqual(removed, 1)
        self.assertEqual(len(self.store.capacity_history("cam-01", hours=24 * 90)), 1)

    def test_latest_survives_a_restart(self):
        self.store.record([self.make_sample(percent=77.0)])
        self.store.close()
        reopened = Store(self.db)
        try:
            latest = reopened.latest()
            self.assertEqual(len(latest), 1)
            self.assertAlmostEqual(latest[0].sd_used_percent, 77.0, places=1)
        finally:
            reopened.close()

    def test_uptime_ratio(self):
        now = time.time()
        for i in range(10):
            self.store.record([self.make_sample(timestamp=now - i * 60,
                                                reachable=(i % 2 == 0))])
        self.assertAlmostEqual(self.store.uptime_ratio("cam-01", hours=1), 0.5,
                               places=2)

    def test_summarise(self):
        from camwatch.poller import CameraSample
        samples = [
            self.make_sample("a", 30.0, severity=Severity.GOOD),
            self.make_sample("b", 95.0, severity=Severity.CRITICAL),
            CameraSample(camera_id="c", name="c", host="h", reachable=False,
                         severity=Severity.CRITICAL),
        ]
        summary = summarise(samples)
        self.assertEqual(summary["cameras_total"], 3)
        self.assertEqual(summary["cameras_online"], 2)
        self.assertEqual(summary["cameras_offline"], 1)
        self.assertEqual(summary["needs_attention"], 2)
        self.assertEqual(summary["severity_counts"]["critical"], 2)


class TestConfigLoading(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cameras.json"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, payload):
        self.path.write_text(json.dumps(payload))
        return str(self.path)

    def test_minimal_config(self):
        config = load_config(self.write({"cameras": [{"host": "10.0.0.1"}]}))
        self.assertEqual(len(config.cameras), 1)
        self.assertEqual(config.cameras[0].id, "cam-01")
        self.assertEqual(config.cameras[0].port, 161)
        self.assertEqual(config.cameras[0].version, snmp.VERSION_V2C)

    def test_defaults_merge_into_each_camera(self):
        config = load_config(self.write({
            "defaults": {"community": "shared", "site": "HQ"},
            "cameras": [{"host": "10.0.0.1"}, {"host": "10.0.0.2", "community": "own"}],
        }))
        self.assertEqual(config.cameras[0].community, "shared")
        self.assertEqual(config.cameras[0].site, "HQ")
        self.assertEqual(config.cameras[1].community, "own")

    def test_env_expansion(self):
        import os
        os.environ["TEST_SNMP_COMMUNITY"] = "from-env"
        try:
            config = load_config(self.write({
                "cameras": [{"host": "10.0.0.1",
                             "community": "${TEST_SNMP_COMMUNITY}"}],
            }))
            self.assertEqual(config.cameras[0].community, "from-env")
        finally:
            del os.environ["TEST_SNMP_COMMUNITY"]

    def test_missing_env_var_is_an_error(self):
        from camwatch.config import ConfigError
        with self.assertRaises(ConfigError):
            load_config(self.write({
                "cameras": [{"host": "10.0.0.1", "community": "${NOPE_NOT_SET}"}],
            }))

    def test_duplicate_ids_rejected(self):
        from camwatch.config import ConfigError
        with self.assertRaises(ConfigError):
            load_config(self.write({"cameras": [
                {"id": "same", "host": "10.0.0.1"},
                {"id": "same", "host": "10.0.0.2"},
            ]}))

    def test_v3_is_rejected_with_a_clear_message(self):
        from camwatch.config import ConfigError
        with self.assertRaises(ConfigError) as ctx:
            load_config(self.write({
                "cameras": [{"host": "10.0.0.1", "version": "v3"}]}))
        self.assertIn("v3", str(ctx.exception))

    def test_inverted_thresholds_rejected(self):
        from camwatch.config import ConfigError
        with self.assertRaises(ConfigError):
            load_config(self.write({
                "thresholds": {"capacity_warning_percent": 95,
                               "capacity_critical_percent": 80},
                "cameras": [{"host": "10.0.0.1"}],
            }))

    def test_missing_host_rejected(self):
        from camwatch.config import ConfigError
        with self.assertRaises(ConfigError):
            load_config(self.write({"cameras": [{"name": "no host"}]}))

    def test_missing_file_message_is_helpful(self):
        from camwatch.config import ConfigError
        with self.assertRaises(ConfigError) as ctx:
            load_config(str(Path(self.tmp.name) / "nope.json"))
        self.assertIn("fake_camera", str(ctx.exception))

    def test_disabled_cameras_are_excluded_from_polling(self):
        config = load_config(self.write({"cameras": [
            {"id": "on", "host": "10.0.0.1"},
            {"id": "off", "host": "10.0.0.2", "enabled": False},
        ]}))
        self.assertEqual(len(config.cameras), 2)
        self.assertEqual([c.id for c in config.enabled_cameras], ["on"])


class TestHttpApi(unittest.TestCase):
    """Boot the real server against the simulator and hit the endpoints."""

    port = BASE_PORT + 60
    http_port = BASE_PORT + 80

    @classmethod
    def setUpClass(cls):
        cls.harness = SimulatorHarness(
            [Personality("API Cam", start_used_fraction=0.5, fill_rate_per_min=0),
             Personality("API Cam 2", sd_present=False)], cls.port)
        cls.harness.__enter__()

        cls.tmp = tempfile.TemporaryDirectory()
        config = AppConfig(
            cameras=[cls.harness.camera_config(0), cls.harness.camera_config(1)],
            thresholds=Thresholds(),
            poll_interval_seconds=5,
            database_path=str(Path(cls.tmp.name) / "api.db"),
        )
        from camwatch.server import Handler, Monitor
        from http.server import ThreadingHTTPServer

        cls.store = Store(config.database_path)
        cls.monitor = Monitor(config, cls.store)
        cls.monitor.poll_once()  # one synchronous cycle so there is data to serve

        handler = type("BoundHandler", (Handler,), {"monitor": cls.monitor})
        ThreadingHTTPServer.allow_reuse_address = True
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", cls.http_port), handler)
        cls.httpd.daemon_threads = True
        cls.server_thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.store.close()
        cls.tmp.cleanup()
        cls.harness.__exit__(None, None, None)

    def get(self, path):
        url = f"http://127.0.0.1:{self.http_port}{path}"
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status, json.loads(response.read())

    def test_fleet_endpoint(self):
        status, payload = self.get("/api/fleet")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["cameras"]), 2)
        self.assertEqual(payload["summary"]["cameras_total"], 2)
        self.assertIn("thresholds", payload)

    def test_fleet_camera_shape(self):
        _, payload = self.get("/api/fleet")
        camera = payload["cameras"][0]
        for key in ("camera_id", "name", "severity", "sd_present", "sd_used_percent",
                    "sd_free_bytes", "issues", "sparkline"):
            self.assertIn(key, camera)

    def test_single_camera_endpoint(self):
        status, payload = self.get("/api/cameras/cam-01")
        self.assertEqual(status, 200)
        self.assertEqual(payload["camera_id"], "cam-01")

    def test_unknown_camera_404s(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/cameras/does-not-exist")
        self.assertEqual(ctx.exception.code, 404)

    def test_history_endpoint(self):
        status, payload = self.get("/api/history?hours=24")
        self.assertEqual(status, 200)
        self.assertIn("points", payload)

    def test_hours_param_is_clamped(self):
        _, payload = self.get("/api/history?hours=999999")
        self.assertLessEqual(payload["hours"], 24 * 90)

    def test_bad_hours_param_falls_back_to_default(self):
        _, payload = self.get("/api/history?hours=abc")
        self.assertEqual(payload["hours"], 24)

    def test_health_endpoint(self):
        status, payload = self.get("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_static_index_is_served(self):
        url = f"http://127.0.0.1:{self.http_port}/"
        with urllib.request.urlopen(url, timeout=5) as response:
            body = response.read().decode()
        self.assertEqual(response.status, 200)
        self.assertIn("Camera &amp; SD card monitor", body)

    def test_directory_traversal_is_blocked(self):
        """../ must never escape the web root."""
        for attack in ("/../camwatch/server.py", "/../../etc/passwd",
                       "/..%2f..%2fetc/passwd"):
            with self.assertRaises(urllib.error.HTTPError,
                                   msg=f"{attack} was not blocked") as ctx:
                self.get(attack)
            self.assertIn(ctx.exception.code, (403, 404))

    def test_unknown_endpoint_404s(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.get("/api/nope")
        self.assertEqual(ctx.exception.code, 404)


class TestCameraManagementApi(unittest.TestCase):
    """Add/edit/remove cameras over HTTP, against a real config file."""

    port = BASE_PORT + 100
    http_port = BASE_PORT + 120

    @classmethod
    def setUpClass(cls):
        cls.harness = SimulatorHarness(
            [Personality("Managed Cam", start_used_fraction=0.4, fill_rate_per_min=0),
             Personality("Spare Cam", start_used_fraction=0.5, fill_rate_per_min=0),
             Personality("No Card Cam", sd_present=False)], cls.port)
        cls.harness.__enter__()

        cls.tmp = tempfile.TemporaryDirectory()
        cls.config_path = Path(cls.tmp.name) / "cameras.json"
        cls.db_path = Path(cls.tmp.name) / "manage.db"
        cls.config_path.write_text(json.dumps({
            "poll_interval_seconds": 3600,  # long: tests trigger polls explicitly
            "database_path": str(cls.db_path),
            "cameras": [{"id": "cam-01", "name": "Managed Cam",
                         "host": "127.0.0.1", "port": cls.port,
                         "community": "public", "timeout": 1.0}],
        }, indent=2))

        from camwatch.config import ConfigStore, load_config
        from camwatch.server import Handler, Monitor
        from http.server import ThreadingHTTPServer

        config = load_config(cls.config_path)
        cls.store = Store(str(cls.db_path))
        cls.monitor = Monitor(config, cls.store,
                              config_store=ConfigStore(cls.config_path),
                              allow_config_edits=True)
        cls.monitor.poll_once()

        handler = type("BoundHandler", (Handler,), {"monitor": cls.monitor})
        ThreadingHTTPServer.allow_reuse_address = True
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", cls.http_port), handler)
        cls.httpd.daemon_threads = True
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.store.close()
        cls.tmp.cleanup()
        cls.harness.__exit__(None, None, None)

    def tearDown(self):
        # Reset the config between tests so each starts from one camera.
        self.config_path.write_text(json.dumps({
            "poll_interval_seconds": 3600,
            "database_path": str(self.db_path),
            "cameras": [{"id": "cam-01", "name": "Managed Cam",
                         "host": "127.0.0.1", "port": self.port,
                         "community": "public", "timeout": 1.0}],
        }, indent=2))
        self.monitor.reload_config()

    def request(self, method, path, payload=None):
        url = f"http://127.0.0.1:{self.http_port}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as response:
            body = response.read()
            return response.status, (json.loads(body) if body else {})

    def cameras_in_file(self):
        return json.loads(self.config_path.read_text())["cameras"]

    # -- test endpoint -----------------------------------------------------

    def test_test_endpoint_finds_a_card(self):
        status, payload = self.request("POST", "/api/cameras/test", {
            "host": "127.0.0.1", "port": self.port + 1, "community": "public"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["reachable"])
        self.assertTrue(payload["sd_present"])
        self.assertIn("Spare Cam", payload["summary"])

    def test_test_endpoint_reports_unreachable(self):
        status, payload = self.request("POST", "/api/cameras/test", {
            "host": "127.0.0.1", "port": self.port + 900, "timeout": 0.4})
        self.assertEqual(status, 200)  # the probe ran; the camera just didn't answer
        self.assertFalse(payload["reachable"])
        self.assertIn("hint", payload)

    def test_test_endpoint_lists_volumes_when_no_card_found(self):
        """The UI needs the volume list to offer a 'which one is it?' picker."""
        status, payload = self.request("POST", "/api/cameras/test", {
            "host": "127.0.0.1", "port": self.port + 2, "community": "public"})
        self.assertTrue(payload["reachable"])
        self.assertFalse(payload["sd_present"])
        self.assertTrue(payload["volumes"])
        self.assertTrue(any("root" in v["descr"] for v in payload["volumes"]))

    def test_test_endpoint_requires_host(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("POST", "/api/cameras/test", {"name": "no host"})
        self.assertEqual(ctx.exception.code, 400)

    def test_test_endpoint_does_not_save(self):
        self.request("POST", "/api/cameras/test",
                     {"host": "127.0.0.1", "port": self.port + 1})
        self.assertEqual(len(self.cameras_in_file()), 1)

    # -- create ------------------------------------------------------------

    def test_add_camera(self):
        status, payload = self.request("POST", "/api/cameras", {
            "name": "Spare Cam", "host": "127.0.0.1", "port": self.port + 1})
        self.assertEqual(status, 201)
        self.assertEqual(payload["camera"]["id"], "cam-spare-cam")
        self.assertEqual(len(self.cameras_in_file()), 2)

    def test_added_camera_is_polled_and_appears_in_fleet(self):
        self.request("POST", "/api/cameras", {
            "name": "Spare Cam", "host": "127.0.0.1", "port": self.port + 1})
        self.monitor.poll_once()
        _, fleet = self.request("GET", "/api/fleet")
        names = [c["name"] for c in fleet["cameras"]]
        self.assertIn("Spare Cam", names)

    def test_add_rejects_bad_input(self):
        for payload in ({"name": "no host"}, {"host": "1.2.3.4", "port": 99999},
                        {"host": "1.2.3.4", "version": "v3"},
                        {"host": "1.2.3.4", "nope": 1}):
            with self.assertRaises(urllib.error.HTTPError,
                                   msg=f"{payload} should be rejected") as ctx:
                self.request("POST", "/api/cameras", payload)
            self.assertEqual(ctx.exception.code, 400)
        self.assertEqual(len(self.cameras_in_file()), 1)

    def test_add_rejects_malformed_json(self):
        url = f"http://127.0.0.1:{self.http_port}/api/cameras"
        req = urllib.request.Request(url, data=b"{not json", method="POST")
        req.add_header("Content-Type", "application/json")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    # -- update ------------------------------------------------------------

    def test_patch_camera(self):
        status, payload = self.request("PATCH", "/api/cameras/cam-01",
                                       {"name": "Renamed", "site": "HQ"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["camera"]["name"], "Renamed")
        entry = self.cameras_in_file()[0]
        self.assertEqual(entry["name"], "Renamed")
        self.assertEqual(entry["host"], "127.0.0.1")  # untouched

    def test_patch_unknown_camera_400s(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("PATCH", "/api/cameras/nope", {"name": "X"})
        self.assertEqual(ctx.exception.code, 400)

    # -- delete ------------------------------------------------------------

    def test_delete_camera(self):
        self.request("POST", "/api/cameras", {
            "name": "Spare Cam", "host": "127.0.0.1", "port": self.port + 1})
        status, payload = self.request("DELETE", "/api/cameras/cam-spare-cam")
        self.assertEqual(status, 200)
        self.assertEqual(payload["removed"], "cam-spare-cam")
        self.assertEqual(len(self.cameras_in_file()), 1)

    def test_deleted_camera_disappears_from_fleet(self):
        """A removed camera must not linger in the in-memory view."""
        self.request("POST", "/api/cameras", {
            "name": "Spare Cam", "host": "127.0.0.1", "port": self.port + 1})
        self.monitor.poll_once()
        self.request("DELETE", "/api/cameras/cam-spare-cam")
        _, fleet = self.request("GET", "/api/fleet")
        self.assertNotIn("Spare Cam", [c["name"] for c in fleet["cameras"]])
        self.assertEqual(fleet["summary"]["cameras_total"], 1)

    def test_cannot_delete_the_last_camera(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.request("DELETE", "/api/cameras/cam-01")
        self.assertEqual(ctx.exception.code, 400)
        self.assertEqual(len(self.cameras_in_file()), 1)

    # -- capability flag ---------------------------------------------------

    def test_fleet_reports_edit_capability(self):
        _, fleet = self.request("GET", "/api/fleet")
        self.assertTrue(fleet["can_edit_cameras"])


class TestCameraManagementDisabled(unittest.TestCase):
    """With editing off, every mutating endpoint must refuse."""

    port = BASE_PORT + 140
    http_port = BASE_PORT + 160

    @classmethod
    def setUpClass(cls):
        cls.harness = SimulatorHarness([Personality("Locked Cam")], cls.port)
        cls.harness.__enter__()
        cls.tmp = tempfile.TemporaryDirectory()
        config_path = Path(cls.tmp.name) / "cameras.json"
        config_path.write_text(json.dumps({
            "poll_interval_seconds": 3600,
            "database_path": str(Path(cls.tmp.name) / "locked.db"),
            "cameras": [{"id": "cam-01", "host": "127.0.0.1", "port": cls.port,
                         "timeout": 1.0}],
        }))

        from camwatch.config import ConfigStore, load_config
        from camwatch.server import Handler, Monitor
        from http.server import ThreadingHTTPServer

        cls.config_path = config_path
        cls.store = Store(str(Path(cls.tmp.name) / "locked.db"))
        cls.monitor = Monitor(load_config(config_path), cls.store,
                              config_store=ConfigStore(config_path),
                              allow_config_edits=False)
        cls.monitor.poll_once()
        handler = type("BoundHandler", (Handler,), {"monitor": cls.monitor})
        ThreadingHTTPServer.allow_reuse_address = True
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", cls.http_port), handler)
        cls.httpd.daemon_threads = True
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.store.close()
        cls.tmp.cleanup()
        cls.harness.__exit__(None, None, None)

    def request(self, method, path, payload=None):
        url = f"http://127.0.0.1:{self.http_port}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read() or b"{}")

    def test_all_mutations_are_forbidden(self):
        attempts = [
            ("POST", "/api/cameras", {"host": "1.2.3.4"}),
            ("POST", "/api/cameras/test", {"host": "1.2.3.4"}),
            ("PATCH", "/api/cameras/cam-01", {"name": "X"}),
            ("DELETE", "/api/cameras/cam-01", None),
            ("POST", "/api/reload", None),
        ]
        for method, path, payload in attempts:
            with self.assertRaises(urllib.error.HTTPError,
                                   msg=f"{method} {path} should be forbidden") as ctx:
                self.request(method, path, payload)
            self.assertEqual(ctx.exception.code, 403)

    def test_config_file_is_untouched(self):
        before = self.config_path.read_text()
        try:
            self.request("POST", "/api/cameras", {"host": "1.2.3.4"})
        except urllib.error.HTTPError:
            pass
        self.assertEqual(self.config_path.read_text(), before)

    def test_read_endpoints_still_work(self):
        status, fleet = self.request("GET", "/api/fleet")
        self.assertEqual(status, 200)
        self.assertFalse(fleet["can_edit_cameras"])

    def test_refresh_is_still_allowed(self):
        """Refresh only re-polls existing cameras, so it isn't gated."""
        status, _ = self.request("POST", "/api/refresh", None)
        self.assertEqual(status, 200)


class TestSimulatorPortHandling(unittest.TestCase):
    """A busy port must fail loudly, and --auto-port must route around it."""

    port = BASE_PORT + 180

    def test_bind_conflict_raises_a_clear_error(self):
        from tools.fake_camera import Fleet, PortInUseError, build_fleet

        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        blocker.bind(("127.0.0.1", self.port))
        try:
            fleet = Fleet(build_fleet(2, self.port))
            with self.assertRaises(PortInUseError) as ctx:
                fleet.start()
            self.assertEqual(ctx.exception.port, self.port)
        finally:
            blocker.close()

    def test_failed_start_leaves_no_sockets_open(self):
        """A half-started fleet must release what it already bound, or the
        retry on the next port hits its own leftovers."""
        from tools.fake_camera import Fleet, PortInUseError, build_fleet

        # Block the *second* port so the first binds before the failure.
        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        blocker.bind(("127.0.0.1", self.port + 21))
        try:
            fleet = Fleet(build_fleet(3, self.port + 20))
            with self.assertRaises(PortInUseError):
                fleet.start()
            # The first port must have been released on the way out.
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                probe.bind(("127.0.0.1", self.port + 20))
            finally:
                probe.close()
        finally:
            blocker.close()

    def test_find_free_base_port_skips_a_busy_range(self):
        from tools.fake_camera import find_free_base_port

        start = self.port + 40
        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        blocker.bind(("127.0.0.1", start))
        try:
            chosen = find_free_base_port("127.0.0.1", start, 4)
            self.assertGreater(chosen, start)
            self.assertEqual((chosen - start) % 4, 0)
        finally:
            blocker.close()

    def test_find_free_base_port_keeps_the_start_when_free(self):
        from tools.fake_camera import find_free_base_port
        start = self.port + 60
        self.assertEqual(find_free_base_port("127.0.0.1", start, 3), start)

    def test_port_is_free_detects_a_taken_port(self):
        from tools.fake_camera import port_is_free
        start = self.port + 80
        self.assertTrue(port_is_free("127.0.0.1", start))
        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        blocker.bind(("127.0.0.1", start))
        try:
            # Must be detectable as busy, not masked by SO_REUSEADDR.
            self.assertFalse(port_is_free("127.0.0.1", start))
        finally:
            blocker.close()


class TestLoopbackDetection(unittest.TestCase):
    def test_loopback_hosts(self):
        from camwatch.server import is_loopback
        for host in ("127.0.0.1", "localhost", "", "::1", "127.0.1.1"):
            self.assertTrue(is_loopback(host), f"{host!r} should be loopback")

    def test_non_loopback_hosts(self):
        from camwatch.server import is_loopback
        for host in ("0.0.0.0", "192.168.1.10", "10.0.0.1", "::", "example.com"):
            self.assertFalse(is_loopback(host), f"{host!r} should not be loopback")


if __name__ == "__main__":
    unittest.main()
