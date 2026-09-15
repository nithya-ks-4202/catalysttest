"""SNMP v1/v2c client over UDP - stdlib only.

Supports GET, GETNEXT and GETBULK, plus a `walk()` helper that picks GETBULK on
v2c and falls back to GETNEXT on v1. Retries are per-request with a bounded
timeout, because a camera with a dying SD card tends to be slow rather than
silent, and we would rather record "slow" than "offline".

Not supported: SNMPv3. USM's authPriv needs AES/DES, which the standard library
does not ship. v1/v2c covers the read-only community-string setup that IP
cameras are almost always deployed with; see README for the v3 note.
"""

from __future__ import annotations

import os
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

from . import ber
from .ber import BERError, Decoder, SnmpException

VERSION_V1 = 0
VERSION_V2C = 1

VERSION_NAMES = {"1": VERSION_V1, "v1": VERSION_V1, "2c": VERSION_V2C, "v2c": VERSION_V2C}

ERROR_STATUS = {
    0: "noError",
    1: "tooBig",
    2: "noSuchName",
    3: "badValue",
    4: "readOnly",
    5: "genErr",
    6: "noAccess",
    7: "wrongType",
    8: "wrongLength",
    9: "wrongEncoding",
    10: "wrongValue",
    11: "noCreation",
    12: "inconsistentValue",
    13: "resourceUnavailable",
    14: "commitFailed",
    15: "undoFailed",
    16: "authorizationError",
    17: "notWritable",
    18: "inconsistentName",
}

# Some agents cap GETBULK responses well below what we ask for; 10 is a safe
# repetition count that still keeps a storage-table walk to a couple of packets.
DEFAULT_MAX_REPETITIONS = 10


class SnmpError(Exception):
    """Base class for SNMP failures."""


class SnmpTimeout(SnmpError):
    """No usable response arrived within the timeout/retry budget."""


class SnmpProtocolError(SnmpError):
    """The agent answered, but with an error-status or an unparseable packet."""


@dataclass(frozen=True)
class VarBind:
    oid: tuple[int, ...]
    value: Any

    @property
    def oid_str(self) -> str:
        return ber.format_oid(self.oid)

    def as_int(self, default: int | None = None) -> int | None:
        """Best-effort integer view of the value, or `default`."""
        value = self.value
        if isinstance(value, ber.TypedValue):
            return value.value
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, bytes):
            try:
                return int(value.decode("ascii", "ignore").strip())
            except ValueError:
                return default
        return default

    def as_text(self, default: str = "") -> str:
        """Best-effort text view. OCTET STRINGs from cameras are rarely clean
        UTF-8, so we decode leniently and drop control characters."""
        value = self.value
        if isinstance(value, bytes):
            text = value.decode("utf-8", "replace")
            return "".join(c for c in text if c.isprintable() or c in " \t").strip()
        if isinstance(value, SnmpException):
            return default
        if value is None:
            return default
        if isinstance(value, ber.TypedValue):
            return str(value.value)
        if isinstance(value, tuple):
            return ber.format_oid(value)
        return str(value)

    @property
    def is_exception(self) -> bool:
        return isinstance(self.value, SnmpException)


@dataclass
class SnmpConfig:
    host: str
    community: str = "public"
    port: int = 161
    version: int = VERSION_V2C
    timeout: float = 2.0
    retries: int = 1
    max_repetitions: int = DEFAULT_MAX_REPETITIONS


@dataclass
class Session:
    """A short-lived SNMP conversation with one agent.

    Not thread-safe: request IDs and the socket are per-session state. The
    poller creates one session per camera per poll cycle, which also keeps a
    hung camera from holding a socket open between cycles.
    """

    config: SnmpConfig
    _sock: socket.socket | None = field(default=None, init=False, repr=False)
    _request_id: int = field(default=0, init=False, repr=False)
    # Rough latency of the last successful exchange, surfaced on the dashboard.
    last_rtt_ms: float | None = field(default=None, init=False)

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    # -- request plumbing --------------------------------------------------

    def _socket(self) -> socket.socket:
        if self._sock is None:
            # Resolve first so we open the right address family for IPv6 hosts.
            infos = socket.getaddrinfo(
                self.config.host, self.config.port, 0, socket.SOCK_DGRAM
            )
            if not infos:
                raise SnmpError(f"cannot resolve {self.config.host}")
            family, socktype, proto, _canon, sockaddr = infos[0]
            sock = socket.socket(family, socktype, proto)
            sock.settimeout(self.config.timeout)
            sock.connect(sockaddr)
            self._sock = sock
        return self._sock

    def _next_request_id(self) -> int:
        if self._request_id == 0:
            # Start somewhere random so a restarted poller doesn't collide with
            # in-flight responses from the previous run.
            self._request_id = int.from_bytes(os.urandom(2), "big") + 1
        else:
            self._request_id += 1
        # Keep it inside the 31-bit range agents reliably echo back.
        self._request_id &= 0x7FFFFFFF
        return self._request_id

    def _build_message(self, pdu_tag: int, request_id: int, varbinds: Sequence[bytes],
                       field1: int, field2: int) -> bytes:
        """field1/field2 are error-status/error-index, or non-repeaters/
        max-repetitions for GETBULK."""
        pdu = ber.encode_tlv(
            pdu_tag,
            ber.encode_integer(request_id)
            + ber.encode_integer(field1)
            + ber.encode_integer(field2)
            + ber.encode_sequence(*varbinds),
        )
        return ber.encode_sequence(
            ber.encode_integer(self.config.version),
            ber.encode_octet_string(self.config.community),
            pdu,
        )

    def _exchange(self, pdu_tag: int, oids: Sequence[str | tuple[int, ...]],
                  field1: int = 0, field2: int = 0) -> list[VarBind]:
        varbinds = [
            ber.encode_sequence(ber.encode_oid(oid), ber.encode_null()) for oid in oids
        ]
        sock = self._socket()
        attempts = max(1, self.config.retries + 1)
        last_error: Exception | None = None

        for attempt in range(attempts):
            request_id = self._next_request_id()
            packet = self._build_message(pdu_tag, request_id, varbinds, field1, field2)
            started = time.monotonic()
            try:
                sock.send(packet)
            except OSError as exc:
                last_error = SnmpError(f"send failed: {exc}")
                continue

            # A late reply to a previous attempt can be sitting in the buffer;
            # keep reading until the request-id matches or the budget runs out.
            deadline = started + self.config.timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    last_error = SnmpTimeout(
                        f"no response from {self.config.host}:{self.config.port}"
                    )
                    break
                sock.settimeout(remaining)
                try:
                    data = sock.recv(65535)
                except socket.timeout:
                    last_error = SnmpTimeout(
                        f"no response from {self.config.host}:{self.config.port}"
                    )
                    break
                except OSError as exc:
                    # ICMP port-unreachable surfaces here on connected UDP
                    # sockets: the agent is not listening at all.
                    last_error = SnmpError(f"{self.config.host}: {exc}")
                    break

                try:
                    resp_id, error_status, error_index, binds = _parse_response(data)
                except BERError as exc:
                    last_error = SnmpProtocolError(f"malformed response: {exc}")
                    break
                if resp_id != request_id:
                    continue  # stale datagram - keep waiting

                self.last_rtt_ms = (time.monotonic() - started) * 1000.0
                if error_status:
                    name = ERROR_STATUS.get(error_status, f"error {error_status}")
                    if error_status == 2 and self.config.version == VERSION_V1:
                        # v1 signals "end of MIB" as noSuchName; the walk loop
                        # treats an empty result as the end.
                        return []
                    raise SnmpProtocolError(
                        f"{self.config.host} returned {name} at index {error_index}"
                    )
                return binds

        assert last_error is not None
        raise last_error

    # -- public operations -------------------------------------------------

    def get(self, *oids: str | tuple[int, ...]) -> list[VarBind]:
        return self._exchange(ber.TAG_GET_REQUEST, oids)

    def get_one(self, oid: str | tuple[int, ...]) -> VarBind | None:
        binds = self.get(oid)
        if not binds or binds[0].is_exception:
            return None
        return binds[0]

    def get_next(self, *oids: str | tuple[int, ...]) -> list[VarBind]:
        return self._exchange(ber.TAG_GET_NEXT_REQUEST, oids)

    def get_bulk(self, oid: str | tuple[int, ...], max_repetitions: int | None = None,
                 non_repeaters: int = 0) -> list[VarBind]:
        reps = max_repetitions or self.config.max_repetitions
        return self._exchange(
            ber.TAG_GET_BULK_REQUEST, [oid], field1=non_repeaters, field2=reps
        )

    def walk(self, root: str | tuple[int, ...], limit: int = 512) -> Iterator[VarBind]:
        """Yield every varbind under `root`, in OID order.

        Uses GETBULK on v2c and GETNEXT on v1. `limit` bounds the number of rows
        so a broken agent that never leaves the subtree cannot hang a poll.
        """
        root_arcs = ber.parse_oid(root) if isinstance(root, str) else tuple(root)
        current: tuple[int, ...] = root_arcs
        seen = 0

        while seen < limit:
            if self.config.version == VERSION_V2C:
                binds = self.get_bulk(current)
            else:
                binds = self.get_next(current)
            if not binds:
                return

            progressed = False
            for bind in binds:
                if bind.oid[: len(root_arcs)] != root_arcs:
                    return  # walked off the end of the subtree
                if isinstance(bind.value, SnmpException):
                    if bind.value.name == "endOfMibView":
                        return
                    continue
                if bind.oid <= current and progressed:
                    return  # agent stopped advancing - bail rather than loop
                current = bind.oid
                progressed = True
                seen += 1
                yield bind
                if seen >= limit:
                    return

            if not progressed:
                return


def _parse_response(data: bytes) -> tuple[int, int, int, list[VarBind]]:
    """Decode an SNMP response message into (request_id, err, err_index, binds)."""
    outer, _ = Decoder(data).expect_sequence()
    outer.read_integer()  # version
    outer.expect(ber.TAG_OCTET_STRING)  # community
    pdu, tag = outer.expect_sequence()
    if tag != ber.TAG_GET_RESPONSE:
        raise BERError(f"expected a response PDU, got tag 0x{tag:02x}")

    request_id = pdu.read_integer()
    error_status = pdu.read_integer()
    error_index = pdu.read_integer()
    varbind_list, _ = pdu.expect_sequence()

    binds: list[VarBind] = []
    while varbind_list.remaining > 0:
        item, _ = varbind_list.expect_sequence()
        oid = item.read_oid()
        value = item.read_value()
        binds.append(VarBind(oid, value))
    return request_id, error_status, error_index, binds


def parse_version(text: str | int) -> int:
    """Map a config value like "2c" or 1 onto a protocol version constant."""
    if isinstance(text, int):
        if text in (VERSION_V1, VERSION_V2C):
            return text
        raise ValueError(f"unsupported SNMP version: {text}")
    key = str(text).strip().lower()
    if key not in VERSION_NAMES:
        raise ValueError(
            f"unsupported SNMP version {text!r} (use 'v1' or 'v2c'; v3 is not supported)"
        )
    return VERSION_NAMES[key]
