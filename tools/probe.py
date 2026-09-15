#!/usr/bin/env python3
"""Probe a camera over SNMP and work out how to monitor it.

The question this answers is "will camwatch work with my camera, and which
volume is the SD card?" - it dumps everything the agent exposes, marks the row
camwatch would pick, and prints a config entry you can paste straight in.

    # One camera
    python3 tools/probe.py 192.168.1.41
    python3 tools/probe.py 192.168.1.41 --community mysecret

    # Find cameras on the network first
    python3 tools/probe.py --scan 192.168.1.0/24

Exit status is 0 when a usable SD card was found, 1 otherwise, so this is
also usable as a check in a script.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import ipaddress
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from camwatch import mibs, snmp  # noqa: E402
from camwatch.config import CameraConfig, Thresholds  # noqa: E402
from camwatch.health import format_bytes  # noqa: E402
from camwatch.poller import (parse_storage_rows, parse_ucd_disks,  # noqa: E402
                             pick_sd_card, poll_camera)

# Communities worth trying when the user hasn't said which one to use. Almost
# every camera ships with one of these two.
COMMON_COMMUNITIES = ("public", "private")


def probe_host(host: str, community: str, port: int, version: int,
               timeout: float) -> dict | None:
    """Return basic system info if `host` answers SNMP, else None."""
    config = snmp.SnmpConfig(host=host, community=community, port=port,
                             version=version, timeout=timeout, retries=0)
    try:
        with snmp.Session(config) as session:
            binds = session.get(mibs.SYS_DESCR, mibs.SYS_NAME, mibs.SYS_UPTIME)
            by_oid = {b.oid_str: b for b in binds}
            return {
                "host": host,
                "port": port,
                "community": community,
                "version": "v2c" if version == snmp.VERSION_V2C else "v1",
                "descr": by_oid[mibs.SYS_DESCR].as_text()
                         if mibs.SYS_DESCR in by_oid else "",
                "name": by_oid[mibs.SYS_NAME].as_text()
                        if mibs.SYS_NAME in by_oid else "",
                "rtt_ms": session.last_rtt_ms,
            }
    except (snmp.SnmpError, OSError):
        return None


def autodetect(host: str, port: int, communities: list[str],
               timeout: float) -> dict | None:
    """Try each community against v2c then v1 until something answers."""
    for community in communities:
        for version in (snmp.VERSION_V2C, snmp.VERSION_V1):
            found = probe_host(host, community, port, version, timeout)
            if found:
                return found
    return None


# =========================================================================
# Subnet scan
# =========================================================================


def scan(network: str, port: int, communities: list[str], timeout: float,
         workers: int) -> list[dict]:
    try:
        net = ipaddress.ip_network(network, strict=False)
    except ValueError as exc:
        raise SystemExit(f"bad network {network!r}: {exc}")

    hosts = list(net.hosts()) if net.num_addresses > 2 else [net.network_address]
    if len(hosts) > 4096:
        raise SystemExit(
            f"{network} has {len(hosts)} addresses - scan a /20 or smaller")

    print(f"Scanning {len(hosts)} addresses on udp/{port} "
          f"(communities: {', '.join(communities)})…\n")

    found: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(autodetect, str(ip), port, communities, timeout): ip
            for ip in hosts
        }
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if result:
                found.append(result)
                print(f"  ✓ {result['host']:<16} {result['name'] or '(no sysName)'} "
                      f"— {result['descr'][:56]}")
    return sorted(found, key=lambda r: ipaddress.ip_address(r["host"]))


# =========================================================================
# Detailed single-host report
# =========================================================================


def report(host: str, community: str | None, port: int, version_text: str | None,
           timeout: float, as_json: bool) -> int:
    communities = [community] if community else list(COMMON_COMMUNITIES)
    versions = ([snmp.parse_version(version_text)] if version_text
                else [snmp.VERSION_V2C, snmp.VERSION_V1])

    info = None
    for comm in communities:
        for ver in versions:
            info = probe_host(host, comm, port, ver, timeout)
            if info:
                break
        if info:
            break

    if not info:
        print(f"✗ No SNMP response from {host}:{port}\n")
        print("Things to check:")
        print("  • Is SNMP enabled in the camera's web UI? It's usually off by")
        print("    default, under Network → Advanced → SNMP.")
        print("  • Is the community string right? Try --community <yours>.")
        print("  • Is it SNMPv3-only? camwatch does not support v3 (see README).")
        print("  • Is a firewall blocking udp/161 between here and the camera?")
        if community is None:
            print(f"\n(Tried communities: {', '.join(communities)})")
        return 1

    community = info["community"]
    version = snmp.parse_version(info["version"])

    print(f"✓ {host}:{port} answered SNMP {info['version']} "
          f"in {info['rtt_ms']:.0f} ms\n")
    print(f"  sysName   {info['name'] or '(empty)'}")
    print(f"  sysDescr  {info['descr'] or '(empty)'}")
    print(f"  community {community}")
    print()

    config = snmp.SnmpConfig(host=host, community=community, port=port,
                             version=version, timeout=timeout, retries=1)
    camera = CameraConfig(id="probe", name=info["name"] or host, host=host,
                          port=port, community=community, version=version)
    thresholds = Thresholds()

    with snmp.Session(config) as session:
        try:
            rows = parse_storage_rows(session.walk(mibs.HR_STORAGE_TABLE))
        except snmp.SnmpError as exc:
            rows = []
            print(f"  (hrStorageTable walk failed: {exc})")

        chosen = pick_sd_card(rows, camera, thresholds) if rows else None

        if rows:
            print("HOST-RESOURCES-MIB storage table")
            print(f"  {'':<2}{'IDX':>4}  {'DESCRIPTION':<34}{'SIZE':>11}"
                  f"{'USED':>7}  TYPE")
            print("  " + "-" * 72)
            for row in rows:
                marker = "→" if chosen is not None and row.index == chosen.index else " "
                used = (f"{row.used_percent:.0f}%"
                        if row.used_percent is not None else "-")
                size = format_bytes(row.size_bytes) if row.size_bytes else "-"
                print(f"  {marker:<2}{row.index:>4}  {_clip(row.descr, 33):<34}"
                      f"{size:>11}{used:>7}  {_type_name(row)}")
            print()

        try:
            disks = parse_ucd_disks(session.walk(mibs.UCD_DISK_TABLE))
        except snmp.SnmpError:
            disks = []
        if disks:
            print("UCD-SNMP-MIB dskTable")
            for disk in disks:
                print(f"    {disk['path']:<34}"
                      f"{format_bytes(disk['total_kb'] * 1024):>11}")
            print()

    # Run the real poller so the verdict matches what the dashboard would show.
    sample = poll_camera(camera, thresholds)

    if as_json:
        print(json.dumps(sample.to_dict(), indent=2, default=str))
        return 0 if sample.sd_present else 1

    if sample.sd_present:
        print(f"✓ SD card detected via {sample.sd_source}")
        print(f"    {sample.sd_label}")
        print(f"    {format_bytes(sample.sd_used_bytes)} used of "
              f"{format_bytes(sample.sd_total_bytes)} "
              f"({sample.sd_used_percent:.1f}%)")
        print(f"    status: {sample.severity}"
              + (f" — {'; '.join(sample.issues)}" if sample.issues else ""))
    else:
        print("✗ No SD card detected")
        if rows:
            print("  The camera answered, but no volume looked like a recording card.")
            print("  If one of the rows above IS the card, pin it by index:")
            print(f'      "sd_storage_index": <IDX from the table above>')
            print("  or match it by name:")
            print('      "sd_patterns": ["part of the description"]')
        else:
            print("  The camera exposes no storage table at all. It may need a")
            print("  vendor MIB — see sd_size_oid/sd_used_oid in cameras.example.json.")

    print()
    print("Config entry — paste into the \"cameras\" list in cameras.json:")
    print()
    entry = {
        "id": _suggest_id(info["name"] or host),
        "name": info["name"] or host,
        "host": host,
    }
    if port != 161:
        entry["port"] = port
    if community != "public":
        entry["community"] = community
    if version != snmp.VERSION_V2C:
        entry["version"] = info["version"]
    if not sample.sd_present and rows:
        entry["sd_storage_index"] = "<pick one from the table above>"
    for line in json.dumps(entry, indent=2).splitlines():
        print(f"    {line}")
    print()
    return 0 if sample.sd_present else 1


def _type_name(row) -> str:
    names = {
        mibs.HR_STORAGE_TYPE_RAM: "RAM",
        mibs.HR_STORAGE_TYPE_VIRTUAL_MEMORY: "virtual memory",
        mibs.HR_STORAGE_TYPE_FIXED_DISK: "fixed disk",
        mibs.HR_STORAGE_TYPE_REMOVABLE_DISK: "removable disk",
        mibs.HR_STORAGE_TYPE_FLOPPY: "floppy",
        mibs.HR_STORAGE_TYPE_OTHER: "other",
    }
    return names.get(row.type_oid, "unknown")


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _suggest_id(name: str) -> str:
    slug = "".join(c.lower() if c.isalnum() else "-" for c in name).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return f"cam-{slug[:24]}" if slug else "cam-01"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("host", nargs="?", help="camera IP or hostname")
    parser.add_argument("--scan", metavar="CIDR",
                        help="scan a network for SNMP devices, e.g. 192.168.1.0/24")
    parser.add_argument("--community", "-c",
                        help="SNMP community (default: try 'public' then 'private')")
    parser.add_argument("--port", "-p", type=int, default=161)
    parser.add_argument("--version", choices=["v1", "v2c"],
                        help="force an SNMP version (default: try v2c then v1)")
    parser.add_argument("--timeout", type=float, default=1.5)
    parser.add_argument("--workers", type=int, default=64,
                        help="parallel probes while scanning")
    parser.add_argument("--json", action="store_true",
                        help="print the poll result as JSON")
    args = parser.parse_args()

    if not args.host and not args.scan:
        parser.error("give a host to probe, or --scan CIDR to find cameras")

    communities = [args.community] if args.community else list(COMMON_COMMUNITIES)

    if args.scan:
        found = scan(args.scan, args.port, communities, args.timeout, args.workers)
        print()
        if not found:
            print("No SNMP devices answered. If your cameras are on this network,")
            print("SNMP is probably disabled in their web UI, or uses a different")
            print("community string (try --community <yours>).")
            return 1
        print(f"Found {len(found)} SNMP device{'s' if len(found) != 1 else ''}. "
              f"Probe one in detail with:")
        print(f"    python3 tools/probe.py {found[0]['host']}"
              + (f" --community {found[0]['community']}"
                 if found[0]['community'] != 'public' else ""))
        return 0

    return report(args.host, args.community, args.port, args.version,
                  args.timeout, args.json)


if __name__ == "__main__":
    raise SystemExit(main())
