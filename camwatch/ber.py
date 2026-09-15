"""Minimal ASN.1 BER encoder/decoder - just the subset SNMP v1/v2c needs.

SNMP is BER all the way down, so rather than pull in a dependency we implement
the handful of tags that actually appear on the wire. Everything here is
stdlib-only and deliberately strict: malformed input raises BERError rather
than guessing, because a silently mis-parsed varbind turns into a wrong number
on someone's dashboard.
"""

from __future__ import annotations

from typing import Any, NamedTuple

# --- Universal tags -------------------------------------------------------
TAG_INTEGER = 0x02
TAG_OCTET_STRING = 0x04
TAG_NULL = 0x05
TAG_OID = 0x06
TAG_SEQUENCE = 0x30

# --- SNMP application tags ------------------------------------------------
TAG_IP_ADDRESS = 0x40
TAG_COUNTER32 = 0x41
TAG_GAUGE32 = 0x42
TAG_TIMETICKS = 0x43
TAG_OPAQUE = 0x44
TAG_COUNTER64 = 0x46

# --- SNMPv2 context-specific "exception" tags -----------------------------
# These arrive in place of a value when an OID has nothing behind it.
TAG_NO_SUCH_OBJECT = 0x80
TAG_NO_SUCH_INSTANCE = 0x81
TAG_END_OF_MIB_VIEW = 0x82

# --- PDU tags -------------------------------------------------------------
TAG_GET_REQUEST = 0xA0
TAG_GET_NEXT_REQUEST = 0xA1
TAG_GET_RESPONSE = 0xA2
TAG_SET_REQUEST = 0xA3
TAG_GET_BULK_REQUEST = 0xA5

UNSIGNED_TAGS = frozenset({TAG_COUNTER32, TAG_GAUGE32, TAG_TIMETICKS, TAG_COUNTER64})
EXCEPTION_TAGS = frozenset(
    {TAG_NO_SUCH_OBJECT, TAG_NO_SUCH_INSTANCE, TAG_END_OF_MIB_VIEW}
)

EXCEPTION_NAMES = {
    TAG_NO_SUCH_OBJECT: "noSuchObject",
    TAG_NO_SUCH_INSTANCE: "noSuchInstance",
    TAG_END_OF_MIB_VIEW: "endOfMibView",
}


class BERError(ValueError):
    """Raised when a buffer is not well-formed BER (or not the BER we expect)."""


class SnmpException(NamedTuple):
    """An SNMPv2 value-position exception, e.g. noSuchInstance."""

    name: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.name


# =========================================================================
# Encoding
# =========================================================================


def encode_length(length: int) -> bytes:
    """Encode a definite-form BER length."""
    if length < 0:
        raise BERError(f"negative length: {length}")
    if length < 0x80:
        return bytes([length])
    body = length.to_bytes((length.bit_length() + 7) // 8, "big")
    if len(body) > 0x7E:
        raise BERError("length too large to encode")
    return bytes([0x80 | len(body)]) + body


def encode_tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + encode_length(len(value)) + value


def encode_integer(value: int, tag: int = TAG_INTEGER) -> bytes:
    """Encode a signed two's-complement INTEGER (minimal number of octets)."""
    if value == 0:
        body = b"\x00"
    else:
        # Room for the sign bit: BER integers are signed, so 0x80 needs a
        # leading zero byte or it would decode as -128.
        nbytes = (value.bit_length() + 8) // 8
        while True:
            try:
                body = value.to_bytes(nbytes, "big", signed=True)
                break
            except OverflowError:  # pragma: no cover - defensive
                nbytes += 1
        # Strip redundant leading bytes, keeping the sign intact.
        while (
            len(body) > 1
            and (
                (body[0] == 0x00 and not body[1] & 0x80)
                or (body[0] == 0xFF and body[1] & 0x80)
            )
        ):
            body = body[1:]
    return encode_tlv(tag, body)


def encode_unsigned(value: int, tag: int) -> bytes:
    """Encode Counter32/Gauge32/TimeTicks/Counter64 (unsigned, but BER-signed).

    Unsigned SNMP types are still encoded as two's-complement integers, so a
    value with the high bit set gains a leading zero octet.
    """
    if value < 0:
        raise BERError(f"unsigned value must be >= 0: {value}")
    body = value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
    if body[0] & 0x80:
        body = b"\x00" + body
    while len(body) > 1 and body[0] == 0x00 and not body[1] & 0x80:
        body = body[1:]
    return encode_tlv(tag, body)


def encode_octet_string(value: bytes | str) -> bytes:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return encode_tlv(TAG_OCTET_STRING, value)


def encode_null() -> bytes:
    return b"\x05\x00"


def encode_oid(oid: str | tuple[int, ...]) -> bytes:
    """Encode an OID given as a dotted string or a tuple of arcs."""
    arcs = parse_oid(oid) if isinstance(oid, str) else tuple(oid)
    if len(arcs) < 2:
        raise BERError("OID needs at least two arcs")
    if arcs[0] > 2 or (arcs[0] < 2 and arcs[1] >= 40):
        raise BERError(f"invalid leading OID arcs: {arcs[:2]}")
    body = bytearray()
    body += _encode_base128(arcs[0] * 40 + arcs[1])
    for arc in arcs[2:]:
        if arc < 0:
            raise BERError("negative OID arc")
        body += _encode_base128(arc)
    return encode_tlv(TAG_OID, bytes(body))


def _encode_base128(value: int) -> bytes:
    if value == 0:
        return b"\x00"
    out = bytearray()
    while value:
        out.insert(0, (value & 0x7F) | 0x80)
        value >>= 7
    out[-1] &= 0x7F
    return bytes(out)


def encode_sequence(*parts: bytes) -> bytes:
    return encode_tlv(TAG_SEQUENCE, b"".join(parts))


def parse_oid(oid: str) -> tuple[int, ...]:
    """Parse a dotted OID string into arcs. Leading dots are tolerated."""
    text = oid.strip().lstrip(".")
    if not text:
        raise BERError("empty OID")
    try:
        return tuple(int(part) for part in text.split("."))
    except ValueError as exc:
        raise BERError(f"bad OID {oid!r}") from exc


def format_oid(arcs: tuple[int, ...]) -> str:
    return ".".join(str(a) for a in arcs)


def encode_value(value: Any) -> bytes:
    """Encode a Python value using the natural SNMP type.

    Used by the simulator; the client only ever sends NULLs.
    """
    if value is None:
        return encode_null()
    if isinstance(value, TypedValue):
        return value.encode()
    if isinstance(value, bool):  # bool before int - bool is an int subclass
        return encode_integer(int(value))
    if isinstance(value, int):
        return encode_integer(value)
    if isinstance(value, (bytes, str)):
        return encode_octet_string(value)
    if isinstance(value, tuple):
        return encode_oid(value)
    raise BERError(f"cannot encode {type(value).__name__}")


class TypedValue:
    """Wrap a Python int so it encodes as a specific SNMP application type."""

    __slots__ = ("tag", "value")

    def __init__(self, tag: int, value: int) -> None:
        self.tag = tag
        self.value = value

    def encode(self) -> bytes:
        if self.tag in UNSIGNED_TAGS:
            return encode_unsigned(self.value, self.tag)
        return encode_integer(self.value, self.tag)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"TypedValue(0x{self.tag:02x}, {self.value})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, TypedValue):
            return self.tag == other.tag and self.value == other.value
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.tag, self.value))


def Counter32(v: int) -> TypedValue:  # noqa: N802 - MIB-style names read better
    return TypedValue(TAG_COUNTER32, v)


def Gauge32(v: int) -> TypedValue:  # noqa: N802
    return TypedValue(TAG_GAUGE32, v)


def TimeTicks(v: int) -> TypedValue:  # noqa: N802
    return TypedValue(TAG_TIMETICKS, v)


def Counter64(v: int) -> TypedValue:  # noqa: N802
    return TypedValue(TAG_COUNTER64, v)


# =========================================================================
# Decoding
# =========================================================================


class Decoder:
    """A cursor over a BER buffer."""

    def __init__(self, data: bytes, pos: int = 0, end: int | None = None) -> None:
        self.data = data
        self.pos = pos
        self.end = len(data) if end is None else end

    @property
    def remaining(self) -> int:
        return self.end - self.pos

    def read_byte(self) -> int:
        if self.pos >= self.end:
            raise BERError("truncated: expected a byte")
        byte = self.data[self.pos]
        self.pos += 1
        return byte

    def read_tag(self) -> int:
        tag = self.read_byte()
        if tag & 0x1F == 0x1F:
            raise BERError("multi-byte tags are not supported")
        return tag

    def read_length(self) -> int:
        first = self.read_byte()
        if first == 0x80:
            raise BERError("indefinite lengths are not valid in SNMP")
        if first < 0x80:
            return first
        count = first & 0x7F
        if count > self.remaining:
            raise BERError("truncated length")
        length = int.from_bytes(self.data[self.pos : self.pos + count], "big")
        self.pos += count
        return length

    def read_bytes(self, count: int) -> bytes:
        if count > self.remaining:
            raise BERError(
                f"truncated: wanted {count} bytes, {self.remaining} remain"
            )
        chunk = self.data[self.pos : self.pos + count]
        self.pos += count
        return chunk

    def read_tlv(self) -> tuple[int, bytes]:
        tag = self.read_tag()
        length = self.read_length()
        return tag, self.read_bytes(length)

    def expect(self, tag: int) -> bytes:
        got, body = self.read_tlv()
        if got != tag:
            raise BERError(f"expected tag 0x{tag:02x}, got 0x{got:02x}")
        return body

    def expect_sequence(self) -> tuple["Decoder", int]:
        """Read a constructed value; return a decoder scoped to its body + its tag."""
        tag = self.read_tag()
        if not tag & 0x20:
            raise BERError(f"expected a constructed tag, got 0x{tag:02x}")
        length = self.read_length()
        if length > self.remaining:
            raise BERError("truncated constructed value")
        sub = Decoder(self.data, self.pos, self.pos + length)
        self.pos += length
        return sub, tag

    def read_integer(self) -> int:
        return decode_integer(self.expect(TAG_INTEGER))

    def read_oid(self) -> tuple[int, ...]:
        return decode_oid(self.expect(TAG_OID))

    def read_value(self) -> Any:
        """Read any value that can appear in a varbind's value position."""
        tag = self.read_tag()
        length = self.read_length()
        body = self.read_bytes(length)
        return decode_value(tag, body)


def decode_integer(body: bytes) -> int:
    if not body:
        raise BERError("empty INTEGER")
    return int.from_bytes(body, "big", signed=True)


def decode_unsigned(body: bytes) -> int:
    if not body:
        raise BERError("empty unsigned value")
    # Counter/Gauge bodies are unsigned; some agents still set the high bit
    # without a pad byte, so mask back into range rather than going negative.
    return int.from_bytes(body, "big", signed=False)


def decode_oid(body: bytes) -> tuple[int, ...]:
    if not body:
        raise BERError("empty OID")

    # Every subidentifier is base-128, including the first - which packs two
    # arcs and can span several bytes once the second arc exceeds 39
    # (e.g. 2.100.3 encodes its leading 180 as 0x81 0x34).
    subids: list[int] = []
    value = 0
    pending = False
    for byte in body:
        value = (value << 7) | (byte & 0x7F)
        pending = True
        if not byte & 0x80:
            subids.append(value)
            value = 0
            pending = False
    if pending:
        raise BERError("OID ends mid-arc")

    first = subids[0]
    if first < 40:
        arcs = [0, first]
    elif first < 80:
        arcs = [1, first - 40]
    else:
        arcs = [2, first - 80]
    arcs.extend(subids[1:])
    return tuple(arcs)


def decode_value(tag: int, body: bytes) -> Any:
    if tag == TAG_INTEGER:
        return decode_integer(body)
    if tag == TAG_OCTET_STRING:
        return body
    if tag == TAG_NULL:
        return None
    if tag == TAG_OID:
        return decode_oid(body)
    if tag in UNSIGNED_TAGS:
        return TypedValue(tag, decode_unsigned(body))
    if tag == TAG_IP_ADDRESS:
        if len(body) != 4:
            raise BERError("IpAddress must be 4 octets")
        return ".".join(str(b) for b in body)
    if tag == TAG_OPAQUE:
        return body
    if tag in EXCEPTION_TAGS:
        return SnmpException(EXCEPTION_NAMES[tag])
    raise BERError(f"unsupported value tag 0x{tag:02x}")
