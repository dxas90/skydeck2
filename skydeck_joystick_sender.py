#!/usr/bin/env python3
"""
skydeck_joystick_sender.py

Reads the Steam Deck (or any gamepad) via the `inputs` library,
maps axes/buttons to 8 CRSF-compatible channels (0-800 range),
and streams them over USB-CDC serial to the ESP32-S2 ELRS bridge
at the configured baud rate and update rate.

Packet format (ASCII, terminated by ':'  ):
    LY LX RY RX LT RT LB RB :
    e.g.  "400400400400000000000000:"  (all centered / released)

Channel mapping:
    Ch1  LY  — Left stick Y  (pitch / throttle depending on mode)
    Ch2  LX  — Left stick X  (yaw)
    Ch3  RY  — Right stick Y (throttle / pitch depending on mode)
    Ch4  RX  — Right stick X (roll)
    Ch5  LT  — Left trigger   (aux)
    Ch6  RT  — Right trigger  (aux)
    Ch7  LB  — Left bumper    (arm / mode switch)
    Ch8  RB  — Right bumper   (aux)
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
DEFAULT_BAUD = 115_200
DEFAULT_HZ   = 100       # sender update rate (packets per second)
CHAN_COUNT   = 8

MAX_JOY_VAL  = 32_767.0  # raw axis maximum from inputs library
MAX_TRIG_VAL = 255.0     # raw trigger maximum
DEADZONE     = 0.05      # fraction of full-scale treated as zero


# ---------------------------------------------------------------------------
# Value mapping helpers
# ---------------------------------------------------------------------------

def map_axis(v: float) -> int:
    """Map a normalized axis value [-1.0 .. +1.0] to channel range [0 .. 800]."""
    return int((v + 1.0) * 400.0)


def map_trigger(v: float) -> int:
    """Map a normalized trigger value [0.0 .. 1.0] to channel range [0 .. 800]."""
    return int(v * 800.0)


# ---------------------------------------------------------------------------
# Gamepad reader (background thread)
# ---------------------------------------------------------------------------

class GamepadReader:
    """
    Polls the gamepad in a background daemon thread via `inputs.get_gamepad()`.
    The latest normalized values are available via read_channels() at any time.

    Channel order: [LX, LY, RX, RY, LT, LB, RT, RB]
    """

    def __init__(self) -> None:
        self._lock  = threading.Lock()
        self._state: Dict[str, float] = {}
        self._stop  = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="gamepad-reader")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)

    def read_channels(self) -> List[float]:
        """Return the latest [LX, LY, RX, RY, LT, LB, RT, RB] as floats."""
        with self._lock:
            s = self._state.copy()
        return [
            s.get("ABS_X",  0.0),   # LX
            -s.get("ABS_Y", 0.0),   # LY  (Y axis is inverted on most gamepads)
            s.get("ABS_RX", 0.0),   # RX
            -s.get("ABS_RY", 0.0),  # RY
            s.get("ABS_Z",  0.0),   # LT
            float(s.get("BTN_TL", 0)),  # LB
            s.get("ABS_RZ", 0.0),   # RT
            float(s.get("BTN_TR", 0)),  # RB
        ]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _norm_axis(raw: int) -> float:
        """Normalize raw joystick axis to [-1.0 .. +1.0] with deadzone."""
        x = raw / MAX_JOY_VAL
        return x if abs(x) >= DEADZONE else 0.0

    @staticmethod
    def _norm_trigger(raw: int) -> float:
        """Normalize raw trigger value to [0.0 .. 1.0]."""
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
# Packet builder
# ---------------------------------------------------------------------------

def build_packet(channels: List[float]) -> bytes:
    """
    Build an ASCII serial packet from channel values.

    Input:  [LX, LY, RX, RY, LT, LB, RT, RB]
    Output: b"LY LX RY RX LT RT LB RB :"
            Each value is zero-padded to 3 digits (000-800), terminated by ':'.
    """
    lx, ly, rx, ry, lt, lb, rt, rb = channels[:CHAN_COUNT]
    packet = (
        f"{map_axis(ly):03d}"
        f"{map_axis(lx):03d}"
        f"{map_axis(ry):03d}"
        f"{map_axis(rx):03d}"
        f"{map_trigger(lt):03d}"
        f"{map_trigger(rt):03d}"
        f"{map_trigger(lb):03d}"
        f"{map_trigger(rb):03d}:"
    )
    return packet.encode("ascii")


# ---------------------------------------------------------------------------
# Serial helpers
# ---------------------------------------------------------------------------

@contextmanager
def open_serial(port: str, baud: int):
    """Context manager that opens and cleanly closes a serial port."""
    ser = serial.Serial(port, baud, timeout=1)
    try:
        yield ser
    finally:
        ser.close()


def auto_find_port(baud: int) -> str:
    """Try common USB-CDC device nodes and return the first responsive one."""
    candidates = [f"/dev/ttyACM{i}" for i in range(4)]
    for path in candidates:
        try:
            with serial.Serial(path, baud, timeout=0.1):
                logging.info("Auto-detected serial port: %s", path)
                return path
        except Exception:
            continue
    logging.error(
        "No serial device found on %s. "
        "Plug in the ExpressLRS module and try again.",
        ", ".join(candidates),
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="SkyDeck joystick-to-CRSF serial sender for Steam Deck",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("-p", "--port", help="Serial port, e.g. /dev/ttyACM0")
    parser.add_argument("-b", "--baud", type=int, default=DEFAULT_BAUD, help="Serial baud rate")
    parser.add_argument("-r", "--rate", type=int, default=DEFAULT_HZ,   help="Packet send rate (Hz)")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable DEBUG logging"
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    port     = args.port or auto_find_port(args.baud)
    interval = 1.0 / args.rate

    logging.info("Opening serial port %s @ %d baud", port, args.baud)
    gamepad = GamepadReader()

    try:
        with open_serial(port, args.baud) as ser:
            logging.info("Sender running at %d Hz — Ctrl-C to stop", args.rate)
            while True:
                t0  = time.monotonic()
                pkt = build_packet(gamepad.read_channels())
                ser.write(pkt)
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
