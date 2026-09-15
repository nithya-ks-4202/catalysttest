"""Turn SNMP varbinds into a camera's SD card status.

The interesting work is `pick_sd_card`: an agent hands us a storage table with
RAM, the root filesystem and (hopefully) the card all mixed together, and we
have to decide which row is the recording medium. Getting that wrong is worse
than reporting nothing, because a root filesystem sitting at 96% would page
someone about an SD card that is actually fine.
"""

from __future__ import annotations

import concurrent.futures
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

from . import mibs, snmp
from .config import AppConfig, CameraConfig, Thresholds
from .health import Severity, evaluate

# Values the vendor status OID may return that mean "the card is not usable".
READ_ONLY_TOKENS = ("readonly", "read-only", "read only", "ro", "locked", "protected")
ABSENT_TOKENS = ("absent", "missing", "none", "noCard", "no card", "unplugged", "0")


@dataclass
class StorageRow:
    """One row of hrStorageTable, in bytes rather than allocation units."""

    index: int
    descr: str = ""
    type_oid: tuple[int, ...] | None = None
    allocation_units: int = 0
    size_units: int = 0
    used_units: int = 0
    allocation_failures: int = 0

    @property
    def size_bytes(self) -> int:
        return max(0, self.size_units) * max(0, self.allocation_units)

    @property
    def used_bytes(self) -> int:
        return max(0, self.used_units) * max(0, self.allocation_units)

    @property
    def free_bytes(self) -> int:
        return max(0, self.size_bytes - self.used_bytes)

    @property
    def used_percent(self) -> float | None:
        if self.size_bytes <= 0:
            return None
        return min(100.0, (self.used_bytes / self.size_bytes) * 100.0)

    @property
    def is_removable(self) -> bool:
        return self.type_oid == mibs.HR_STORAGE_TYPE_REMOVABLE_DISK

    @property
    def is_memory(self) -> bool:
        return self.type_oid in mibs.NON_DISK_STORAGE_TYPES


@dataclass
class CameraSample:
    """Everything one poll learned about one camera."""

    camera_id: str
    name: str
    host: str
    site: str = ""
    tags: tuple[str, ...] = ()
    timestamp: float = field(default_factory=time.time)

    reachable: bool = False
    error: str | None = None
    rtt_ms: float | None = None

    sys_name: str = ""
    sys_descr: str = ""
    uptime_seconds: float | None = None

    sd_present: bool = False
    sd_label: str = ""
    sd_total_bytes: int = 0
    sd_used_bytes: int = 0
    sd_used_percent: float | None = None
    sd_read_only: bool = False
    sd_write_errors: int = 0
    sd_health_percent: int | None = None
    sd_source: str = ""  # which MIB the numbers came from

    severity: str = Severity.UNKNOWN
    issues: list[str] = field(default_factory=list)
    # Filled in by the store once history is available.
    days_until_full: float | None = None

    @property
    def sd_free_bytes(self) -> int:
        return max(0, self.sd_total_bytes - self.sd_used_bytes)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["tags"] = list(self.tags)
        data["sd_free_bytes"] = self.sd_free_bytes
        return data


# =========================================================================
# Table reassembly
# =========================================================================


def rows_from_walk(binds: Iterable[snmp.VarBind], table_root: str,
                   columns: dict[int, str]) -> dict[int, dict[str, Any]]:
    """Group a table walk back into rows keyed by their table index.

    A walked table arrives column-major (all of column 1, then all of column 2),
    so we bucket by the trailing index arcs and reassemble.
    """
    root = snmp.ber.parse_oid(table_root)
    rows: dict[int, dict[str, Any]] = {}
    for bind in binds:
        if bind.oid[: len(root)] != root or len(bind.oid) <= len(root):
            continue
        column = bind.oid[len(root)]
        if column not in columns:
            continue
        suffix = bind.oid[len(root) + 1:]
        if not suffix:
            continue
        # Multi-arc indexes exist in the wild; the whole suffix is the key.
        key = suffix[0] if len(suffix) == 1 else hash(suffix) & 0x7FFFFFFF
        rows.setdefault(key, {"_index_arcs": suffix})[columns[column]] = bind
    return rows


def parse_storage_rows(binds: Iterable[snmp.VarBind]) -> list[StorageRow]:
    grouped = rows_from_walk(binds, mibs.HR_STORAGE_TABLE, mibs.HR_STORAGE_COLUMNS)
    rows: list[StorageRow] = []
    for key, columns in sorted(grouped.items()):
        index_bind = columns.get("index")
        type_bind = columns.get("type")
        rows.append(
            StorageRow(
                index=index_bind.as_int(key) if index_bind else key,
                descr=columns["descr"].as_text() if "descr" in columns else "",
                type_oid=(type_bind.value
                          if type_bind and isinstance(type_bind.value, tuple) else None),
                allocation_units=(columns["allocation_units"].as_int(0) or 0
                                  if "allocation_units" in columns else 0),
                size_units=(columns["size"].as_int(0) or 0) if "size" in columns else 0,
                used_units=(columns["used"].as_int(0) or 0) if "used" in columns else 0,
                allocation_failures=((columns["allocation_failures"].as_int(0) or 0)
                                     if "allocation_failures" in columns else 0),
            )
        )
    return rows


def pick_sd_card(rows: list[StorageRow], camera: CameraConfig,
                 thresholds: Thresholds) -> StorageRow | None:
    """Choose the row that is the camera's SD card.

    Precedence, strongest signal first:
      1. An explicit `sd_storage_index` in the config - the operator has looked.
      2. A row whose description matches an SD pattern, most specific first.
      3. A row typed as a removable disk.
    Memory rows and implausibly small volumes never qualify.
    """
    candidates = [
        row for row in rows
        if not row.is_memory and row.size_bytes >= thresholds.min_plausible_sd_bytes
    ]
    if not candidates:
        return None

    if camera.sd_storage_index is not None:
        for row in candidates:
            if row.index == camera.sd_storage_index:
                return row
        return None  # operator named an index that isn't there - say so, don't guess

    named = [row for row in candidates if mibs.matches_sd(row.descr, camera.sd_patterns)]
    if named:
        # Most specific pattern wins; ties go to the removable one, then the
        # largest, since the recording card is usually the biggest volume.
        named.sort(
            key=lambda r: (
                mibs.sd_match_rank(r.descr, camera.sd_patterns),
                not r.is_removable,
                -r.size_bytes,
            )
        )
        return named[0]

    removable = [row for row in candidates if row.is_removable]
    if removable:
        removable.sort(key=lambda r: -r.size_bytes)
        return removable[0]

    return None


def parse_ucd_disks(binds: Iterable[snmp.VarBind]) -> list[dict[str, Any]]:
    grouped = rows_from_walk(binds, mibs.UCD_DISK_TABLE, mibs.UCD_DISK_COLUMNS)
    out = []
    for key, columns in sorted(grouped.items()):
        out.append({
            "index": key,
            "path": columns["path"].as_text() if "path" in columns else "",
            "total_kb": (columns["total_kb"].as_int(0) or 0) if "total_kb" in columns else 0,
            "used_kb": (columns["used_kb"].as_int(0) or 0) if "used_kb" in columns else 0,
            "error_flag": (columns["error_flag"].as_int(0) or 0) if "error_flag" in columns else 0,
            "error_msg": columns["error_msg"].as_text() if "error_msg" in columns else "",
        })
    return out


# =========================================================================
# Polling
# =========================================================================


def poll_camera(camera: CameraConfig, thresholds: Thresholds) -> CameraSample:
    """Poll one camera. Never raises - failures become an unreachable sample."""
    sample = CameraSample(
        camera_id=camera.id,
        name=camera.name,
        host=f"{camera.host}:{camera.port}",
        site=camera.site,
        tags=camera.tags,
    )

    try:
        with snmp.Session(camera.snmp_config()) as session:
            system = session.get(mibs.SYS_NAME, mibs.SYS_DESCR, mibs.SYS_UPTIME)
            sample.reachable = True
            sample.rtt_ms = session.last_rtt_ms

            by_oid = {bind.oid_str: bind for bind in system}
            name_bind = by_oid.get(mibs.SYS_NAME)
            descr_bind = by_oid.get(mibs.SYS_DESCR)
            uptime_bind = by_oid.get(mibs.SYS_UPTIME)
            if name_bind and not name_bind.is_exception:
                sample.sys_name = name_bind.as_text()
            if descr_bind and not descr_bind.is_exception:
                sample.sys_descr = descr_bind.as_text()
            if uptime_bind and not uptime_bind.is_exception:
                ticks = uptime_bind.as_int()
                if ticks is not None:
                    sample.uptime_seconds = ticks / 100.0  # TimeTicks are 1/100s

            _collect_sd_status(session, camera, thresholds, sample)

    except snmp.SnmpTimeout as exc:
        sample.reachable = False
        sample.error = str(exc)
    except snmp.SnmpError as exc:
        sample.reachable = False
        sample.error = str(exc)
    except OSError as exc:
        sample.reachable = False
        sample.error = f"{camera.host}: {exc}"

    evaluate(sample, thresholds)
    return sample


def _collect_sd_status(session: snmp.Session, camera: CameraConfig,
                       thresholds: Thresholds, sample: CameraSample) -> None:
    """Fill the sd_* fields, trying each source in turn."""
    # --- 1. explicit vendor OIDs -----------------------------------------
    if camera.sd_size_oid and camera.sd_used_oid:
        size = session.get_one(camera.sd_size_oid)
        used = session.get_one(camera.sd_used_oid)
        if size and used:
            total = size.as_int(0) or 0
            consumed = used.as_int(0) or 0
            if total > 0:
                sample.sd_present = True
                sample.sd_source = "vendor"
                sample.sd_label = "SD card (vendor MIB)"
                sample.sd_total_bytes = total
                sample.sd_used_bytes = min(consumed, total)
                sample.sd_used_percent = min(100.0, consumed / total * 100.0)

    # --- 2. HOST-RESOURCES-MIB -------------------------------------------
    if not sample.sd_present:
        try:
            rows = parse_storage_rows(session.walk(mibs.HR_STORAGE_TABLE))
        except snmp.SnmpError:
            rows = []
        row = pick_sd_card(rows, camera, thresholds)
        if row is not None:
            sample.sd_present = True
            sample.sd_source = "hrStorage"
            sample.sd_label = row.descr or f"storage index {row.index}"
            sample.sd_total_bytes = row.size_bytes
            sample.sd_used_bytes = row.used_bytes
            sample.sd_used_percent = row.used_percent
            if row.allocation_failures:
                sample.sd_write_errors = row.allocation_failures

    # --- 3. UCD-SNMP dskTable --------------------------------------------
    if not sample.sd_present:
        try:
            disks = parse_ucd_disks(session.walk(mibs.UCD_DISK_TABLE))
        except snmp.SnmpError:
            disks = []
        for disk in disks:
            if not mibs.matches_sd(disk["path"], camera.sd_patterns):
                continue
            total = disk["total_kb"] * 1024
            if total < thresholds.min_plausible_sd_bytes:
                continue
            sample.sd_present = True
            sample.sd_source = "ucdDisk"
            sample.sd_label = disk["path"]
            sample.sd_total_bytes = total
            sample.sd_used_bytes = min(disk["used_kb"] * 1024, total)
            sample.sd_used_percent = min(100.0, sample.sd_used_bytes / total * 100.0)
            if disk["error_flag"]:
                sample.issues.append(disk["error_msg"] or "disk error flag set")
            break

    # --- optional extras --------------------------------------------------
    if camera.sd_status_oid:
        status = session.get_one(camera.sd_status_oid)
        if status is not None:
            text = status.as_text().strip().lower()
            if any(token in text for token in READ_ONLY_TOKENS):
                sample.sd_read_only = True
            if text and any(text == token.lower() for token in ABSENT_TOKENS):
                sample.sd_present = False

    if camera.sd_health_oid:
        health = session.get_one(camera.sd_health_oid)
        if health is not None:
            value = health.as_int()
            if value is not None and 0 <= value <= 100:
                sample.sd_health_percent = value

    if camera.sd_write_errors_oid:
        errors = session.get_one(camera.sd_write_errors_oid)
        if errors is not None:
            sample.sd_write_errors = max(sample.sd_write_errors, errors.as_int(0) or 0)


def poll_fleet(config: AppConfig) -> list[CameraSample]:
    """Poll every enabled camera concurrently and return the samples in
    config order (so the dashboard's list doesn't reshuffle each cycle)."""
    cameras = config.enabled_cameras
    if not cameras:
        return []
    workers = max(1, min(config.max_workers, len(cameras)))
    results: dict[str, CameraSample] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers,
                                               thread_name_prefix="poll") as pool:
        futures = {
            pool.submit(poll_camera, camera, config.thresholds): camera
            for camera in cameras
        }
        for future in concurrent.futures.as_completed(futures):
            camera = futures[future]
            try:
                results[camera.id] = future.result()
            except Exception as exc:  # pragma: no cover - poll_camera catches its own
                sample = CameraSample(
                    camera_id=camera.id, name=camera.name,
                    host=f"{camera.host}:{camera.port}", site=camera.site,
                    tags=camera.tags, reachable=False,
                    error=f"internal poll error: {exc}",
                )
                evaluate(sample, config.thresholds)
                results[camera.id] = sample
    return [results[c.id] for c in cameras if c.id in results]
