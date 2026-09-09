#!/usr/bin/env python3
"""
skydeck_joystick_sender.py

Reads the Steam Deck (or any Linux gamepad) via the `inputs` library and
sends binary CRSF RC_CHANNELS_PACKED frames directly to an ExpressLRS TX
module connected over USB-CDC at 400 000 baud.

No ESP32 bridge needed — every ELRS module accepts CRSF natively.

Channel mapping (16 total; 8 active, remainder parked at CRSF mid):
    Ch1  Left stick Y  — Throttle (Mode 2) / Pitch (Mode 1)
    Ch2  Left stick X  — Yaw
    Ch3  Right stick Y — Pitch (Mode 2) / Throttle (Mode 1)
    Ch4  Right stick X — Roll
    Ch5  LB (toggle)   — ARM: press once to arm, press again to disarm
    Ch6  RB            — Aux 2 / flight-mode switch
    Ch7  Left trigger  — Aux 3
    Ch8  Right trigger — Aux 4
    Ch9-16             — parked at CRSF_CH_MID (991)

Arm toggle (Ch5):
    Fires on the rising edge of LB (button-down only).
    Armed   → Ch5 = CRSF_CH_MAX (1811)
    Disarmed → Ch5 = CRSF_CH_MIN (172)
    FC setup: map AUX1 (Ch5) as arm switch, threshold > ~1700.
"""

from __future__ import annotations

import argparse
import logging
import threading
import time

from inputs import UnpluggedError, get_gamepad

from crsf import (
    CHAN_COUNT,
    CRSF_BAUD,
    CRSF_CH_MAX,
    CRSF_CH_MID,
    CRSF_CH_MIN,
    axis_to_crsf,
    build_crsf_frame,
    button_to_crsf,
    find_elrs_port,
    open_serial,
    trigger_to_crsf,
)

# ---------------------------------------------------------------------------
# Gamepad normalisation constants
# ---------------------------------------------------------------------------
MAX_JOY_VAL = 32_767.0  # raw axis maximum from inputs library
MAX_TRIG_VAL = 255.0  # raw trigger maximum
DEADZONE = 0.05  # fraction of full-scale treated as centre

DEFAULT_HZ = 150  # send rate — ELRS handles up to 500 Hz
ACTIVE_CHANS = 8


# ---------------------------------------------------------------------------
# Gamepad reader — background thread
# ---------------------------------------------------------------------------


class GamepadReader:
    """
    Polls the gamepad in a daemon thread via `inputs.get_gamepad()`.
    The latest normalised channel values are always available via
    read_crsf_channels() without blocking the send loop.

    Arm toggle (Ch5 / LB):
        Rising edge of LB flips the internal armed flag.
        Ch5 reflects the flag — not the physical button state.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, float] = {}
        self._armed = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="gamepad-reader")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)

    @property
    def armed(self) -> bool:
        with self._lock:
            return self._armed

    def read_crsf_channels(self) -> list[int]:
        """Return 16 CRSF channel integers for the current gamepad state."""
        with self._lock:
            s = self._state.copy()
            armed = self._armed

        lx = s.get("ABS_X", 0.0)
        ly = -s.get("ABS_Y", 0.0)  # Y axis is inverted on most gamepads
        rx = s.get("ABS_RX", 0.0)
        ry = -s.get("ABS_RY", 0.0)
        lt = s.get("ABS_Z", 0.0)
        rt = s.get("ABS_RZ", 0.0)
        rb = float(s.get("BTN_TR", 0))

        active: list[int] = [
            axis_to_crsf(ly),  # Ch1  LY
            axis_to_crsf(lx),  # Ch2  LX
            axis_to_crsf(ry),  # Ch3  RY
            axis_to_crsf(rx),  # Ch4  RX
            CRSF_CH_MAX if armed else CRSF_CH_MIN,  # Ch5  ARM toggle
            button_to_crsf(rb),  # Ch6  RB
            trigger_to_crsf(lt),  # Ch7  LT
            trigger_to_crsf(rt),  # Ch8  RT
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
            "ABS_X": self._norm_axis,
            "ABS_Y": self._norm_axis,
            "ABS_RX": self._norm_axis,
            "ABS_RY": self._norm_axis,
            "ABS_Z": self._norm_trigger,
            "ABS_RZ": self._norm_trigger,
            # BTN_TL (LB) is handled as a toggle — not stored in _state
            "BTN_TR": float,
        }
        while not self._stop.is_set():
            try:
                for event in get_gamepad():
                    if event.code == "BTN_TL" and event.state == 1:
                        with self._lock:
                            self._armed = not self._armed
                        logging.info(
                            "Arm toggle: %s",
                            "ARMED" if self._armed else "DISARMED",
                        )
                    elif event.code in normalizers:
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
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="SkyDeck: stream gamepad input as CRSF to an ExpressLRS TX module",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-p",
        "--port",
        help="Serial port of the ELRS module, e.g. /dev/ttyACM0 (auto-detected if omitted)",
    )
    parser.add_argument(
        "-b",
        "--baud",
        type=int,
        default=CRSF_BAUD,
        help="Serial baud rate",
    )
    parser.add_argument(
        "-r",
        "--rate",
        type=int,
        default=DEFAULT_HZ,
        help="CRSF frame send rate in Hz",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    port = args.port or find_elrs_port(args.baud)
    interval = 1.0 / args.rate

    logging.info("Opening ELRS module on %s @ %d baud", port, args.baud)
    gamepad = GamepadReader()

    try:
        with open_serial(port, args.baud) as ser:
            logging.info("Sending CRSF at %d Hz — Ctrl-C to stop", args.rate)
            while True:
                t0 = time.monotonic()
                frame = build_crsf_frame(gamepad.read_crsf_channels())
                ser.write(frame)
                elapsed = time.monotonic() - t0
                slack = interval - elapsed
                if slack > 0:
                    time.sleep(slack)
    except KeyboardInterrupt:
        logging.info("Stopped by user (Ctrl-C)")
    except Exception as exc:
        logging.error("Fatal error: %s", exc)
        raise
    finally:
        gamepad.stop()


if __name__ == "__main__":
    main()
