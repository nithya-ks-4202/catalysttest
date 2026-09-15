"""BER encoding/decoding round-trips and the edge cases that bite in practice."""

import unittest

from camwatch import ber


class TestLength(unittest.TestCase):
    def test_short_form(self):
        self.assertEqual(ber.encode_length(0), b"\x00")
        self.assertEqual(ber.encode_length(127), b"\x7f")

    def test_long_form(self):
        self.assertEqual(ber.encode_length(128), b"\x81\x80")
        self.assertEqual(ber.encode_length(256), b"\x82\x01\x00")

    def test_round_trip(self):
        for length in (0, 1, 127, 128, 255, 256, 65535, 70000):
            encoded = ber.encode_length(length)
            decoder = ber.Decoder(encoded)
            self.assertEqual(decoder.read_length(), length)

    def test_negative_rejected(self):
        with self.assertRaises(ber.BERError):
            ber.encode_length(-1)


class TestInteger(unittest.TestCase):
    def test_round_trip(self):
        for value in (0, 1, -1, 127, 128, -128, -129, 255, 256, 32767, -32768,
                      2147483647, -2147483648):
            encoded = ber.encode_integer(value)
            decoded = ber.Decoder(encoded).read_integer()
            self.assertEqual(decoded, value, f"failed for {value}")

    def test_minimal_encoding(self):
        # 0x80 must gain a leading zero or it decodes as -128.
        self.assertEqual(ber.encode_integer(128), b"\x02\x02\x00\x80")
        self.assertEqual(ber.encode_integer(127), b"\x02\x01\x7f")
        self.assertEqual(ber.encode_integer(-128), b"\x02\x01\x80")

    def test_empty_body_rejected(self):
        with self.assertRaises(ber.BERError):
            ber.decode_integer(b"")


class TestUnsigned(unittest.TestCase):
    def test_round_trip(self):
        for value in (0, 1, 255, 256, 2 ** 31, 2 ** 32 - 1, 2 ** 63):
            encoded = ber.encode_unsigned(value, ber.TAG_GAUGE32)
            tag, body = ber.Decoder(encoded).read_tlv()
            self.assertEqual(tag, ber.TAG_GAUGE32)
            self.assertEqual(ber.decode_unsigned(body), value)

    def test_high_bit_does_not_go_negative(self):
        """A Counter32 above 2^31 must not decode as a negative number."""
        value = 3_000_000_000
        encoded = ber.encode_unsigned(value, ber.TAG_COUNTER32)
        decoded = ber.Decoder(encoded).read_value()
        self.assertIsInstance(decoded, ber.TypedValue)
        self.assertEqual(decoded.value, value)

    def test_negative_rejected(self):
        with self.assertRaises(ber.BERError):
            ber.encode_unsigned(-1, ber.TAG_COUNTER32)


class TestOID(unittest.TestCase):
    def test_round_trip(self):
        oids = [
            "1.3.6.1.2.1.1.5.0",
            "1.3.6.1.2.1.25.2.3.1.6.3",
            "1.3.6.1.4.1.99999.1.2.3.0",
            "0.0",
            "2.100.3",
        ]
        for oid in oids:
            encoded = ber.encode_oid(oid)
            decoded = ber.Decoder(encoded).read_oid()
            self.assertEqual(ber.format_oid(decoded), oid)

    def test_large_arc_uses_multibyte(self):
        """Arcs above 127 need base-128 continuation bytes."""
        decoded = ber.Decoder(ber.encode_oid("1.3.6.1.4.1.999999999")).read_oid()
        self.assertEqual(decoded[-1], 999999999)

    def test_first_two_arcs_pack_into_one_byte(self):
        self.assertEqual(ber.encode_oid("1.3")[2], 43)  # 1*40 + 3

    def test_invalid_rejected(self):
        for bad in ("", "1", "3.1.1", "1.40.1", "a.b.c"):
            with self.assertRaises(ber.BERError, msg=f"{bad!r} should be rejected"):
                ber.encode_oid(bad)

    def test_truncated_arc_rejected(self):
        # Trailing byte with the continuation bit set but nothing following.
        with self.assertRaises(ber.BERError):
            ber.decode_oid(b"\x2b\x81")


class TestValues(unittest.TestCase):
    def test_octet_string(self):
        encoded = ber.encode_octet_string("Lobby North")
        self.assertEqual(ber.Decoder(encoded).read_value(), b"Lobby North")

    def test_null(self):
        self.assertIsNone(ber.Decoder(ber.encode_null()).read_value())

    def test_timeticks(self):
        value = ber.Decoder(ber.encode_value(ber.TimeTicks(123456))).read_value()
        self.assertEqual(value.tag, ber.TAG_TIMETICKS)
        self.assertEqual(value.value, 123456)

    def test_exception_tags(self):
        for tag, name in ber.EXCEPTION_NAMES.items():
            decoded = ber.Decoder(ber.encode_tlv(tag, b"")).read_value()
            self.assertIsInstance(decoded, ber.SnmpException)
            self.assertEqual(decoded.name, name)

    def test_ip_address(self):
        encoded = ber.encode_tlv(ber.TAG_IP_ADDRESS, bytes([192, 168, 1, 40]))
        self.assertEqual(ber.Decoder(encoded).read_value(), "192.168.1.40")

    def test_unsupported_tag_rejected(self):
        with self.assertRaises(ber.BERError):
            ber.Decoder(ber.encode_tlv(0x7B, b"\x01")).read_value()


class TestDecoderSafety(unittest.TestCase):
    """A hostile or broken agent must produce an error, never a wrong value."""

    def test_truncated_buffer(self):
        with self.assertRaises(ber.BERError):
            ber.Decoder(b"\x02\x08\x01\x02").read_value()

    def test_indefinite_length_rejected(self):
        decoder = ber.Decoder(b"\x30\x80\x00\x00")
        self.assertEqual(decoder.read_tag(), ber.TAG_SEQUENCE)
        with self.assertRaises(ber.BERError):
            decoder.read_length()

    def test_multibyte_tag_rejected(self):
        with self.assertRaises(ber.BERError):
            ber.Decoder(b"\x1f\x81\x00\x00").read_tag()

    def test_empty_buffer(self):
        with self.assertRaises(ber.BERError):
            ber.Decoder(b"").read_tag()

    def test_sequence_scope_is_bounded(self):
        """A nested sequence decoder must not read past its own length."""
        inner = ber.encode_sequence(ber.encode_integer(1))
        outer = inner + ber.encode_integer(999)
        sub, _ = ber.Decoder(outer).expect_sequence()
        self.assertEqual(sub.read_integer(), 1)
        self.assertEqual(sub.remaining, 0)


if __name__ == "__main__":
    unittest.main()
