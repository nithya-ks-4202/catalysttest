"""Severity rules - the opinions about what counts as a problem.

Kept apart from the poller so the thresholds are easy to find and argue with,
and so they can be unit-tested without any network in the picture.

The severity vocabulary matches the dashboard's status palette exactly:
good / warning / serious / critical, plus unknown for "we couldn't tell".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import Thresholds
    from .poller import CameraSample


class Severity:
    GOOD = "good"
    WARNING = "warning"
    SERIOUS = "serious"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


# Worst-wins ordering when several rules fire at once.
SEVERITY_RANK = {
    Severity.GOOD: 0,
    Severity.UNKNOWN: 1,
    Severity.WARNING: 2,
    Severity.SERIOUS: 3,
    Severity.CRITICAL: 4,
}


def worst(*severities: str) -> str:
    return max(severities, key=lambda s: SEVERITY_RANK.get(s, 0))


def evaluate(sample: "CameraSample", thresholds: "Thresholds") -> str:
    """Set `sample.severity` and `sample.issues` in place; return the severity."""
    issues: list[str] = list(sample.issues)
    severity = Severity.GOOD

    # An unreachable camera is the loudest thing on the board: we know nothing
    # about its recording state, which in practice means it isn't recording.
    if not sample.reachable:
        sample.severity = Severity.CRITICAL
        detail = sample.error or "no SNMP response"
        sample.issues = [f"Camera unreachable ({detail})"]
        return sample.severity

    if not sample.sd_present:
        severity = worst(severity, Severity.CRITICAL)
        issues.append("No SD card detected")
    else:
        percent = sample.sd_used_percent
        if percent is None:
            severity = worst(severity, Severity.UNKNOWN)
            issues.append("SD card reported no capacity")
        elif percent >= thresholds.capacity_critical_percent:
            severity = worst(severity, Severity.CRITICAL)
            issues.append(f"SD card {percent:.0f}% full")
        elif percent >= thresholds.capacity_warning_percent:
            severity = worst(severity, Severity.WARNING)
            issues.append(f"SD card {percent:.0f}% full")

        if sample.sd_read_only:
            severity = worst(severity, Severity.CRITICAL)
            issues.append("SD card is read-only - recording will fail")

        if sample.sd_write_errors >= thresholds.write_error_warning:
            # Write errors on flash mean the card is wearing out. Serious rather
            # than critical: it is still recording, but it is on its way out.
            severity = worst(severity, Severity.SERIOUS)
            issues.append(f"{sample.sd_write_errors:,} write errors reported")

        if sample.sd_health_percent is not None:
            if sample.sd_health_percent <= 20:
                severity = worst(severity, Severity.CRITICAL)
                issues.append(f"SD card health {sample.sd_health_percent}%")
            elif sample.sd_health_percent <= 50:
                severity = worst(severity, Severity.SERIOUS)
                issues.append(f"SD card health {sample.sd_health_percent}%")

    if (sample.uptime_seconds is not None
            and sample.uptime_seconds < thresholds.recent_reboot_seconds):
        severity = worst(severity, Severity.WARNING)
        issues.append(f"Rebooted {format_duration(sample.uptime_seconds)} ago")

    sample.severity = severity
    sample.issues = issues
    return severity


def format_duration(seconds: float) -> str:
    """Human-readable duration, coarse on purpose ("3 days", not "3d 4h 12m")."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24} days"


def format_bytes(value: float) -> str:
    """Binary units, matching how cameras report card sizes."""
    value = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024.0 or unit == "TiB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TiB"  # pragma: no cover - loop always returns


def estimate_days_until_full(history: list[tuple[float, float]]) -> float | None:
    """Least-squares fit of used-% over time -> days until the card hits 100%.

    `history` is [(unix_timestamp, used_percent), ...]. Returns None when the
    card is shrinking, flat, already full, or there is too little history to
    say anything honest.
    """
    points = [(t, p) for t, p in history if p is not None]
    if len(points) < 4:
        return None

    span_seconds = points[-1][0] - points[0][0]
    if span_seconds < 3600:
        return None  # less than an hour of history: any slope is noise

    n = len(points)
    mean_t = sum(t for t, _ in points) / n
    mean_p = sum(p for _, p in points) / n
    numerator = sum((t - mean_t) * (p - mean_p) for t, p in points)
    denominator = sum((t - mean_t) ** 2 for t, _ in points)
    if denominator == 0:
        return None

    slope_per_second = numerator / denominator
    if slope_per_second <= 0:
        return None  # not filling (or being rotated), so "until full" is meaningless

    current = points[-1][1]
    remaining = 100.0 - current
    if remaining <= 0:
        return 0.0
    days = (remaining / slope_per_second) / 86400.0
    # Past a couple of months the extrapolation is fiction; don't print it.
    return round(days, 2) if days <= 60 else None
