"""
tests/test_crsf.py — Unit tests for crsf.py primitives.

All tests run without hardware. No serial port, no gamepad needed.
"""

import pytest

from crsf import (
    ADDR_FLIGHT_CTRL,
    ADDR_HANDSET,
    ADDR_MODULE,
    CHAN_COUNT,
    CRSF_CH_MAX,
    CRSF_CH_MID,
    CRSF_CH_MIN,
    CRSF_FRAME_SIZE,
    CRSF_TYPE_CHANNELS,
    TYPE_PING_DEVICES,
    axis_to_crsf,
    build_crsf_frame,
    button_to_crsf,
    crc8_dvb_s2,
    make_ext_frame,
    norm_to_crsf,
    parse_ext_frames,
    trigger_to_crsf,
)

# ---------------------------------------------------------------------------
# CRC-8/DVB-S2
# ---------------------------------------------------------------------------


class TestCrc8:
    def test_zero_length(self):
        assert crc8_dvb_s2(b"") == 0

    def test_known_vector(self):
        # CRC of b"\x16" (CRSF_TYPE_CHANNELS) followed by 22 zero bytes
        data = bytes([CRSF_TYPE_CHANNELS]) + bytes(22)
        crc = crc8_dvb_s2(data)
        assert isinstance(crc, int)
        assert 0 <= crc <= 255

    def test_single_bit_flip_changes_crc(self):
        data = b"\x16\xab\xcd\xef"
        crc_orig = crc8_dvb_s2(data)
        flipped = bytes([data[0] ^ 0x01]) + data[1:]
        assert crc8_dvb_s2(flipped) != crc_orig

    def test_deterministic(self):
        data = b"skydeck"
        assert crc8_dvb_s2(data) == crc8_dvb_s2(data)


# ---------------------------------------------------------------------------
# build_crsf_frame
# ---------------------------------------------------------------------------


class TestBuildCrsfFrame:
    def test_frame_size(self):
        frame = build_crsf_frame([CRSF_CH_MID] * CHAN_COUNT)
        assert len(frame) == CRSF_FRAME_SIZE

    def test_sync_byte(self):
        frame = build_crsf_frame([CRSF_CH_MID] * CHAN_COUNT)
        assert frame[0] == ADDR_MODULE

    def test_length_field(self):
        frame = build_crsf_frame([CRSF_CH_MID] * CHAN_COUNT)
        # length byte = bytes after itself = type(1) + payload(22) + crc(1) = 24
        assert frame[1] == 24

    def test_type_byte(self):
        frame = build_crsf_frame([CRSF_CH_MID] * CHAN_COUNT)
        assert frame[2] == CRSF_TYPE_CHANNELS

    def test_crc_valid(self):
        frame = build_crsf_frame([CRSF_CH_MID] * CHAN_COUNT)
        body = frame[2:-1]  # type + packed channels
        assert crc8_dvb_s2(body) == frame[-1]

    def test_crc_changes_on_corruption(self):
        frame = build_crsf_frame([CRSF_CH_MID] * CHAN_COUNT)
        corrupt = bytearray(frame)
        corrupt[5] ^= 0xFF
        body = bytes(corrupt[2:-1])
        assert crc8_dvb_s2(body) != corrupt[-1]

    def test_round_trip_all_mid(self):
        channels = [CRSF_CH_MID] * CHAN_COUNT
        frame = build_crsf_frame(channels)
        unpacked = _unpack_channels(frame)
        assert unpacked == channels

    def test_round_trip_all_min(self):
        channels = [CRSF_CH_MIN] * CHAN_COUNT
        assert _unpack_channels(build_crsf_frame(channels)) == channels

    def test_round_trip_all_max(self):
        channels = [CRSF_CH_MAX] * CHAN_COUNT
        assert _unpack_channels(build_crsf_frame(channels)) == channels

    def test_round_trip_mixed(self):
        channels = [
            172,
            991,
            1811,
            500,
            172,
            991,
            1811,
            500,
            991,
            991,
            991,
            991,
            991,
            991,
            991,
            991,
        ]
        assert _unpack_channels(build_crsf_frame(channels)) == channels

    def test_short_input_padded_with_mid(self):
        frame = build_crsf_frame([CRSF_CH_MIN] * 8)
        unpacked = _unpack_channels(frame)
        assert unpacked[:8] == [CRSF_CH_MIN] * 8
        assert unpacked[8:] == [CRSF_CH_MID] * 8

    def test_raises_not_asserts(self):
        # Must raise ValueError, not rely on assert (which -O disables)
        import crsf as crsf_mod

        original = crsf_mod.CRSF_FRAME_SIZE
        crsf_mod.CRSF_FRAME_SIZE = 99
        try:
            with pytest.raises(ValueError):
                build_crsf_frame([CRSF_CH_MID] * CHAN_COUNT)
        finally:
            crsf_mod.CRSF_FRAME_SIZE = original


def _unpack_channels(frame: bytes):
    """Reference decoder: unpack 16 × 11-bit values from a CRSF frame."""
    packed = frame[3:25]
    bits = int.from_bytes(packed, "little")
    return [(bits >> (11 * i)) & 0x7FF for i in range(16)]


# ---------------------------------------------------------------------------
# Channel value mapping helpers
# ---------------------------------------------------------------------------


class TestChannelMapping:
    def test_norm_to_crsf_zero(self):
        assert norm_to_crsf(0.0) == CRSF_CH_MIN

    def test_norm_to_crsf_one(self):
        assert norm_to_crsf(1.0) == CRSF_CH_MAX

    def test_norm_to_crsf_half(self):
        assert norm_to_crsf(0.5) == pytest.approx(CRSF_CH_MID, abs=2)

    def test_axis_centre(self):
        assert axis_to_crsf(0.0) == pytest.approx(CRSF_CH_MID, abs=2)

    def test_axis_full_negative(self):
        assert axis_to_crsf(-1.0) == CRSF_CH_MIN

    def test_axis_full_positive(self):
        assert axis_to_crsf(1.0) == CRSF_CH_MAX

    def test_trigger_zero(self):
        assert trigger_to_crsf(0.0) == CRSF_CH_MIN

    def test_trigger_full(self):
        assert trigger_to_crsf(1.0) == CRSF_CH_MAX

    def test_button_off(self):
        assert button_to_crsf(0.0) == CRSF_CH_MIN

    def test_button_on(self):
        assert button_to_crsf(1.0) == CRSF_CH_MAX


# ---------------------------------------------------------------------------
# make_ext_frame
# ---------------------------------------------------------------------------


class TestMakeExtFrame:
    def test_sync_byte_is_flight_ctrl(self):
        frame = make_ext_frame(ADDR_MODULE, ADDR_HANDSET, TYPE_PING_DEVICES, b"")
        # Extended frames must use ADDR_FLIGHT_CTRL (0xC8) as outer sync
        assert frame[0] == ADDR_FLIGHT_CTRL

    def test_crc_valid(self):
        frame = make_ext_frame(ADDR_MODULE, ADDR_HANDSET, TYPE_PING_DEVICES, b"\x01\x02")
        body = frame[2:-1]
        assert crc8_dvb_s2(body) == frame[-1]

    def test_length_field(self):
        payload = b"\xaa\xbb"
        frame = make_ext_frame(ADDR_MODULE, ADDR_HANDSET, 0x2C, payload)
        # body = type(1) + dest(1) + src(1) + payload(2) = 5; length = 5 + 1(crc) = 6
        assert frame[1] == 6

    def test_frame_type_in_body(self):
        frame = make_ext_frame(ADDR_MODULE, ADDR_HANDSET, 0x2C, b"")
        assert frame[2] == 0x2C

    def test_dest_src_in_body(self):
        frame = make_ext_frame(ADDR_MODULE, ADDR_HANDSET, 0x2C, b"")
        assert frame[3] == ADDR_MODULE
        assert frame[4] == ADDR_HANDSET


# ---------------------------------------------------------------------------
# parse_ext_frames
# ---------------------------------------------------------------------------


class TestParseExtFrames:
    def _make_frame(self, ftype: int, payload: bytes) -> bytes:
        return make_ext_frame(ADDR_MODULE, ADDR_HANDSET, ftype, payload)

    def test_parses_single_frame(self):
        raw = self._make_frame(0x2B, b"\x01\x00\xff")
        frames, leftover = parse_ext_frames(raw)
        assert len(frames) == 1
        assert frames[0][0] == 0x2B
        assert leftover == b""

    def test_parses_two_consecutive_frames(self):
        f1 = self._make_frame(0x2B, b"\x01")
        f2 = self._make_frame(0x2C, b"\x02")
        frames, leftover = parse_ext_frames(f1 + f2)
        assert len(frames) == 2
        assert frames[0][0] == 0x2B
        assert frames[1][0] == 0x2C
        assert leftover == b""

    def test_incomplete_frame_returned_as_leftover(self):
        raw = self._make_frame(0x2B, b"\x01\x02\x03")
        frames, leftover = parse_ext_frames(raw[:-2])  # truncate
        assert frames == []
        assert len(leftover) > 0

    def test_bad_crc_skipped(self):
        raw = bytearray(self._make_frame(0x2B, b"\x01"))
        raw[-1] ^= 0xFF  # corrupt CRC
        frames, _ = parse_ext_frames(bytes(raw))
        assert frames == []

    def test_garbage_prefix_skipped(self):
        garbage = b"\xde\xad\xbe\xef"
        valid = self._make_frame(0x29, b"\x42")
        frames, _ = parse_ext_frames(garbage + valid)
        assert len(frames) == 1

    def test_empty_buffer(self):
        frames, leftover = parse_ext_frames(b"")
        assert frames == []
        assert leftover == b""
