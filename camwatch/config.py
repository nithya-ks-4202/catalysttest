"""Loading and validating cameras.json."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import snmp
from .mibs import DEFAULT_SD_PATTERNS

ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(Exception):
    """Raised for a malformed or unusable config file."""


@dataclass
class Thresholds:
    capacity_warning_percent: float = 75.0
    capacity_critical_percent: float = 90.0
    # A camera that rebooted this recently gets flagged: repeated reboots are a
    # classic symptom of a card the firmware can no longer write to.
    recent_reboot_seconds: int = 900
    # Cards this small are almost certainly a mis-detected partition, not the
    # recording medium.
    min_plausible_sd_bytes: int = 64 * 1024 * 1024
    write_error_warning: int = 1

    def validate(self) -> None:
        if not 0 < self.capacity_warning_percent < 100:
            raise ConfigError("capacity_warning_percent must be between 0 and 100")
        if not 0 < self.capacity_critical_percent <= 100:
            raise ConfigError("capacity_critical_percent must be between 0 and 100")
        if self.capacity_warning_percent >= self.capacity_critical_percent:
            raise ConfigError(
                "capacity_warning_percent must be below capacity_critical_percent"
            )


@dataclass
class CameraConfig:
    id: str
    name: str
    host: str
    port: int = 161
    community: str = "public"
    version: int = snmp.VERSION_V2C
    timeout: float = 2.0
    retries: int = 1
    site: str = ""
    # Optional per-camera overrides for vendor MIBs.
    sd_patterns: tuple[str, ...] = DEFAULT_SD_PATTERNS
    sd_storage_index: int | None = None
    sd_size_oid: str | None = None
    sd_used_oid: str | None = None
    sd_status_oid: str | None = None
    sd_health_oid: str | None = None
    sd_write_errors_oid: str | None = None
    enabled: bool = True
    tags: tuple[str, ...] = ()

    def snmp_config(self) -> snmp.SnmpConfig:
        return snmp.SnmpConfig(
            host=self.host,
            community=self.community,
            port=self.port,
            version=self.version,
            timeout=self.timeout,
            retries=self.retries,
        )


@dataclass
class AppConfig:
    cameras: list[CameraConfig] = field(default_factory=list)
    thresholds: Thresholds = field(default_factory=Thresholds)
    poll_interval_seconds: int = 60
    history_days: int = 30
    database_path: str = "camwatch.db"
    # How many cameras to poll at once. SNMP polls are almost entirely waiting
    # on the network, so threads are the right tool and the number can be high.
    max_workers: int = 16

    @property
    def enabled_cameras(self) -> list[CameraConfig]:
        return [c for c in self.cameras if c.enabled]


def _expand_env(value: Any) -> Any:
    """Expand ${VAR} references so community strings can live outside the file."""
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ConfigError(
                f"config references ${{{name}}} but that environment variable is not set"
            )
        return os.environ[name]

    return ENV_PATTERN.sub(replace, value)


def _require(entry: dict[str, Any], key: str, where: str) -> Any:
    if key not in entry or entry[key] in (None, ""):
        raise ConfigError(f"{where}: missing required field {key!r}")
    return entry[key]


def parse_camera(entry: dict[str, Any], index: int,
                 defaults: dict[str, Any]) -> CameraConfig:
    where = f"cameras[{index}]"
    if not isinstance(entry, dict):
        raise ConfigError(f"{where}: expected an object")

    merged = {**defaults, **entry}
    host = str(_expand_env(_require(merged, "host", where)))
    cam_id = str(merged.get("id") or f"cam-{index + 1:02d}")
    name = str(merged.get("name") or host)

    try:
        version = snmp.parse_version(merged.get("version", "v2c"))
    except ValueError as exc:
        raise ConfigError(f"{where}: {exc}") from exc

    port = int(merged.get("port", 161))
    if not 1 <= port <= 65535:
        raise ConfigError(f"{where}: port {port} is out of range")

    patterns = merged.get("sd_patterns")
    if patterns is None:
        sd_patterns = DEFAULT_SD_PATTERNS
    else:
        if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
            raise ConfigError(f"{where}: sd_patterns must be a list of strings")
        sd_patterns = tuple(p.lower() for p in patterns)

    tags = merged.get("tags", [])
    if not isinstance(tags, list):
        raise ConfigError(f"{where}: tags must be a list")

    return CameraConfig(
        id=cam_id,
        name=name,
        host=host,
        port=port,
        community=str(_expand_env(merged.get("community", "public"))),
        version=version,
        timeout=float(merged.get("timeout", 2.0)),
        retries=int(merged.get("retries", 1)),
        site=str(merged.get("site", "")),
        sd_patterns=sd_patterns,
        sd_storage_index=(int(merged["sd_storage_index"])
                          if merged.get("sd_storage_index") is not None else None),
        sd_size_oid=merged.get("sd_size_oid"),
        sd_used_oid=merged.get("sd_used_oid"),
        sd_status_oid=merged.get("sd_status_oid"),
        sd_health_oid=merged.get("sd_health_oid"),
        sd_write_errors_oid=merged.get("sd_write_errors_oid"),
        enabled=bool(merged.get("enabled", True)),
        tags=tuple(str(t) for t in tags),
    )


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"config file not found: {path}\n"
            "Copy cameras.example.json to cameras.json, or run "
            "`python3 tools/fake_camera.py --write-config cameras.json` for a demo fleet."
        )
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path}: invalid JSON - {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be an object")

    entries = raw.get("cameras")
    if not isinstance(entries, list) or not entries:
        raise ConfigError(f"{path}: 'cameras' must be a non-empty list")

    defaults = raw.get("defaults", {})
    if not isinstance(defaults, dict):
        raise ConfigError(f"{path}: 'defaults' must be an object")

    cameras = [parse_camera(entry, i, defaults) for i, entry in enumerate(entries)]

    seen: set[str] = set()
    for camera in cameras:
        if camera.id in seen:
            raise ConfigError(f"{path}: duplicate camera id {camera.id!r}")
        seen.add(camera.id)

    thresholds = Thresholds(**{
        key: value
        for key, value in (raw.get("thresholds") or {}).items()
        if key in Thresholds.__dataclass_fields__
    })
    thresholds.validate()

    interval = int(raw.get("poll_interval_seconds", 60))
    if interval < 5:
        raise ConfigError("poll_interval_seconds must be at least 5")

    return AppConfig(
        cameras=cameras,
        thresholds=thresholds,
        poll_interval_seconds=interval,
        history_days=int(raw.get("history_days", 30)),
        database_path=str(raw.get("database_path", "camwatch.db")),
        max_workers=int(raw.get("max_workers", 16)),
    )
