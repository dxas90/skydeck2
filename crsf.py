"""
crsf.py — Shared CRSF protocol primitives for SkyDeck.

Used by both skydeck_joystick_sender.py and elrs_config.py.
"""

from __future__ import annotations

import sys
from collections.abc import Generator
from contextlib import contextmanager

import serial

# ---------------------------------------------------------------------------
# Wire constants
# ---------------------------------------------------------------------------
CRSF_BAUD = 400_000  # ExpressLRS native CRSF baud rate

# Address bytes
ADDR_BROADCAST = 0x00
ADDR_HANDSET = 0xEA  # ground-station / handset ("us")
ADDR_MODULE = 0xEE  # ELRS TX module
ADDR_FLIGHT_CTRL = 0xC8  # flight controller / extended-frame sync byte

# RC channel frame
CRSF_TYPE_CHANNELS = 0x16
CRSF_FRAME_SIZE = 26  # bytes on the wire for RC_CHANNELS_PACKED
CHAN_COUNT = 16

# Channel value range
CRSF_CH_MIN = 172
CRSF_CH_MID = 991
CRSF_CH_MAX = 1811

# Extended-packet frame types  (mirror of elrs.lua)
TYPE_PING_DEVICES = 0x28
TYPE_DEVICE_INFO = 0x29
TYPE_PARAM_RESPONSE = 0x2B
TYPE_PARAM_READ = 0x2C
TYPE_PARAM_WRITE = 0x2D
TYPE_ELRS_INFO = 0x2E


# ---------------------------------------------------------------------------
# CRC-8/DVB-S2
# ---------------------------------------------------------------------------


def _build_crc8_table() -> bytes:
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = ((crc << 1) ^ 0xD5) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
        table.append(crc)
    return bytes(table)


_CRC8_TABLE: bytes = _build_crc8_table()


def crc8_dvb_s2(data: bytes) -> int:
    """Compute CRC-8/DVB-S2 over data."""
    crc = 0
    for byte in data:
        crc = _CRC8_TABLE[crc ^ byte]
    return crc


# ---------------------------------------------------------------------------
# RC_CHANNELS_PACKED frame builder
# ---------------------------------------------------------------------------


def build_crsf_frame(channels: list[int]) -> bytes:
    """
    Pack up to 16 CRSF channel values (integers in [172..1811]) into a
    26-byte CRSF RC_CHANNELS_PACKED frame ready to write to serial.

    Channels shorter than 16 are padded with CRSF_CH_MID.
    Raises ValueError if the resulting frame is not exactly 26 bytes.
    """
    ch = list(channels[:CHAN_COUNT])
    ch += [CRSF_CH_MID] * (CHAN_COUNT - len(ch))

    # Pack 16 × 11-bit values, LSB-first, into 22 bytes
    bits = 0
    bit_count = 0
    packed = bytearray()
    for val in ch:
        bits |= (val & 0x7FF) << bit_count
        bit_count += 11
        while bit_count >= 8:
            packed.append(bits & 0xFF)
            bits >>= 8
            bit_count -= 8

    # Frame layout:
    #   [0]    ADDR_MODULE       sync / destination
    #   [1]    length            bytes after this field (type + packed + crc = 24)
    #   [2]    CRSF_TYPE_CHANNELS
    #   [3-24] 22 packed channel bytes
    #   [25]   CRC-8/DVB-S2 of bytes [2..24]
    payload = bytes([CRSF_TYPE_CHANNELS]) + bytes(packed)
    crc = crc8_dvb_s2(payload)
    frame = bytes([ADDR_MODULE, len(payload) + 1]) + payload + bytes([crc])
    if len(frame) != CRSF_FRAME_SIZE:
        raise ValueError(f"CRSF frame size {len(frame)}, expected {CRSF_FRAME_SIZE}")
    return frame


# ---------------------------------------------------------------------------
# Extended-packet frame builder / parser
# ---------------------------------------------------------------------------


def make_ext_frame(dest: int, src: int, frame_type: int, payload: bytes) -> bytes:
    """
    Build a CRSF extended-header frame.

    Extended frames use ADDR_FLIGHT_CTRL (0xC8) as the outer sync byte,
    followed by length, then [frame_type, dest, src, ...payload, CRC].
    """
    body = bytes([frame_type, dest, src]) + payload
    length = len(body) + 1  # +1 for CRC
    frame = bytes([ADDR_FLIGHT_CTRL, length]) + body
    frame += bytes([crc8_dvb_s2(body)])
    return frame


def parse_ext_frames(buf: bytes) -> tuple[list[tuple[int, bytes]], bytes]:
    """
    Parse as many complete CRSF frames as possible from buf.

    Returns (frames, leftover) where each frame is (frame_type, payload).
    payload does NOT include type, dest, src, or CRC.
    """
    frames: list[tuple[int, bytes]] = []
    i = 0
    while i + 1 < len(buf):
        # Sync byte: accept any known CRSF address
        if buf[i] not in (ADDR_MODULE, ADDR_HANDSET, ADDR_BROADCAST, ADDR_FLIGHT_CTRL):
            i += 1
            continue
        length = buf[i + 1]
        if length < 2:
            i += 1
            continue
        frame_end = i + 2 + length
        if frame_end > len(buf):
            break  # incomplete — wait for more data
        body = buf[i + 2 : frame_end - 1]
        got_crc = buf[frame_end - 1]
        if crc8_dvb_s2(body) != got_crc:
            i += 1  # bad CRC — skip sync byte and retry
            continue
        ftype = body[0]
        payload = body[3:] if len(body) >= 3 else b""
        frames.append((ftype, payload))
        i = frame_end
    return frames, buf[i:]


# ---------------------------------------------------------------------------
# Channel value mapping helpers
# ---------------------------------------------------------------------------


def norm_to_crsf(norm: float) -> int:
    """Map [0.0 .. 1.0] → CRSF range [172 .. 1811]."""
    return int(CRSF_CH_MIN + norm * (CRSF_CH_MAX - CRSF_CH_MIN))


def axis_to_crsf(v: float) -> int:
    """Map normalized axis [-1.0 .. +1.0] → CRSF range."""
    return norm_to_crsf((v + 1.0) / 2.0)


def trigger_to_crsf(v: float) -> int:
    """Map normalized trigger [0.0 .. 1.0] → CRSF range."""
    return norm_to_crsf(v)


def button_to_crsf(v: float) -> int:
    """Map button (0 or 1) → CRSF_CH_MIN / CRSF_CH_MAX."""
    return CRSF_CH_MAX if v else CRSF_CH_MIN


# ---------------------------------------------------------------------------
# Serial port helpers
# ---------------------------------------------------------------------------


def find_elrs_port(baud: int = CRSF_BAUD) -> str:
    """
    Probe /dev/ttyACM0-3 and return the first port that opens successfully.
    Exits with an error message if none are found.
    """
    candidates = [f"/dev/ttyACM{i}" for i in range(4)]
    for path in candidates:
        try:
            with serial.Serial(path, baud, timeout=0.1):
                return path
        except serial.SerialException:
            continue
    print(
        f"ERROR: ExpressLRS module not found on {', '.join(candidates)}. Plug it in and try again.",
        file=sys.stderr,
    )
    sys.exit(1)


@contextmanager
def open_serial(port: str, baud: int) -> Generator[serial.Serial, None, None]:
    """Open a serial port and guarantee it is closed on exit."""
    ser = serial.Serial(port, baud, timeout=1)
    try:
        yield ser
    finally:
        ser.close()
