#!/usr/bin/env python3
"""
elrs_config.py — Interactive ELRS module configurator for SkyDeck.

Implements the CRSF Extended Packet protocol (mirrors elrs.lua) to read
and write ExpressLRS TX module parameters over USB-CDC.

Standalone:
    python3 elrs_config.py [-p /dev/ttyACM0]

Navigation:
    Up/Down      move between fields
    Left/Right   change value
    Enter        execute command
    r            reload all fields
    q / Esc      quit
"""

from __future__ import annotations

import argparse
import curses
import threading
import time
from typing import Any

import serial

from crsf import (
    ADDR_BROADCAST,
    ADDR_HANDSET,
    ADDR_MODULE,
    CRSF_BAUD,
    TYPE_DEVICE_INFO,
    TYPE_PARAM_READ,
    TYPE_PARAM_RESPONSE,
    TYPE_PARAM_WRITE,
    TYPE_PING_DEVICES,
    find_elrs_port,
    make_ext_frame,
    parse_ext_frames,
)

# ---------------------------------------------------------------------------
# Field type constants  (CRSF parameter type enum)
# ---------------------------------------------------------------------------
FTYPE_UINT8 = 0
FTYPE_INT8 = 1
FTYPE_UINT16 = 2
FTYPE_INT16 = 3
FTYPE_FLOAT = 8
FTYPE_SELECT = 9
FTYPE_STRING = 10
FTYPE_FOLDER = 11
FTYPE_INFO = 12
FTYPE_COMMAND = 13

_NUMERIC_TYPES = (FTYPE_UINT8, FTYPE_INT8, FTYPE_UINT16, FTYPE_INT16, FTYPE_FLOAT)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class Field:
    """One ELRS parameter field as returned by the module."""

    def __init__(self, fid: int) -> None:
        self.id: int = fid
        self.parent: int | None = None
        self.ftype: int | None = None
        self.name: str = ""
        self.value: int | None = None
        self.min: int | None = None
        self.max: int | None = None
        self.options: list[str] = []
        self.unit: str = ""
        self.info: str = ""  # string/info text or command description
        self.loaded: bool = False

    def value_str(self) -> str:
        if self.ftype == FTYPE_SELECT and self.options and self.value is not None:
            idx = int(self.value)
            return self.options[idx] if 0 <= idx < len(self.options) else str(self.value)
        if self.ftype == FTYPE_COMMAND:
            return "[run]"
        if self.ftype in (FTYPE_INFO, FTYPE_STRING):
            return self.info  # real text is always in .info
        if self.value is not None:
            return f"{self.value}{self.unit}"
        return "..."

    def can_edit(self) -> bool:
        return self.ftype in (*_NUMERIC_TYPES, FTYPE_SELECT, FTYPE_COMMAND)


# ---------------------------------------------------------------------------
# Binary helpers
# ---------------------------------------------------------------------------


def _read_str(data: bytes, offset: int) -> tuple[str, int]:
    """Read a null-terminated string. Returns (string, next_offset)."""
    try:
        end = data.index(0, offset)
    except ValueError:
        end = len(data)
    return data[offset:end].decode("utf-8", errors="replace"), end + 1


def _read_u(data: bytes, offset: int, size: int) -> int:
    result = 0
    for i in range(size):
        result = (result << 8) | data[offset + i]
    return result


def parse_param_response(field: Field, payload: bytes) -> None:
    """Decode a PARAM_RESPONSE payload into a Field in-place."""
    if len(payload) < 3:
        return

    field.parent = payload[0] or None
    raw_type = payload[1] & 0x7F
    field.ftype = raw_type
    field.name, off = _read_str(payload, 2)

    if raw_type in (FTYPE_UINT8, FTYPE_INT8):
        sz = 1
        field.value = _read_u(payload, off, sz)
        field.min = _read_u(payload, off + sz, sz)
        field.max = _read_u(payload, off + 2 * sz, sz)
        field.unit, _ = _read_str(payload, off + 4 * sz)
        if raw_type == FTYPE_INT8 and field.value >= 128:
            field.value -= 256

    elif raw_type in (FTYPE_UINT16, FTYPE_INT16):
        sz = 2
        field.value = _read_u(payload, off, sz)
        field.min = _read_u(payload, off + sz, sz)
        field.max = _read_u(payload, off + 2 * sz, sz)
        field.unit, _ = _read_str(payload, off + 4 * sz)

    elif raw_type == FTYPE_SELECT:
        opts_end = payload.index(0, off) if 0 in payload[off:] else len(payload)
        opts_bytes = payload[off:opts_end]
        field.options = [o.decode("utf-8", errors="replace") for o in opts_bytes.split(b";") if o]
        val_off = opts_end + 1
        field.value = payload[val_off] if val_off < len(payload) else 0

    elif raw_type in (FTYPE_STRING, FTYPE_INFO):
        field.info, _ = _read_str(payload, off)
        field.value = 0  # sentinel — display via .info

    elif raw_type == FTYPE_COMMAND:
        field.info = payload[off + 1 :].decode("utf-8", errors="replace").rstrip("\x00")

    field.loaded = True


# ---------------------------------------------------------------------------
# ELRS session — serial I/O + protocol state
# ---------------------------------------------------------------------------


class ElrsSession:
    """
    Manages one serial connection to an ELRS TX module.
    Implements the CRSF extended-packet request/response cycle.
    Thread-safe: read_field / write_field can be called from any thread.
    """

    POLL_TIMEOUT = 2.0

    def __init__(self, port: str, baud: int = CRSF_BAUD) -> None:
        self._ser = serial.Serial(port, baud, timeout=0.05)
        self._write_lock = threading.Lock()  # guards serial writes only
        self._read_lock = threading.Lock()  # guards _rxbuf
        self._rxbuf = b""
        self.device_name: str = ""
        self.fields: dict[int, Field] = {}
        self.field_count: int = 0
        self.status: str = "Pinging module..."

    def close(self) -> None:
        self._ser.close()

    # ------------------------------------------------------------------
    # Low-level I/O
    # ------------------------------------------------------------------

    def _write(self, frame: bytes) -> None:
        with self._write_lock:
            self._ser.write(frame)

    def _read_frames(self, timeout: float = 0.1) -> list[tuple[int, bytes]]:
        """
        Read bytes from serial until timeout, return all complete frames.
        Does NOT hold any lock while sleeping — write() is never blocked.
        """
        frames: list[tuple[int, bytes]] = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = self._ser.read(256)  # non-blocking (timeout=0.05 on Serial)
            if chunk:
                with self._read_lock:
                    self._rxbuf += chunk
                    new, self._rxbuf = parse_ext_frames(self._rxbuf)
                frames.extend(new)
            else:
                time.sleep(0.01)
        return frames

    # ------------------------------------------------------------------
    # Protocol actions
    # ------------------------------------------------------------------

    def ping(self) -> bool:
        """Broadcast device ping. Returns True if a module responds."""
        self._write(make_ext_frame(ADDR_BROADCAST, ADDR_HANDSET, TYPE_PING_DEVICES, b""))
        deadline = time.monotonic() + self.POLL_TIMEOUT
        while time.monotonic() < deadline:
            for ftype, payload in self._read_frames(0.1):
                if ftype == TYPE_DEVICE_INFO and len(payload) >= 4:
                    name, off = _read_str(payload, 1)
                    self.device_name = name
                    self.field_count = payload[off + 12] if off + 12 < len(payload) else 0
                    self.status = f"Connected: {self.device_name}"
                    return True
        self.status = "No ELRS module responded to ping"
        return False

    def read_field(self, fid: int) -> Field | None:
        """Fetch one parameter field from the module (handles chunked responses)."""
        field = self.fields.setdefault(fid, Field(fid))
        chunk_idx = 0
        accumulated = b""
        deadline = time.monotonic() + self.POLL_TIMEOUT

        while time.monotonic() < deadline:
            self._write(
                make_ext_frame(
                    ADDR_MODULE,
                    ADDR_HANDSET,
                    TYPE_PARAM_READ,
                    bytes([fid, chunk_idx]),
                )
            )
            for ftype, payload in self._read_frames(0.15):
                if ftype != TYPE_PARAM_RESPONSE or len(payload) < 2:
                    continue
                if payload[0] != fid:
                    continue
                chunks_remain = payload[1]
                accumulated += payload[2:]
                if chunks_remain == 0:
                    parse_param_response(field, accumulated)
                    return field
                chunk_idx += 1
                deadline = time.monotonic() + self.POLL_TIMEOUT
        return None

    def write_field(self, field: Field) -> None:
        """Send a parameter-write frame for numeric, select, or command fields."""
        if field.value is None:
            return
        v = int(field.value)
        if field.ftype in (FTYPE_UINT8, FTYPE_INT8):
            payload = bytes([v & 0xFF])
        elif field.ftype in (FTYPE_UINT16, FTYPE_INT16):
            payload = bytes([(v >> 8) & 0xFF, v & 0xFF])
        elif field.ftype == FTYPE_SELECT:
            payload = bytes([v & 0xFF])
        elif field.ftype == FTYPE_COMMAND:
            payload = bytes([1])  # lcsClick
        else:
            return
        self._write(
            make_ext_frame(
                ADDR_MODULE,
                ADDR_HANDSET,
                TYPE_PARAM_WRITE,
                bytes([ADDR_MODULE, ADDR_HANDSET, field.id]) + payload,
            )
        )

    def load_all_fields(self, progress_cb: Any = None) -> None:
        """Load every field sequentially, calling progress_cb(done, total) each step."""
        self.fields.clear()
        for fid in range(1, self.field_count + 1):
            self.read_field(fid)
            if progress_cb:
                progress_cb(fid, self.field_count)


# ---------------------------------------------------------------------------
# Curses TUI
# ---------------------------------------------------------------------------


def _run_tui(stdscr: Any, session: ElrsSession) -> None:
    curses.curs_set(0)
    stdscr.timeout(100)

    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)  # header
        curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_CYAN)  # selected row
        curses.init_pair(3, curses.COLOR_YELLOW, -1)  # readonly
        curses.init_pair(4, curses.COLOR_GREEN, -1)  # status ok
        curses.init_pair(5, curses.COLOR_RED, -1)  # status error

    H, W = stdscr.getmaxyx()
    sel = 0
    msg = ""

    HELP = " \u2191\u2193:move  \u2190\u2192:change  Enter:run  r:reload  q:quit "

    def _status(text: str, ok: bool = True) -> None:
        attr = curses.color_pair(4 if ok else 5)
        stdscr.addnstr(H - 1, 0, text.ljust(W - 1), W - 1, attr)

    def _visible_fields() -> list[Field]:
        return [f for f in session.fields.values() if f.loaded and f.ftype != FTYPE_FOLDER]

    def _draw() -> None:
        nonlocal msg
        stdscr.erase()
        title = f" SkyDeck \u2014 ELRS Config  [{session.device_name or 'connecting...'}] "
        stdscr.addnstr(0, 0, title.center(W), W, curses.color_pair(1) | curses.A_BOLD)
        stdscr.addnstr(1, 0, HELP.center(W), W)

        fields = _visible_fields()
        if not fields:
            stdscr.addnstr(3, 2, session.status, W - 2, curses.color_pair(3))
            _status(session.status)
            stdscr.refresh()
            return

        visible_rows = H - 4
        start = max(0, sel - visible_rows // 2)
        for row, field in enumerate(fields[start : start + visible_rows]):
            y = row + 2
            is_sel = (row + start) == sel
            attr = curses.color_pair(2) if is_sel else 0
            if field.ftype in (FTYPE_INFO, FTYPE_STRING):
                attr = curses.color_pair(3)

            label = f"  {field.name:<28}"
            value = field.value_str()
            if field.ftype == FTYPE_SELECT and field.options and is_sel:
                value = f"< {value} >"
            stdscr.addnstr(y, 0, f"{label} {value}".ljust(W), W, attr)

        _status(msg or session.status)
        msg = ""
        stdscr.refresh()

    # --- initial load ---
    stdscr.addnstr(3, 2, "Pinging ELRS module...", W - 2, curses.color_pair(3))
    stdscr.refresh()
    if not session.ping():
        stdscr.addnstr(4, 2, session.status, W - 2, curses.color_pair(5))
        stdscr.addnstr(5, 2, "Press any key to exit.", W - 2)
        stdscr.refresh()
        stdscr.getch()
        return

    stdscr.addnstr(4, 2, f"Loading {session.field_count} fields...", W - 2)
    stdscr.refresh()

    def _progress(done: int, total: int) -> None:
        bar_w = max(1, W - 20)
        filled = int(bar_w * done / max(total, 1))
        bar = "[" + "#" * filled + " " * (bar_w - filled) + "]"
        stdscr.addnstr(5, 2, f"{bar}  {done}/{total}", W - 2)
        stdscr.refresh()

    session.load_all_fields(progress_cb=_progress)

    # --- event loop ---
    while True:
        _draw()
        key = stdscr.getch()
        fields = _visible_fields()

        if key in (ord("q"), 27):
            break
        if not fields:
            continue

        if key in (curses.KEY_UP, ord("k")):
            sel = max(0, sel - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            sel = min(len(fields) - 1, sel + 1)
        elif key in (curses.KEY_LEFT, ord("h")):
            f = fields[sel]
            if f.can_edit() and f.value is not None:
                if f.ftype == FTYPE_SELECT:
                    f.value = (int(f.value) - 1) % max(len(f.options), 1)
                elif f.min is not None:
                    f.value = max(f.min, int(f.value) - 1)
                session.write_field(f)
                msg = f"Saved: {f.name} = {f.value_str()}"
        elif key in (curses.KEY_RIGHT, ord("l")):
            f = fields[sel]
            if f.can_edit() and f.value is not None:
                if f.ftype == FTYPE_SELECT:
                    f.value = (int(f.value) + 1) % max(len(f.options), 1)
                elif f.max is not None:
                    f.value = min(f.max, int(f.value) + 1)
                session.write_field(f)
                msg = f"Saved: {f.name} = {f.value_str()}"
        elif key in (curses.KEY_ENTER, 10, 13):
            f = fields[sel]
            if f.ftype == FTYPE_COMMAND:
                session.write_field(f)
                msg = f"Command sent: {f.name}"
        elif key == ord("r"):
            session.load_all_fields(progress_cb=_progress)
            msg = "Fields reloaded."


def run_config(port: str, baud: int = CRSF_BAUD) -> None:
    """Open the curses TUI against the given serial port."""
    session = ElrsSession(port, baud)
    try:
        curses.wrapper(_run_tui, session)
    finally:
        session.close()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="SkyDeck ELRS Configurator — TUI for ExpressLRS TX settings",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("-p", "--port", help="Serial port (auto-detected if omitted)")
    parser.add_argument("-b", "--baud", type=int, default=CRSF_BAUD)
    args = parser.parse_args()

    port = args.port or find_elrs_port(args.baud)
    print(f"Connecting to ELRS module on {port} @ {args.baud} baud...")
    run_config(port, args.baud)


if __name__ == "__main__":
    main()
