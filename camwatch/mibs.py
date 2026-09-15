"""OIDs we poll, and the rules for recognising an SD card among them.

Three sources, tried in order:

1. HOST-RESOURCES-MIB `hrStorageTable` - the generic one. Most cameras running
   an embedded Linux with net-snmp expose the SD card here as a removable disk.
2. UCD-SNMP-MIB `dskTable` - older net-snmp builds, and anything where the
   operator configured `disk /mnt/sd` explicitly.
3. Vendor OIDs from the camera's own config entry - the escape hatch for
   cameras with a private MIB (Axis, Hikvision, Dahua and friends all have one).
"""

from __future__ import annotations

# --- SNMPv2-MIB / system group -------------------------------------------
SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
SYS_CONTACT = "1.3.6.1.2.1.1.4.0"
SYS_NAME = "1.3.6.1.2.1.1.5.0"
SYS_LOCATION = "1.3.6.1.2.1.1.6.0"

# --- HOST-RESOURCES-MIB ---------------------------------------------------
HR_STORAGE_TABLE = "1.3.6.1.2.1.25.2.3.1"
HR_STORAGE_INDEX = "1.3.6.1.2.1.25.2.3.1.1"
HR_STORAGE_TYPE = "1.3.6.1.2.1.25.2.3.1.2"
HR_STORAGE_DESCR = "1.3.6.1.2.1.25.2.3.1.3"
HR_STORAGE_ALLOCATION_UNITS = "1.3.6.1.2.1.25.2.3.1.4"
HR_STORAGE_SIZE = "1.3.6.1.2.1.25.2.3.1.5"
HR_STORAGE_USED = "1.3.6.1.2.1.25.2.3.1.6"
HR_STORAGE_ALLOCATION_FAILURES = "1.3.6.1.2.1.25.2.3.1.7"

# Column number -> field name, for reassembling walked rows.
HR_STORAGE_COLUMNS = {
    1: "index",
    2: "type",
    3: "descr",
    4: "allocation_units",
    5: "size",
    6: "used",
    7: "allocation_failures",
}

# hrStorageTypes - the ones worth distinguishing.
HR_STORAGE_TYPE_OTHER = (1, 3, 6, 1, 2, 1, 25, 2, 1, 1)
HR_STORAGE_TYPE_RAM = (1, 3, 6, 1, 2, 1, 25, 2, 1, 2)
HR_STORAGE_TYPE_VIRTUAL_MEMORY = (1, 3, 6, 1, 2, 1, 25, 2, 1, 3)
HR_STORAGE_TYPE_FIXED_DISK = (1, 3, 6, 1, 2, 1, 25, 2, 1, 4)
HR_STORAGE_TYPE_REMOVABLE_DISK = (1, 3, 6, 1, 2, 1, 25, 2, 1, 5)
HR_STORAGE_TYPE_FLOPPY = (1, 3, 6, 1, 2, 1, 25, 2, 1, 6)

# Memory-ish rows are never the SD card, so they are excluded up front.
NON_DISK_STORAGE_TYPES = frozenset(
    {HR_STORAGE_TYPE_RAM, HR_STORAGE_TYPE_VIRTUAL_MEMORY}
)

# --- UCD-SNMP-MIB dskTable ------------------------------------------------
UCD_DISK_TABLE = "1.3.6.1.4.1.2021.9.1"
UCD_DISK_PATH = "1.3.6.1.4.1.2021.9.1.2"
UCD_DISK_TOTAL_KB = "1.3.6.1.4.1.2021.9.1.6"
UCD_DISK_USED_KB = "1.3.6.1.4.1.2021.9.1.8"
UCD_DISK_PERCENT = "1.3.6.1.4.1.2021.9.1.9"
UCD_DISK_ERROR_FLAG = "1.3.6.1.4.1.2021.9.1.100"
UCD_DISK_ERROR_MSG = "1.3.6.1.4.1.2021.9.1.101"

UCD_DISK_COLUMNS = {
    2: "path",
    6: "total_kb",
    8: "used_kb",
    9: "percent",
    100: "error_flag",
    101: "error_msg",
}

# --- SD card recognition --------------------------------------------------
# Matched case-insensitively as substrings of hrStorageDescr / dskPath. Ordered
# most- to least-specific; the first hit wins when several rows match.
DEFAULT_SD_PATTERNS: tuple[str, ...] = (
    "/mnt/sd",
    "/media/sd",
    "sdcard",
    "sd card",
    "sd_card",
    "mmcblk",
    "/mmc",
    "removable",
    "external",
    "storage",
)

# Rows that look like an SD card by name but are really the OS - skipped so a
# root filesystem at 95% does not get reported as a failing card.
SD_EXCLUDE_PATTERNS: tuple[str, ...] = (
    "swap",
    "tmpfs",
    "devtmpfs",
    "/proc",
    "/sys",
    "ram disk",
    "physical memory",
    "virtual memory",
    "memory buffers",
    "cached memory",
    "shared memory",
)


def matches_sd(description: str, patterns: tuple[str, ...] = DEFAULT_SD_PATTERNS) -> bool:
    """True if `description` names something that looks like a camera SD card."""
    text = description.lower()
    if any(bad in text for bad in SD_EXCLUDE_PATTERNS):
        return False
    return any(pattern in text for pattern in patterns)


def sd_match_rank(description: str, patterns: tuple[str, ...] = DEFAULT_SD_PATTERNS) -> int:
    """Index of the first matching pattern (lower = more specific).

    Used to choose between several candidate rows: "/mnt/sdcard" should win
    over a generic row that merely contains "storage".
    """
    text = description.lower()
    for rank, pattern in enumerate(patterns):
        if pattern in text:
            return rank
    return len(patterns)
