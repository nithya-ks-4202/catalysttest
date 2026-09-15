"""Loading and validating cameras.json."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
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
    return _config_from_raw(raw, str(path))


def _config_from_raw(raw: dict[str, Any], path: str) -> AppConfig:
    """Turn an already-parsed config document into an AppConfig.

    Shared by `load_config` and `ConfigStore.write_raw`, so an edit made
    through the API is validated by exactly the same rules as a hand-written
    file - a change the server accepts is always one that will still load on
    the next restart.
    """
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


# Fields a camera entry may carry. Anything else in a submitted entry is
# rejected rather than silently written into the file.
CAMERA_FIELDS = frozenset({
    "id", "name", "host", "port", "community", "version", "timeout", "retries",
    "site", "tags", "sd_patterns", "sd_storage_index", "sd_size_oid",
    "sd_used_oid", "sd_status_oid", "sd_health_oid", "sd_write_errors_oid",
    "enabled",
})


class ConfigStore:
    """Read/modify/write cameras.json safely.

    Everything goes through the *raw* JSON rather than through AppConfig,
    because AppConfig has already expanded ``${VAR}`` placeholders. Writing a
    parsed config back out would bake the resolved community string into the
    file - turning a deliberately externalised secret into a plaintext one, in
    a file people commit. Round-tripping the raw dict keeps the placeholder.

    Writes are atomic (temp file + rename) and validated by re-parsing before
    they replace the original, so a rejected edit leaves the file untouched.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def read_raw(self) -> dict[str, Any]:
        if not self.path.exists():
            raise ConfigError(f"config file not found: {self.path}")
        try:
            raw = json.loads(self.path.read_text())
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{self.path}: invalid JSON - {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"{self.path}: top level must be an object")
        raw.setdefault("cameras", [])
        return raw

    def write_raw(self, raw: dict[str, Any]) -> AppConfig:
        """Validate, then atomically replace the file. Returns the new config."""
        with self._lock:
            # Parse a deep copy first: if this raises, the file is untouched.
            config = _config_from_raw(json.loads(json.dumps(raw)), str(self.path))

            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                "w", delete=False, dir=str(self.path.parent),
                prefix=f".{self.path.name}.", suffix=".tmp",
            )
            try:
                with handle as fh:
                    json.dump(raw, fh, indent=2)
                    fh.write("\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(handle.name, self.path)
            except BaseException:
                # Don't leave a stray temp file behind on failure.
                try:
                    os.unlink(handle.name)
                except OSError:
                    pass
                raise
            return config

    # -- camera operations -------------------------------------------------

    def list_cameras(self) -> list[dict[str, Any]]:
        return list(self.read_raw().get("cameras", []))

    def add_camera(self, entry: dict[str, Any]) -> tuple[AppConfig, dict[str, Any]]:
        with self._lock:
            raw = self.read_raw()
            cleaned = _clean_camera_entry(entry)
            existing = {c.get("id") for c in raw["cameras"] if isinstance(c, dict)}

            if not cleaned.get("id"):
                cleaned["id"] = _unique_id(
                    _slugify(cleaned.get("name") or cleaned["host"]), existing)
            elif cleaned["id"] in existing:
                raise ConfigError(f"a camera with id {cleaned['id']!r} already exists")

            raw["cameras"].append(cleaned)
            return self.write_raw(raw), cleaned

    def update_camera(self, camera_id: str,
                      patch: dict[str, Any]) -> tuple[AppConfig, dict[str, Any]]:
        with self._lock:
            raw = self.read_raw()
            index = _find_camera(raw["cameras"], camera_id)
            cleaned = _clean_camera_entry(patch, require_host=False)

            if "id" in cleaned and cleaned["id"] != camera_id:
                others = {c.get("id") for i, c in enumerate(raw["cameras"])
                          if i != index and isinstance(c, dict)}
                if cleaned["id"] in others:
                    raise ConfigError(
                        f"a camera with id {cleaned['id']!r} already exists")

            entry = dict(raw["cameras"][index])
            for key, value in cleaned.items():
                # An explicit null clears a field back to its default.
                if value is None:
                    entry.pop(key, None)
                else:
                    entry[key] = value
            raw["cameras"][index] = entry
            return self.write_raw(raw), entry

    def remove_camera(self, camera_id: str) -> AppConfig:
        with self._lock:
            raw = self.read_raw()
            index = _find_camera(raw["cameras"], camera_id)
            raw["cameras"].pop(index)
            if not raw["cameras"]:
                raise ConfigError(
                    "cannot remove the last camera - the config needs at least one")
            return self.write_raw(raw)


def _find_camera(cameras: list[Any], camera_id: str) -> int:
    for index, entry in enumerate(cameras):
        if isinstance(entry, dict) and entry.get("id") == camera_id:
            return index
    raise ConfigError(f"no camera with id {camera_id!r}")


def _clean_camera_entry(entry: dict[str, Any],
                        require_host: bool = True) -> dict[str, Any]:
    """Validate a submitted camera entry and drop anything unrecognised."""
    if not isinstance(entry, dict):
        raise ConfigError("camera entry must be an object")

    unknown = set(entry) - CAMERA_FIELDS
    # Tolerate the "_comment_*" keys used in cameras.example.json.
    unknown = {key for key in unknown if not key.startswith("_")}
    if unknown:
        raise ConfigError(f"unknown field(s): {', '.join(sorted(unknown))}")

    cleaned: dict[str, Any] = {}
    for key, value in entry.items():
        if key.startswith("_"):
            continue
        cleaned[key] = value

    if require_host and not str(cleaned.get("host", "")).strip():
        raise ConfigError("host is required")
    if "host" in cleaned:
        cleaned["host"] = str(cleaned["host"]).strip()
        if not cleaned["host"]:
            raise ConfigError("host is required")

    if "port" in cleaned and cleaned["port"] is not None:
        try:
            cleaned["port"] = int(cleaned["port"])
        except (TypeError, ValueError):
            raise ConfigError("port must be a number")
        if not 1 <= cleaned["port"] <= 65535:
            raise ConfigError(f"port {cleaned['port']} is out of range")

    if "version" in cleaned and cleaned["version"] is not None:
        from . import snmp as _snmp
        try:
            _snmp.parse_version(cleaned["version"])
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc

    for key in ("timeout", "retries"):
        if key in cleaned and cleaned[key] is not None:
            try:
                cleaned[key] = float(cleaned[key]) if key == "timeout" \
                    else int(cleaned[key])
            except (TypeError, ValueError):
                raise ConfigError(f"{key} must be a number")
            if cleaned[key] < 0:
                raise ConfigError(f"{key} cannot be negative")

    if "sd_storage_index" in cleaned and cleaned["sd_storage_index"] is not None:
        try:
            cleaned["sd_storage_index"] = int(cleaned["sd_storage_index"])
        except (TypeError, ValueError):
            raise ConfigError("sd_storage_index must be a whole number")

    for key in ("tags", "sd_patterns"):
        if key in cleaned and cleaned[key] is not None:
            if not isinstance(cleaned[key], list) or \
                    not all(isinstance(v, str) for v in cleaned[key]):
                raise ConfigError(f"{key} must be a list of strings")

    for key in ("sd_size_oid", "sd_used_oid", "sd_status_oid", "sd_health_oid",
                "sd_write_errors_oid"):
        if cleaned.get(key):
            from .ber import BERError, parse_oid
            try:
                parse_oid(str(cleaned[key]))
            except BERError as exc:
                raise ConfigError(f"{key}: {exc}") from exc

    if "enabled" in cleaned and cleaned["enabled"] is not None:
        cleaned["enabled"] = bool(cleaned["enabled"])

    if "id" in cleaned and cleaned["id"] is not None:
        cleaned["id"] = str(cleaned["id"]).strip()
        if not cleaned["id"]:
            raise ConfigError("id cannot be blank")

    return cleaned


def _slugify(text: str) -> str:
    slug = "".join(c.lower() if c.isalnum() else "-" for c in str(text)).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug[:32] or "camera"


def _unique_id(base: str, existing: set[str | None]) -> str:
    candidate = f"cam-{base}"
    if candidate not in existing:
        return candidate
    for n in range(2, 1000):
        numbered = f"cam-{base}-{n}"
        if numbered not in existing:
            return numbered
    raise ConfigError("could not generate a unique camera id")
