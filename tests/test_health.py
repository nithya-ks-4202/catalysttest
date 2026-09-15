"""Health rules and the SD-card-picking logic - no network involved."""

import unittest

from camwatch import mibs
from camwatch.config import CameraConfig, Thresholds
from camwatch.health import (Severity, estimate_days_until_full, evaluate,
                             format_bytes, format_duration, worst)
from camwatch.poller import CameraSample, StorageRow, pick_sd_card

GIB = 1024 ** 3


def make_sample(**overrides) -> CameraSample:
    defaults = dict(
        camera_id="cam-01", name="Test", host="10.0.0.1:161",
        reachable=True, sd_present=True, sd_total_bytes=64 * GIB,
        sd_used_bytes=32 * GIB, sd_used_percent=50.0, uptime_seconds=86400,
    )
    defaults.update(overrides)
    return CameraSample(**defaults)


def make_row(index, descr, size_gb, used_fraction=0.5, type_oid=None, failures=0):
    block = 4096
    total = int(size_gb * GIB) // block
    return StorageRow(
        index=index, descr=descr, type_oid=type_oid, allocation_units=block,
        size_units=total, used_units=int(total * used_fraction),
        allocation_failures=failures,
    )


class TestSeverityOrdering(unittest.TestCase):
    def test_worst_wins(self):
        self.assertEqual(worst(Severity.GOOD, Severity.CRITICAL), Severity.CRITICAL)
        self.assertEqual(worst(Severity.WARNING, Severity.SERIOUS), Severity.SERIOUS)
        self.assertEqual(worst(Severity.GOOD, Severity.GOOD), Severity.GOOD)
        self.assertEqual(worst(Severity.UNKNOWN, Severity.WARNING), Severity.WARNING)


class TestEvaluate(unittest.TestCase):
    def setUp(self):
        self.thresholds = Thresholds()

    def test_healthy_camera(self):
        sample = make_sample()
        self.assertEqual(evaluate(sample, self.thresholds), Severity.GOOD)
        self.assertEqual(sample.issues, [])

    def test_unreachable_is_critical_and_drops_other_noise(self):
        sample = make_sample(reachable=False, error="timeout", sd_present=False)
        self.assertEqual(evaluate(sample, self.thresholds), Severity.CRITICAL)
        self.assertEqual(len(sample.issues), 1)
        self.assertIn("unreachable", sample.issues[0])

    def test_missing_card_is_critical(self):
        sample = make_sample(sd_present=False, sd_used_percent=None)
        self.assertEqual(evaluate(sample, self.thresholds), Severity.CRITICAL)
        self.assertIn("No SD card detected", sample.issues)

    def test_capacity_thresholds(self):
        cases = [
            (50.0, Severity.GOOD),
            (74.9, Severity.GOOD),
            (75.0, Severity.WARNING),
            (89.9, Severity.WARNING),
            (90.0, Severity.CRITICAL),
            (99.9, Severity.CRITICAL),
        ]
        for percent, expected in cases:
            sample = make_sample(sd_used_percent=percent)
            self.assertEqual(evaluate(sample, self.thresholds), expected,
                             f"{percent}% should be {expected}")

    def test_read_only_is_critical_even_when_nearly_empty(self):
        sample = make_sample(sd_used_percent=5.0, sd_read_only=True)
        self.assertEqual(evaluate(sample, self.thresholds), Severity.CRITICAL)
        self.assertTrue(any("read-only" in i for i in sample.issues))

    def test_write_errors_are_serious(self):
        sample = make_sample(sd_write_errors=42)
        self.assertEqual(evaluate(sample, self.thresholds), Severity.SERIOUS)

    def test_zero_write_errors_is_fine(self):
        self.assertEqual(evaluate(make_sample(sd_write_errors=0), self.thresholds),
                         Severity.GOOD)

    def test_card_health_bands(self):
        self.assertEqual(evaluate(make_sample(sd_health_percent=80), self.thresholds),
                         Severity.GOOD)
        self.assertEqual(evaluate(make_sample(sd_health_percent=45), self.thresholds),
                         Severity.SERIOUS)
        self.assertEqual(evaluate(make_sample(sd_health_percent=10), self.thresholds),
                         Severity.CRITICAL)

    def test_recent_reboot_warns(self):
        sample = make_sample(uptime_seconds=120)
        self.assertEqual(evaluate(sample, self.thresholds), Severity.WARNING)
        self.assertTrue(any("Rebooted" in i for i in sample.issues))

    def test_worst_condition_wins_over_several(self):
        sample = make_sample(sd_used_percent=95.0, uptime_seconds=60,
                             sd_write_errors=10)
        self.assertEqual(evaluate(sample, self.thresholds), Severity.CRITICAL)
        self.assertEqual(len(sample.issues), 3)  # every issue is still listed

    def test_no_capacity_reported_is_unknown(self):
        sample = make_sample(sd_used_percent=None)
        self.assertEqual(evaluate(sample, self.thresholds), Severity.UNKNOWN)

    def test_custom_thresholds_are_respected(self):
        strict = Thresholds(capacity_warning_percent=40, capacity_critical_percent=60)
        self.assertEqual(evaluate(make_sample(sd_used_percent=50.0), strict),
                         Severity.WARNING)
        self.assertEqual(evaluate(make_sample(sd_used_percent=65.0), strict),
                         Severity.CRITICAL)


class TestPickSDCard(unittest.TestCase):
    def setUp(self):
        self.camera = CameraConfig(id="c", name="c", host="10.0.0.1")
        self.thresholds = Thresholds()

    def pick(self, rows, camera=None):
        return pick_sd_card(rows, camera or self.camera, self.thresholds)

    def test_picks_named_sd_card_over_root(self):
        rows = [
            make_row(1, "Physical memory", 0.5, type_oid=mibs.HR_STORAGE_TYPE_RAM),
            make_row(2, "/ (root filesystem)", 0.25, 0.96,
                     type_oid=mibs.HR_STORAGE_TYPE_FIXED_DISK),
            make_row(3, "/mnt/sdcard (SD Card)", 64, 0.4,
                     type_oid=mibs.HR_STORAGE_TYPE_REMOVABLE_DISK),
        ]
        picked = self.pick(rows)
        self.assertIsNotNone(picked)
        self.assertEqual(picked.index, 3)

    def test_never_picks_memory(self):
        rows = [make_row(1, "Physical memory", 8, type_oid=mibs.HR_STORAGE_TYPE_RAM)]
        self.assertIsNone(self.pick(rows))

    def test_never_picks_swap_even_if_it_matches(self):
        rows = [make_row(1, "Swap space on /mnt/sdcard", 4,
                         type_oid=mibs.HR_STORAGE_TYPE_VIRTUAL_MEMORY)]
        self.assertIsNone(self.pick(rows))

    def test_falls_back_to_removable_type(self):
        rows = [
            make_row(1, "/ (root)", 1, type_oid=mibs.HR_STORAGE_TYPE_FIXED_DISK),
            make_row(2, "/data/volume1", 128, 0.3,
                     type_oid=mibs.HR_STORAGE_TYPE_REMOVABLE_DISK),
        ]
        picked = self.pick(rows)
        self.assertEqual(picked.index, 2)

    def test_ignores_implausibly_small_volumes(self):
        """A 4 MiB boot partition called 'sdcard' is not the recording medium."""
        rows = [make_row(1, "/mnt/sdcard", 0.004,
                         type_oid=mibs.HR_STORAGE_TYPE_REMOVABLE_DISK)]
        self.assertIsNone(self.pick(rows))

    def test_explicit_index_wins(self):
        camera = CameraConfig(id="c", name="c", host="h", sd_storage_index=2)
        rows = [
            make_row(2, "/data (vendor volume)", 32, 0.7),
            make_row(3, "/mnt/sdcard (SD Card)", 64, 0.4,
                     type_oid=mibs.HR_STORAGE_TYPE_REMOVABLE_DISK),
        ]
        self.assertEqual(self.pick(rows, camera).index, 2)

    def test_explicit_index_missing_returns_nothing(self):
        """Don't silently guess a different volume than the operator named."""
        camera = CameraConfig(id="c", name="c", host="h", sd_storage_index=9)
        rows = [make_row(3, "/mnt/sdcard", 64, 0.4,
                         type_oid=mibs.HR_STORAGE_TYPE_REMOVABLE_DISK)]
        self.assertIsNone(self.pick(rows, camera))

    def test_most_specific_pattern_wins(self):
        rows = [
            make_row(1, "External storage volume", 32, 0.2),
            make_row(2, "/mnt/sdcard", 16, 0.2),
        ]
        # "/mnt/sd" is earlier in the pattern list than "external"/"storage".
        self.assertEqual(self.pick(rows).index, 2)

    def test_custom_patterns(self):
        camera = CameraConfig(id="c", name="c", host="h",
                              sd_patterns=("recording volume",))
        rows = [
            make_row(1, "/mnt/sdcard", 64, 0.2,
                     type_oid=mibs.HR_STORAGE_TYPE_REMOVABLE_DISK),
            make_row(2, "Recording Volume A", 32, 0.5),
        ]
        self.assertEqual(self.pick(rows, camera).index, 2)

    def test_empty_table(self):
        self.assertIsNone(self.pick([]))


class TestStorageRowMath(unittest.TestCase):
    def test_bytes_and_percent(self):
        row = make_row(1, "/mnt/sdcard", 64, 0.25)
        self.assertAlmostEqual(row.size_bytes / GIB, 64, places=3)
        self.assertAlmostEqual(row.used_percent, 25.0, places=1)
        self.assertAlmostEqual(row.free_bytes / GIB, 48, places=2)

    def test_zero_size_has_no_percent(self):
        row = StorageRow(index=1, allocation_units=4096, size_units=0, used_units=0)
        self.assertIsNone(row.used_percent)

    def test_used_above_size_clamps_to_100(self):
        """Some agents report used > size after a resize; never show 140%."""
        row = StorageRow(index=1, allocation_units=4096, size_units=100, used_units=140)
        self.assertEqual(row.used_percent, 100.0)

    def test_negative_values_are_clamped(self):
        row = StorageRow(index=1, allocation_units=4096, size_units=-5, used_units=-9)
        self.assertEqual(row.size_bytes, 0)
        self.assertEqual(row.used_bytes, 0)


class TestProjection(unittest.TestCase):
    def test_linear_fill_projects_sensibly(self):
        # 1% per hour from 50% -> 50 hours (~2.08 days) to full.
        hour = 3600.0
        history = [(i * hour, 50.0 + i) for i in range(10)]
        days = estimate_days_until_full(history)
        self.assertIsNotNone(days)
        self.assertAlmostEqual(days, (100.0 - 59.0) / 24.0, places=1)

    def test_flat_history_has_no_projection(self):
        history = [(i * 3600.0, 42.0) for i in range(10)]
        self.assertIsNone(estimate_days_until_full(history))

    def test_shrinking_card_has_no_projection(self):
        history = [(i * 3600.0, 80.0 - i) for i in range(10)]
        self.assertIsNone(estimate_days_until_full(history))

    def test_too_few_points(self):
        self.assertIsNone(estimate_days_until_full([(0.0, 10.0), (3600.0, 20.0)]))

    def test_too_short_a_span(self):
        history = [(i * 60.0, 50.0 + i) for i in range(10)]  # 10 minutes
        self.assertIsNone(estimate_days_until_full(history))

    def test_very_slow_fill_is_not_reported(self):
        """Beyond ~60 days the extrapolation is fiction, so report nothing."""
        history = [(i * 3600.0, 10.0 + i * 0.001) for i in range(48)]
        self.assertIsNone(estimate_days_until_full(history))

    def test_already_full(self):
        history = [(i * 3600.0, 99.0 + i * 0.2) for i in range(10)]
        self.assertEqual(estimate_days_until_full(history), 0.0)


class TestFormatting(unittest.TestCase):
    def test_bytes(self):
        self.assertEqual(format_bytes(0), "0 B")
        self.assertEqual(format_bytes(512), "512 B")
        self.assertEqual(format_bytes(1024), "1.0 KiB")
        self.assertEqual(format_bytes(64 * GIB), "64.0 GiB")

    def test_duration(self):
        self.assertEqual(format_duration(30), "30s")
        self.assertEqual(format_duration(90), "1 min")
        self.assertEqual(format_duration(7200), "2h")
        self.assertEqual(format_duration(86400 * 5), "5 days")


if __name__ == "__main__":
    unittest.main()
