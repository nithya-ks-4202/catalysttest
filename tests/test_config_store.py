"""ConfigStore: safe read/modify/write of cameras.json.

The most important test in here is `test_env_placeholder_is_never_expanded`.
Writing a parsed config back out would bake a resolved community string into a
file people commit - that must never happen.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from camwatch.config import ConfigError, ConfigStore, load_config  # noqa: E402


class ConfigStoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cameras.json"
        self.write({
            "cameras": [
                {"id": "cam-a", "name": "Alpha", "host": "10.0.0.1"},
                {"id": "cam-b", "name": "Bravo", "host": "10.0.0.2"},
            ]
        })
        self.store = ConfigStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, payload):
        self.path.write_text(json.dumps(payload, indent=2))

    def read(self):
        return json.loads(self.path.read_text())

    def ids(self):
        return [c["id"] for c in self.read()["cameras"]]


class TestSecretHandling(ConfigStoreTestCase):
    def test_env_placeholder_is_never_expanded(self):
        """A ${VAR} community must survive a write as a placeholder.

        If this fails, saving from the dashboard silently converts an
        externalised secret into a plaintext one in a committed file.
        """
        self.write({
            "defaults": {"community": "${TEST_COMMUNITY_SECRET}"},
            "cameras": [{"id": "cam-a", "host": "10.0.0.1"}],
        })
        os.environ["TEST_COMMUNITY_SECRET"] = "hunter2-do-not-write-me"
        try:
            config, _ = self.store.add_camera({"host": "10.0.0.5", "name": "New"})
            # The in-memory config resolves it, as polling needs the real value.
            self.assertEqual(config.cameras[0].community, "hunter2-do-not-write-me")
            # The file must not.
            on_disk = self.path.read_text()
            self.assertNotIn("hunter2-do-not-write-me", on_disk)
            self.assertIn("${TEST_COMMUNITY_SECRET}", on_disk)
        finally:
            del os.environ["TEST_COMMUNITY_SECRET"]

    def test_placeholder_survives_update_and_remove(self):
        self.write({
            "defaults": {"community": "${TEST_COMMUNITY_SECRET}"},
            "cameras": [{"id": "cam-a", "host": "10.0.0.1"},
                        {"id": "cam-b", "host": "10.0.0.2"}],
        })
        os.environ["TEST_COMMUNITY_SECRET"] = "sekrit"
        try:
            self.store.update_camera("cam-a", {"name": "Renamed"})
            self.assertIn("${TEST_COMMUNITY_SECRET}", self.path.read_text())
            self.store.remove_camera("cam-b")
            self.assertIn("${TEST_COMMUNITY_SECRET}", self.path.read_text())
            self.assertNotIn("sekrit", self.path.read_text())
        finally:
            del os.environ["TEST_COMMUNITY_SECRET"]


class TestAdd(ConfigStoreTestCase):
    def test_add_appends(self):
        _, entry = self.store.add_camera({"host": "10.0.0.3", "name": "Charlie"})
        self.assertIn(entry["id"], self.ids())
        self.assertEqual(len(self.ids()), 3)

    def test_id_is_generated_from_name(self):
        _, entry = self.store.add_camera({"host": "10.0.0.3", "name": "Loading Dock"})
        self.assertEqual(entry["id"], "cam-loading-dock")

    def test_generated_id_avoids_collision(self):
        self.store.add_camera({"host": "10.0.0.3", "name": "Dock"})
        _, second = self.store.add_camera({"host": "10.0.0.4", "name": "Dock"})
        self.assertEqual(second["id"], "cam-dock-2")

    def test_id_falls_back_to_host_when_unnamed(self):
        _, entry = self.store.add_camera({"host": "10.0.0.3"})
        self.assertEqual(entry["id"], "cam-10-0-0-3")

    def test_explicit_duplicate_id_rejected(self):
        with self.assertRaises(ConfigError):
            self.store.add_camera({"id": "cam-a", "host": "10.0.0.9"})

    def test_host_is_required(self):
        with self.assertRaises(ConfigError):
            self.store.add_camera({"name": "No host"})
        with self.assertRaises(ConfigError):
            self.store.add_camera({"host": "   "})

    def test_unknown_fields_rejected(self):
        with self.assertRaises(ConfigError) as ctx:
            self.store.add_camera({"host": "10.0.0.9", "rm": "-rf"})
        self.assertIn("rm", str(ctx.exception))

    def test_comment_keys_are_tolerated(self):
        _, entry = self.store.add_camera(
            {"host": "10.0.0.9", "_comment": "why this camera exists"})
        self.assertNotIn("_comment", entry)

    def test_invalid_values_rejected(self):
        bad_entries = [
            {"host": "10.0.0.9", "port": 99999},
            {"host": "10.0.0.9", "port": 0},
            {"host": "10.0.0.9", "port": "not-a-number"},
            {"host": "10.0.0.9", "version": "v3"},
            {"host": "10.0.0.9", "timeout": -1},
            {"host": "10.0.0.9", "sd_size_oid": "not.an.oid"},
            {"host": "10.0.0.9", "sd_storage_index": "abc"},
            {"host": "10.0.0.9", "tags": "not-a-list"},
            {"host": "10.0.0.9", "sd_patterns": [1, 2]},
        ]
        for entry in bad_entries:
            with self.assertRaises(ConfigError, msg=f"{entry} should be rejected"):
                self.store.add_camera(entry)

    def test_rejected_add_leaves_file_untouched(self):
        before = self.path.read_text()
        with self.assertRaises(ConfigError):
            self.store.add_camera({"host": "10.0.0.9", "port": -1})
        self.assertEqual(self.path.read_text(), before)

    def test_added_camera_is_loadable(self):
        """Anything the store accepts must still load on the next restart."""
        self.store.add_camera({"host": "10.0.0.9", "name": "Later",
                               "port": 1610, "version": "v1"})
        config = load_config(self.path)
        camera = [c for c in config.cameras if c.name == "Later"][0]
        self.assertEqual(camera.port, 1610)


class TestUpdate(ConfigStoreTestCase):
    def test_partial_update_keeps_other_fields(self):
        _, entry = self.store.update_camera("cam-a", {"site": "HQ"})
        self.assertEqual(entry["name"], "Alpha")
        self.assertEqual(entry["host"], "10.0.0.1")
        self.assertEqual(entry["site"], "HQ")

    def test_null_clears_a_field(self):
        self.store.update_camera("cam-a", {"site": "HQ"})
        _, entry = self.store.update_camera("cam-a", {"site": None})
        self.assertNotIn("site", entry)

    def test_host_not_required_on_update(self):
        _, entry = self.store.update_camera("cam-a", {"name": "Renamed"})
        self.assertEqual(entry["name"], "Renamed")

    def test_blank_host_still_rejected_on_update(self):
        with self.assertRaises(ConfigError):
            self.store.update_camera("cam-a", {"host": "  "})

    def test_rename_id(self):
        _, entry = self.store.update_camera("cam-a", {"id": "cam-renamed"})
        self.assertEqual(entry["id"], "cam-renamed")
        self.assertIn("cam-renamed", self.ids())

    def test_rename_onto_existing_id_rejected(self):
        with self.assertRaises(ConfigError):
            self.store.update_camera("cam-a", {"id": "cam-b"})

    def test_unknown_camera_rejected(self):
        with self.assertRaises(ConfigError):
            self.store.update_camera("cam-nope", {"name": "X"})

    def test_rejected_update_leaves_file_untouched(self):
        before = self.path.read_text()
        with self.assertRaises(ConfigError):
            self.store.update_camera("cam-a", {"version": "v3"})
        self.assertEqual(self.path.read_text(), before)


class TestRemove(ConfigStoreTestCase):
    def test_remove(self):
        self.store.remove_camera("cam-a")
        self.assertEqual(self.ids(), ["cam-b"])

    def test_remove_unknown_rejected(self):
        with self.assertRaises(ConfigError):
            self.store.remove_camera("cam-nope")

    def test_cannot_remove_the_last_camera(self):
        """An empty camera list will not load, so refuse to create one."""
        self.store.remove_camera("cam-a")
        with self.assertRaises(ConfigError):
            self.store.remove_camera("cam-b")
        self.assertEqual(self.ids(), ["cam-b"])


class TestFileHandling(ConfigStoreTestCase):
    def test_unrelated_top_level_keys_are_preserved(self):
        self.write({
            "poll_interval_seconds": 42,
            "history_days": 7,
            "database_path": "custom.db",
            "thresholds": {"capacity_warning_percent": 60,
                           "capacity_critical_percent": 80},
            "cameras": [{"id": "cam-a", "host": "10.0.0.1"}],
        })
        self.store.add_camera({"host": "10.0.0.2"})
        raw = self.read()
        self.assertEqual(raw["poll_interval_seconds"], 42)
        self.assertEqual(raw["history_days"], 7)
        self.assertEqual(raw["database_path"], "custom.db")
        self.assertEqual(raw["thresholds"]["capacity_warning_percent"], 60)

    def test_write_is_atomic_and_leaves_no_temp_files(self):
        self.store.add_camera({"host": "10.0.0.3"})
        leftovers = [p.name for p in Path(self.tmp.name).iterdir()
                     if p.name != "cameras.json"]
        self.assertEqual(leftovers, [])

    def test_failed_write_leaves_no_temp_files(self):
        with self.assertRaises(ConfigError):
            self.store.add_camera({"host": "10.0.0.3", "port": -1})
        leftovers = [p.name for p in Path(self.tmp.name).iterdir()
                     if p.name != "cameras.json"]
        self.assertEqual(leftovers, [])

    def test_missing_file_reports_clearly(self):
        store = ConfigStore(Path(self.tmp.name) / "nope.json")
        with self.assertRaises(ConfigError):
            store.list_cameras()

    def test_malformed_json_reports_clearly(self):
        self.path.write_text("{not json")
        with self.assertRaises(ConfigError) as ctx:
            self.store.list_cameras()
        self.assertIn("invalid JSON", str(ctx.exception))

    def test_output_is_valid_json_with_trailing_newline(self):
        self.store.add_camera({"host": "10.0.0.3"})
        text = self.path.read_text()
        self.assertTrue(text.endswith("\n"))
        json.loads(text)


if __name__ == "__main__":
    unittest.main()
