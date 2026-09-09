#!/usr/bin/env python3
"""
skydeck_joystick_sender.py

Reads the Steam Deck (or any gamepad) via the `inputs` library and sends
binary CRSF RC-channel frames directly to an ExpressLRS TX module connected
over USB (the module's USB-CDC / Backpack serial port).

No ESP32 bridge is needed — the ELRS module accepts CRSF natively at 400 000 baud.

CRSF channel mapping (16 channels total; 8 active, remainder parked at mid):
    Ch1  LY  — Left stick Y   (Throttle in Mode 2 / Pitch in Mode 1)
    Ch2  LX  — Left stick X   (Yaw)
    Ch3  RY  — Right stick Y  (Pitch in Mode 2 / Throttle in Mode 1)
    Ch4  RX  — Right stick X  (Roll)
    Ch5  LT  — Left trigger   (Aux 1)
    Ch6  RT  — Right trigger  (Aux 2)
    Ch7  LB  — Left bumper    (Arm / flight-mode switch)
    Ch8  RB  — Right bumper   (Aux 4)
    Ch9-16   — parked at CRSF mid (991)

CRSF frame structure (26 bytes):
    [0]     0xEE  destination address (CRSF_ADDRESS_MODULE)
    [1]     0x18  payload length = 24 (type + 22 packed channel bytes + CRC)
    [2]     0x16  frame type RC_CHANNELS_PACKED
    [3-24]  16 × 11-bit channel values, LSB-first, packed across byte boundaries
    [25]    CRC-8/DVB-S2 over bytes [2..24]
"""

import argparse
import logging
import sys
import threading
import time
from contextlib import contextmanager
from typing import Dict, List

import serial
from inputs import UnpluggedError, get_gamepad

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CRSF_BAUD    = 400_000   # ExpressLRS native CRSF baud rate
DEFAULT_HZ   = 150       # packet send rate — ELRS handles up to 500 Hz;
                          # 150 Hz is plenty and keeps USB overhead low
CHAN_COUNT    = 16        # total CRSF channels in one frame

# Active channel count (remainder parked at mid)
ACTIVE_CHANS  = 8

# CRSF channel value range
CRSF_CH_MIN   = 172
CRSF_CH_MID   = 991
CRSF_CH_MAX   = 1811

# CRSF frame constants
CRSF_ADDR_MODULE    = 0xEE
CRSF_TYPE_CHANNELS  = 0x16
CRSF_FRAME_SIZE     = 26   # total bytes on the wire

# Gamepad normalisation
MAX_JOY_VAL  = 32_767.0   # raw axis maximum from inputs library
MAX_TRIG_VAL = 255.0       # raw trigger maximum
DEADZONE     = 0.05        # fraction of full-scale treated as zero


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

_CRC8_TABLE = _build_crc8_table()


def crc8_dvb_s2(data: bytes) -> int:
    """Compute CRC-8/DVB-S2 over data."""
    crc = 0
    for byte in data:
        crc = _CRC8_TABLE[crc ^ byte]
    return crc


# ---------------------------------------------------------------------------
# CRSF channel value helpers
# ---------------------------------------------------------------------------

def norm_to_crsf(norm: float) -> int:
    """Map a normalized value [0.0 .. 1.0] to CRSF range [172 .. 1811]."""
    return int(CRSF_CH_MIN + norm * (CRSF_CH_MAX - CRSF_CH_MIN))


def axis_to_crsf(v: float) -> int:
    """Map a normalized axis [-1.0 .. +1.0] to CRSF range [172 .. 1811]."""
    return norm_to_crsf((v + 1.0) / 2.0)


def trigger_to_crsf(v: float) -> int:
    """Map a normalized trigger [0.0 .. 1.0] to CRSF range [172 .. 1811]."""
    return norm_to_crsf(v)


def button_to_crsf(v: float) -> int:
    """Map a button (0 or 1) to CRSF low / high."""
    return CRSF_CH_MAX if v else CRSF_CH_MIN


# ---------------------------------------------------------------------------
# CRSF frame builder
# ---------------------------------------------------------------------------

def build_crsf_frame(channels: List[int]) -> bytes:
    """
    Pack up to 16 CRSF channel values (integers in [172..1811]) into a
    26-byte CRSF RC_CHANNELS_PACKED frame ready to write to serial.

    Channels shorter than 16 are padded with CRSF_CH_MID.
    """
    # Pad / truncate to exactly 16 channels
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

    # Frame: addr | length | type | 22-byte packed channels | CRC
    # length field = bytes after itself = type(1) + packed(22) + crc(1) = 24
    payload = bytes([CRSF_TYPE_CHANNELS]) + bytes(packed)  # 23 bytes (type + packed)
    crc = crc8_dvb_s2(payload)
    frame = bytes([CRSF_ADDR_MODULE, len(payload) + 1]) + payload + bytes([crc])
    assert len(frame) == CRSF_FRAME_SIZE, f"Unexpected frame size: {len(frame)}"
    return frame


# ---------------------------------------------------------------------------
# Gamepad reader (background thread)
# ---------------------------------------------------------------------------

class GamepadReader:
    """
    Polls the gamepad in a background daemon thread via `inputs.get_gamepad()`.
    The latest normalized channel values are available via read_crsf_channels()
    at any time without blocking.
    """

    def __init__(self) -> None:
        self._lock   = threading.Lock()
        self._state: Dict[str, float] = {}
        self._stop   = threading.Event()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="gamepad-reader"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)

    def read_crsf_channels(self) -> List[int]:
        """
        Return 16 CRSF channel values reflecting the current gamepad state.
        Channels 1-8 are mapped from the gamepad; 9-16 are parked at mid.
        """
        with self._lock:
            s = self._state.copy()

        lx  = s.get("ABS_X",  0.0)
        ly  = -s.get("ABS_Y", 0.0)   # Y is inverted on most gamepads
        rx  = s.get("ABS_RX", 0.0)
        ry  = -s.get("ABS_RY", 0.0)
        lt  = s.get("ABS_Z",  0.0)
        rt  = s.get("ABS_RZ", 0.0)
        lb  = float(s.get("BTN_TL", 0))
        rb  = float(s.get("BTN_TR", 0))

        active = [
            axis_to_crsf(ly),      # Ch1  LY
            axis_to_crsf(lx),      # Ch2  LX
            axis_to_crsf(ry),      # Ch3  RY
            axis_to_crsf(rx),      # Ch4  RX
            trigger_to_crsf(lt),   # Ch5  LT
            trigger_to_crsf(rt),   # Ch6  RT
            button_to_crsf(lb),    # Ch7  LB
            button_to_crsf(rb),    # Ch8  RB
        ]
        return active + [CRSF_CH_MID] * (CHAN_COUNT - ACTIVE_CHANS)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _norm_axis(raw: int) -> float:
        x = raw / MAX_JOY_VAL
        return x if abs(x) >= DEADZONE else 0.0

    @staticmethod
    def _norm_trigger(raw: int) -> float:
        return max(0.0, min(1.0, raw / MAX_TRIG_VAL))

    def _run(self) -> None:
        normalizers = {
            "ABS_X":  self._norm_axis,
            "ABS_Y":  self._norm_axis,
            "ABS_RX": self._norm_axis,
            "ABS_RY": self._norm_axis,
            "ABS_Z":  self._norm_trigger,
            "ABS_RZ": self._norm_trigger,
            "BTN_TL": lambda v: float(v),
            "BTN_TR": lambda v: float(v),
        }
        while not self._stop.is_set():
            try:
                for event in get_gamepad():
                    if event.code in normalizers:
                        val = normalizers[event.code](event.state)
                        with self._lock:
                            self._state[event.code] = val
            except UnpluggedError:
                logging.warning("Gamepad unplugged — retrying in 0.5 s")
                time.sleep(0.5)
            except Exception:
                logging.exception("Unexpected error in gamepad reader")
                time.sleep(0.1)


# ---------------------------------------------------------------------------
# Serial helpers
# ---------------------------------------------------------------------------

@contextmanager
def open_serial(port: str, baud: int):
    """Context manager: open serial port, yield it, close on exit."""
    ser = serial.Serial(port, baud, timeout=1)
    try:
        yield ser
    finally:
        ser.close()


def auto_find_port(baud: int) -> str:
    """Probe common USB-CDC device nodes and return the first that opens."""
    candidates = [f"/dev/ttyACM{i}" for i in range(4)]
    for path in candidates:
        try:
            with serial.Serial(path, baud, timeout=0.1):
                logging.info("Auto-detected ELRS module on: %s", path)
                return path
        except Exception:
            continue
    logging.error(
        "ExpressLRS module not found on %s — plug it in and try again.",
        ", ".join(candidates),
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="SkyDeck: send gamepad input as CRSF directly to an ExpressLRS TX module",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-p", "--port",
        help="Serial port of the ELRS module, e.g. /dev/ttyACM0 (auto-detected if omitted)",
    )
    parser.add_argument(
        "-b", "--baud", type=int, default=CRSF_BAUD,
        help="Serial baud rate (must match ELRS Backpack CRSF baud)",
    )
    parser.add_argument(
        "-r", "--rate", type=int, default=DEFAULT_HZ,
        help="CRSF frame send rate in Hz",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable DEBUG logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    port     = args.port or auto_find_port(args.baud)
    interval = 1.0 / args.rate

    logging.info("Opening ELRS module on %s @ %d baud", port, args.baud)
    gamepad = GamepadReader()

    try:
        with open_serial(port, args.baud) as ser:
            logging.info("Sending CRSF frames at %d Hz — Ctrl-C to stop", args.rate)
            while True:
                t0    = time.monotonic()
                frame = build_crsf_frame(gamepad.read_crsf_channels())
                ser.write(frame)
                elapsed = time.monotonic() - t0
                remaining = interval - elapsed
                if remaining > 0:
                    time.sleep(remaining)
    except KeyboardInterrupt:
        logging.info("Stopped by user (Ctrl-C)")
    except serial.SerialException as exc:
        logging.error("Serial error: %s", exc)
        sys.exit(1)
    finally:
        gamepad.stop()


if __name__ == "__main__":
    main()
