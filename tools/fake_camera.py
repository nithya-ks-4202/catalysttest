#!/usr/bin/env python3
"""A fake SNMP agent that impersonates a fleet of IP cameras.

Lets you run the whole dashboard without any real hardware, and gives the tests
something deterministic to poll. Each simulated camera gets its own UDP port and
its own personality: healthy, filling up, nearly full, card missing, card
read-only, or flat-out offline.

    python3 tools/fake_camera.py --count 8 --base-port 11610

Then point cameras.json at 127.0.0.1 on those ports (--write-config does it).
"""

from __future__ import annotations

import argparse
import json
import random
import socket
import selectors
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from camwatch import ber, mibs  # noqa: E402
from camwatch.ber import Counter32, Gauge32, TimeTicks  # noqa: E402

GIB = 1024 ** 3


@dataclass
class Personality:
    """How a simulated camera behaves."""

    name: str
    sd_present: bool = True
    sd_capacity_gb: int = 64
    start_used_fraction: float = 0.35
    fill_rate_per_min: float = 0.001  # fraction of the card filled per minute
    read_only: bool = False
    write_errors: int = 0
    reachable: bool = True
    uptime_seconds: int = 86_400 * 12
    model: str = "Catalyst C500 Dome Camera"


PERSONALITIES = [
    Personality("Lobby North", start_used_fraction=0.22, fill_rate_per_min=0.0008),
    Personality("Loading Dock", start_used_fraction=0.78, fill_rate_per_min=0.004),
    Personality("Car Park Level 2", start_used_fraction=0.93, fill_rate_per_min=0.006),
    Personality("Server Room", sd_present=False, model="Catalyst C300 Bullet Camera"),
    Personality("Rear Entrance", start_used_fraction=0.61, read_only=True,
                write_errors=418, model="Catalyst C300 Bullet Camera"),
    Personality("Roof East", reachable=False),
    Personality("Warehouse Aisle 4", start_used_fraction=0.44,
                sd_capacity_gb=128, fill_rate_per_min=0.0015),
    Personality("Reception", start_used_fraction=0.12, uptime_seconds=140,
                model="Catalyst C500 Dome Camera"),
]


@dataclass
class FakeCamera:
    """One simulated agent: an OID -> value map that moves over time."""

    personality: Personality
    port: int
    community: str = "public"
    started: float = field(default_factory=time.monotonic)
    sock: socket.socket | None = None

    # --- the moving parts ------------------------------------------------

    def _used_fraction(self) -> float:
        elapsed_min = (time.monotonic() - self.started) / 60.0
        p = self.personality
        if p.read_only:
            # A read-only card is frozen - that is exactly the symptom.
            return p.start_used_fraction
        fraction = p.start_used_fraction + elapsed_min * p.fill_rate_per_min
        # Cameras wrap around when the card fills; clamp just below full so the
        # dashboard shows a card pinned at ~100% rather than an impossible 140%.
        return min(fraction, 0.995)

    def _uptime_ticks(self) -> int:
        elapsed = time.monotonic() - self.started
        return int((self.personality.uptime_seconds + elapsed) * 100)

    def oid_map(self) -> dict[tuple[int, ...], Any]:
        """Build the full OID -> value map for this instant."""
        p = self.personality
        oids: dict[tuple[int, ...], Any] = {}

        def put(oid: str, value: Any) -> None:
            oids[ber.parse_oid(oid)] = value

        put(mibs.SYS_DESCR, f"{p.model}; firmware 4.2.1; Linux 5.10")
        put(mibs.SYS_OBJECT_ID, (1, 3, 6, 1, 4, 1, 99999, 1, 1))
        put(mibs.SYS_UPTIME, TimeTicks(self._uptime_ticks()))
        put(mibs.SYS_CONTACT, "facilities@example.com")
        put(mibs.SYS_NAME, p.name)
        put(mibs.SYS_LOCATION, p.name)

        # hrStorageTable: row 1 is always RAM (so the poller has to skip it),
        # row 2 the OS partition, row 3 the SD card when one is inserted.
        block = 4096
        rows: list[dict[str, Any]] = [
            {
                "index": 1,
                "type": mibs.HR_STORAGE_TYPE_RAM,
                "descr": "Physical memory",
                "units": 1024,
                "size": 512 * 1024,
                "used": 311 * 1024,
                "failures": 0,
            },
            {
                "index": 2,
                "type": mibs.HR_STORAGE_TYPE_FIXED_DISK,
                "descr": "/ (root filesystem)",
                "units": block,
                "size": (256 * 1024 * 1024) // block,
                "used": (198 * 1024 * 1024) // block,
                "failures": 0,
            },
        ]
        if p.sd_present:
            total_blocks = (p.sd_capacity_gb * GIB) // block
            rows.append(
                {
                    "index": 3,
                    "type": mibs.HR_STORAGE_TYPE_REMOVABLE_DISK,
                    "descr": "/mnt/sdcard (SD Card)",
                    "units": block,
                    "size": total_blocks,
                    "used": int(total_blocks * self._used_fraction()),
                    "failures": p.write_errors,
                }
            )

        for row in rows:
            i = row["index"]
            put(f"{mibs.HR_STORAGE_INDEX}.{i}", i)
            oids[ber.parse_oid(f"{mibs.HR_STORAGE_TYPE}.{i}")] = row["type"]
            put(f"{mibs.HR_STORAGE_DESCR}.{i}", row["descr"])
            put(f"{mibs.HR_STORAGE_ALLOCATION_UNITS}.{i}", row["units"])
            put(f"{mibs.HR_STORAGE_SIZE}.{i}", row["size"])
            put(f"{mibs.HR_STORAGE_USED}.{i}", row["used"])
            oids[ber.parse_oid(f"{mibs.HR_STORAGE_ALLOCATION_FAILURES}.{i}")] = (
                Counter32(row["failures"])
            )

        # A small vendor subtree, so the custom-OID path has something to hit.
        put("1.3.6.1.4.1.99999.1.2.1.0", 1 if p.sd_present else 0)  # card present
        put("1.3.6.1.4.1.99999.1.2.2.0", "readonly" if p.read_only
            else ("ok" if p.sd_present else "absent"))
        oids[ber.parse_oid("1.3.6.1.4.1.99999.1.2.3.0")] = Gauge32(
            max(0, 100 - int(self._used_fraction() * 40))
        )  # pretend wear-levelling health, 0-100
        oids[ber.parse_oid("1.3.6.1.4.1.99999.1.2.4.0")] = Counter32(p.write_errors)
        put("1.3.6.1.4.1.99999.1.3.1.0", 1 if p.sd_present and not p.read_only else 0)
        return oids

    # --- protocol --------------------------------------------------------

    def handle(self, data: bytes) -> bytes | None:
        try:
            request = _parse_request(data)
        except ber.BERError:
            return None
        if request["community"] != self.community.encode():
            return None  # wrong community: a real agent just stays silent

        oids = self.oid_map()
        ordered = sorted(oids)
        results: list[tuple[tuple[int, ...], Any]] = []

        if request["tag"] == ber.TAG_GET_REQUEST:
            for oid in request["oids"]:
                if oid in oids:
                    results.append((oid, oids[oid]))
                else:
                    results.append((oid, ber.SnmpException("noSuchInstance")))
        elif request["tag"] == ber.TAG_GET_NEXT_REQUEST:
            for oid in request["oids"]:
                results.append(_next_after(ordered, oids, oid))
        elif request["tag"] == ber.TAG_GET_BULK_REQUEST:
            reps = max(1, request["field2"])
            for oid in request["oids"]:
                cursor = oid
                for _ in range(reps):
                    nxt = _next_after(ordered, oids, cursor)
                    results.append(nxt)
                    if isinstance(nxt[1], ber.SnmpException):
                        break
                    cursor = nxt[0]
        else:
            return None

        return _build_response(
            request["version"], request["community"], request["request_id"], results
        )


def _next_after(ordered: list[tuple[int, ...]], oids: dict, oid: tuple[int, ...]):
    for candidate in ordered:
        if candidate > oid:
            return candidate, oids[candidate]
    return oid, ber.SnmpException("endOfMibView")


def _parse_request(data: bytes) -> dict[str, Any]:
    outer, _ = ber.Decoder(data).expect_sequence()
    version = outer.read_integer()
    community = outer.expect(ber.TAG_OCTET_STRING)
    pdu, tag = outer.expect_sequence()
    request_id = pdu.read_integer()
    field1 = pdu.read_integer()
    field2 = pdu.read_integer()
    varbinds, _ = pdu.expect_sequence()
    oids = []
    while varbinds.remaining > 0:
        item, _ = varbinds.expect_sequence()
        oids.append(item.read_oid())
        item.read_value()
    return {
        "version": version,
        "community": community,
        "tag": tag,
        "request_id": request_id,
        "field1": field1,
        "field2": field2,
        "oids": oids,
    }


def _build_response(version: int, community: bytes, request_id: int,
                    results: list[tuple[tuple[int, ...], Any]]) -> bytes:
    binds = []
    for oid, value in results:
        if isinstance(value, ber.SnmpException):
            tag = {
                "noSuchObject": ber.TAG_NO_SUCH_OBJECT,
                "noSuchInstance": ber.TAG_NO_SUCH_INSTANCE,
                "endOfMibView": ber.TAG_END_OF_MIB_VIEW,
            }[value.name]
            encoded = ber.encode_tlv(tag, b"")
        else:
            encoded = ber.encode_value(value)
        binds.append(ber.encode_sequence(ber.encode_oid(oid), encoded))

    pdu = ber.encode_tlv(
        ber.TAG_GET_RESPONSE,
        ber.encode_integer(request_id)
        + ber.encode_integer(0)
        + ber.encode_integer(0)
        + ber.encode_sequence(*binds),
    )
    return ber.encode_sequence(
        ber.encode_integer(version), ber.encode_octet_string(community), pdu
    )


class Fleet:
    """Runs every simulated camera on one selector loop."""

    def __init__(self, cameras: list[FakeCamera], host: str = "127.0.0.1",
                 drop_rate: float = 0.0) -> None:
        self.cameras = cameras
        self.host = host
        self.drop_rate = drop_rate
        self.selector = selectors.DefaultSelector()

    def start(self) -> None:
        for camera in self.cameras:
            if not camera.personality.reachable:
                continue  # an unreachable camera simply has nothing listening
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((self.host, camera.port))
            sock.setblocking(False)
            camera.sock = sock
            self.selector.register(sock, selectors.EVENT_READ, camera)

    def serve_forever(self) -> None:
        while True:
            self.poll(timeout=1.0)

    def poll(self, timeout: float = 0.1) -> None:
        for key, _ in self.selector.select(timeout=timeout):
            camera: FakeCamera = key.data
            try:
                data, addr = key.fileobj.recvfrom(65535)
            except BlockingIOError:  # pragma: no cover - selector said ready
                continue
            if self.drop_rate and random.random() < self.drop_rate:
                continue  # simulate packet loss
            reply = camera.handle(data)
            if reply is not None:
                key.fileobj.sendto(reply, addr)

    def close(self) -> None:
        for camera in self.cameras:
            if camera.sock is not None:
                self.selector.unregister(camera.sock)
                camera.sock.close()
                camera.sock = None
        self.selector.close()


def build_fleet(count: int, base_port: int, community: str = "public",
                seed: int = 1) -> list[FakeCamera]:
    """Build `count` simulated cameras.

    Deterministic for a given seed: demo.sh writes the config in one process
    and serves the fleet from another, and the two must agree on every port
    and name.
    """
    rng = random.Random(seed)
    cameras = []
    for i in range(count):
        personality = PERSONALITIES[i % len(PERSONALITIES)]
        if i >= len(PERSONALITIES):
            # Past the scripted set, vary the copies so the fleet isn't clones.
            personality = Personality(
                name=f"{personality.name} ({i // len(PERSONALITIES) + 1})",
                sd_present=personality.sd_present,
                sd_capacity_gb=personality.sd_capacity_gb,
                start_used_fraction=min(0.97, personality.start_used_fraction
                                        + rng.uniform(-0.1, 0.15)),
                fill_rate_per_min=personality.fill_rate_per_min,
                read_only=personality.read_only,
                write_errors=personality.write_errors,
                reachable=personality.reachable,
                uptime_seconds=personality.uptime_seconds,
                model=personality.model,
            )
        cameras.append(FakeCamera(personality, base_port + i, community))
    return cameras


def write_config(cameras: list[FakeCamera], path: Path, host: str,
                 community: str) -> None:
    config = {
        "poll_interval_seconds": 15,
        "thresholds": {"capacity_warning_percent": 75, "capacity_critical_percent": 90},
        "defaults": {
            "community": community,
            "version": "v2c",
            "site": "Simulated site",
            # The simulator implements a small private MIB, so the demo config
            # exercises the vendor-OID path (read-only detection, wear health)
            # alongside the generic hrStorage numbers.
            "sd_status_oid": "1.3.6.1.4.1.99999.1.2.2.0",
            "sd_health_oid": "1.3.6.1.4.1.99999.1.2.3.0",
            "sd_write_errors_oid": "1.3.6.1.4.1.99999.1.2.4.0",
        },
        "cameras": [
            {
                "id": f"cam-{i + 1:02d}",
                "name": c.personality.name,
                "host": host,
                "port": c.port,
            }
            for i, c in enumerate(cameras)
        ],
    }
    path.write_text(json.dumps(config, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--count", type=int, default=len(PERSONALITIES),
                        help="number of simulated cameras (default: %(default)s)")
    parser.add_argument("--base-port", type=int, default=11610,
                        help="first UDP port to bind (default: %(default)s)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--community", default="public")
    parser.add_argument("--drop-rate", type=float, default=0.0,
                        help="fraction of requests to silently drop, 0.0-1.0")
    parser.add_argument("--write-config", type=Path, metavar="PATH",
                        help="write a matching cameras.json and exit-ready config")
    parser.add_argument("--write-config-only", action="store_true",
                        help="write the config and exit without binding any ports")
    parser.add_argument("--seed", type=int, default=1,
                        help="RNG seed for generated personalities, so a config "
                             "written in one run matches the fleet served by another")
    args = parser.parse_args()

    cameras = build_fleet(args.count, args.base_port, args.community, args.seed)
    if args.write_config:
        write_config(cameras, args.write_config, args.host, args.community)
        print(f"wrote {args.write_config}")
    if args.write_config_only:
        if not args.write_config:
            parser.error("--write-config-only requires --write-config PATH")
        return 0

    fleet = Fleet(cameras, args.host, args.drop_rate)
    fleet.start()
    listening = [c for c in cameras if c.sock is not None]
    print(f"Simulating {len(cameras)} cameras "
          f"({len(listening)} listening, {len(cameras) - len(listening)} offline)")
    for camera in cameras:
        state = f"udp/{camera.port}" if camera.sock else "offline (nothing listening)"
        print(f"  {camera.personality.name:<24} {state}")
    print("Ctrl-C to stop.")
    try:
        fleet.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        fleet.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
