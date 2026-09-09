"""
tests/test_gamepad.py — Unit tests for GamepadReader (arm toggle logic,
channel mapping). Uses a fake event source so no gamepad hardware is needed.
"""

from __future__ import annotations

import threading
import time
import unittest.mock as mock

import pytest

from crsf import CRSF_CH_MAX, CRSF_CH_MID, CRSF_CH_MIN

# ---------------------------------------------------------------------------
# Helpers — fake gamepad events without hardware
# ---------------------------------------------------------------------------


class FakeEvent:
    def __init__(self, code: str, state: int):
        self.code = code
        self.state = state


def _make_reader(event_batches: list):
    """
    Patch inputs.get_gamepad to yield events from event_batches (list of lists).
    Returns a started GamepadReader with the patched source.
    Each inner list is one call to get_gamepad().
    After all batches are exhausted, get_gamepad blocks until stop() is called.
    """
    call_count = {"n": 0}
    ready = threading.Event()
    batches = list(event_batches)

    def fake_get_gamepad():
        idx = call_count["n"]
        call_count["n"] += 1
        if idx < len(batches):
            ready.set()
            return batches[idx]
        # Block until the reader is stopped
        time.sleep(10)
        return []

    with mock.patch("skydeck_joystick_sender.get_gamepad", side_effect=fake_get_gamepad):
        # Import inside patch context so the module picks up the mock
        import importlib

        import skydeck_joystick_sender as mod

        importlib.reload(mod)  # re-bind get_gamepad reference

        with mock.patch("skydeck_joystick_sender.get_gamepad", side_effect=fake_get_gamepad):
            reader = mod.GamepadReader()
            ready.wait(timeout=1.0)
            time.sleep(0.05)  # let the thread process events
            return reader


# ---------------------------------------------------------------------------
# Arm toggle tests
# ---------------------------------------------------------------------------


class TestArmToggle:
    def setup_method(self):
        import skydeck_joystick_sender as mod

        self.mod = mod

    def _reader_with_state(self, armed: bool = False, state: dict = None):
        """Build a GamepadReader with controlled internal state."""
        r = object.__new__(self.mod.GamepadReader)
        r._lock = threading.Lock()
        r._state = state or {}
        r._armed = armed
        r._stop = threading.Event()
        r._stop.set()  # don't start the background thread
        r._thread = threading.Thread(target=lambda: None, daemon=True)
        return r

    def test_starts_disarmed(self):
        r = self._reader_with_state()
        assert r.armed is False

    def test_ch5_low_when_disarmed(self):
        r = self._reader_with_state(armed=False)
        channels = r.read_crsf_channels()
        assert channels[4] == CRSF_CH_MIN  # Ch5 index 4

    def test_ch5_high_when_armed(self):
        r = self._reader_with_state(armed=True)
        channels = r.read_crsf_channels()
        assert channels[4] == CRSF_CH_MAX

    def test_toggle_flips_armed(self):
        r = self._reader_with_state(armed=False)
        with r._lock:
            r._armed = not r._armed
        assert r.armed is True

    def test_double_toggle_returns_to_disarmed(self):
        r = self._reader_with_state(armed=False)
        with r._lock:
            r._armed = not r._armed
        with r._lock:
            r._armed = not r._armed
        assert r.armed is False

    def test_ch5_index_is_4(self):
        """Ch5 is the 5th channel, zero-indexed as 4."""
        r = self._reader_with_state(armed=True)
        ch = r.read_crsf_channels()
        assert len(ch) == 16
        assert ch[4] == CRSF_CH_MAX

    def test_remaining_channels_count(self):
        r = self._reader_with_state()
        ch = r.read_crsf_channels()
        assert len(ch) == 16

    def test_inactive_channels_parked_at_mid(self):
        r = self._reader_with_state()
        ch = r.read_crsf_channels()
        assert all(v == CRSF_CH_MID for v in ch[8:])


# ---------------------------------------------------------------------------
# Channel mapping tests
# ---------------------------------------------------------------------------


class TestChannelMapping:
    def setup_method(self):
        import skydeck_joystick_sender as mod

        self.mod = mod

    def _reader_with_axes(self, **axes):
        r = object.__new__(self.mod.GamepadReader)
        r._lock = threading.Lock()
        r._state = {k: float(v) for k, v in axes.items()}
        r._armed = False
        r._stop = threading.Event()
        r._stop.set()
        r._thread = threading.Thread(target=lambda: None, daemon=True)
        return r

    def test_centred_sticks_map_to_mid(self):
        r = self._reader_with_axes(ABS_X=0.0, ABS_Y=0.0, ABS_RX=0.0, ABS_RY=0.0)
        ch = r.read_crsf_channels()
        from crsf import axis_to_crsf

        assert ch[0] == pytest.approx(axis_to_crsf(0.0), abs=2)  # LY
        assert ch[1] == pytest.approx(axis_to_crsf(0.0), abs=2)  # LX

    def test_y_axis_inverted(self):
        """Positive raw ABS_Y should produce LY below centre (inverted)."""
        from crsf import axis_to_crsf

        r = self._reader_with_axes(ABS_Y=1.0)
        ch = r.read_crsf_channels()
        mid = axis_to_crsf(0.0)
        assert ch[0] < mid  # LY = -ABS_Y → negative → below mid

    def test_trigger_released_is_min(self):
        r = self._reader_with_axes(ABS_Z=0.0, ABS_RZ=0.0)
        ch = r.read_crsf_channels()
        assert ch[6] == CRSF_CH_MIN  # LT  (Ch7, index 6)
        assert ch[7] == CRSF_CH_MIN  # RT  (Ch8, index 7)

    def test_trigger_full_is_max(self):
        r = self._reader_with_axes(ABS_Z=1.0, ABS_RZ=1.0)
        ch = r.read_crsf_channels()
        assert ch[6] == CRSF_CH_MAX
        assert ch[7] == CRSF_CH_MAX

    def test_rb_button_high(self):
        r = self._reader_with_axes(BTN_TR=1.0)
        ch = r.read_crsf_channels()
        assert ch[5] == CRSF_CH_MAX  # RB (Ch6, index 5)

    def test_rb_button_low(self):
        r = self._reader_with_axes(BTN_TR=0.0)
        ch = r.read_crsf_channels()
        assert ch[5] == CRSF_CH_MIN


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


class TestNormHelpers:
    def setup_method(self):
        import skydeck_joystick_sender as mod

        self.mod = mod

    def test_axis_deadzone(self):
        """Values within DEADZONE should return 0.0."""
        from skydeck_joystick_sender import DEADZONE, MAX_JOY_VAL, GamepadReader

        raw = int(DEADZONE * MAX_JOY_VAL * 0.9)  # just inside deadzone
        assert GamepadReader._norm_axis(raw) == 0.0

    def test_axis_outside_deadzone(self):
        from skydeck_joystick_sender import DEADZONE, MAX_JOY_VAL, GamepadReader

        raw = int(DEADZONE * MAX_JOY_VAL * 2)  # outside deadzone
        assert GamepadReader._norm_axis(raw) != 0.0

    def test_trigger_clamps_high(self):
        from skydeck_joystick_sender import GamepadReader

        assert GamepadReader._norm_trigger(9999) == 1.0

    def test_trigger_clamps_low(self):
        from skydeck_joystick_sender import GamepadReader

        assert GamepadReader._norm_trigger(-1) == 0.0
