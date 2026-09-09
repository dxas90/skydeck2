#!/usr/bin/env python3
"""
elrs_config.py — Interactive ELRS module configurator for SkyDeck.

Uses the CRSF Extended Packet protocol (same as the OpenTX/EdgeTX Lua script)
to discover and modify ExpressLRS TX module settings directly over the USB
serial port — no radio needed.

Run standalone:
    python3 elrs_config.py [-p /dev/ttyACM0]

Or called from skydeck_joystick_sender.py via --config flag (sender pauses
while the config UI is open, then resumes automatically).

Navigation (keyboard):
    Up / Down     — move between fields
    Left / Right  — change value of selected field
    Enter         — execute command / confirm
    r             — reload all fields from module
    q / Esc       — quit config, return to sender
"""

import argparse
import curses
import logging
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import serial

# ---------------------------------------------------------------------------
# CRSF extended-packet constants  (mirrors elrs.lua)
# ---------------------------------------------------------------------------
CRSF_BAUD           = 400_000

ADDR_BROADCAST      = 0x00
ADDR_HANDSET        = 0xEA   # "us" — the ground station / handset
ADDR_MODULE         = 0xEE   # ELRS TX module

# Extended frame types
TYPE_PING_DEVICES   = 0x28
TYPE_DEVICE_INFO    = 0x29
TYPE_PARAM_READ     = 0x2C
TYPE_PARAM_WRITE    = 0x2D
TYPE_ELRS_INFO      = 0x2E
TYPE_PARAM_RESPONSE = 0x2B

# Field types (match CRSF parameter type enum)
FTYPE_UINT8   = 0
FTYPE_INT8    = 1
FTYPE_UINT16  = 2
FTYPE_INT16   = 3
FTYPE_FLOAT   = 8
FTYPE_SELECT  = 9
FTYPE_STRING  = 10
FTYPE_FOLDER  = 11
FTYPE_INFO    = 12
FTYPE_COMMAND = 13

# ---------------------------------------------------------------------------
# CRC-8/DVB-S2  (same table as sender)
# ---------------------------------------------------------------------------
def _make_crc_table() -> bytes:
    t = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = ((c << 1) ^ 0xD5) & 0xFF if c & 0x80 else (c << 1) & 0xFF
        t.append(c)
    return bytes(t)

_CRC8 = _make_crc_table()

def crc8(data: bytes) -> int:
    c = 0
    for b in data:
        c = _CRC8[c ^ b]
    return c


# ---------------------------------------------------------------------------
# Low-level CRSF extended frame helpers
# ---------------------------------------------------------------------------

def make_ext_frame(dest: int, src: int, frame_type: int, payload: bytes) -> bytes:
    """Build a CRSF extended-header frame ready to write to serial."""
    body   = bytes([frame_type, dest, src]) + payload
    length = len(body) + 1   # +1 for CRC
    frame  = bytes([ADDR_MODULE, length]) + body
    frame += bytes([crc8(body)])
    return frame


def parse_ext_frames(buf: bytes) -> Tuple[List[Tuple[int, bytes]], bytes]:
    """
    Parse as many complete CRSF frames as possible from buf.
    Returns (list of (frame_type, payload), leftover_bytes).
    payload does NOT include the type byte, dest, src, or CRC.
    """
    frames: List[Tuple[int, bytes]] = []
    i = 0
    while i < len(buf) - 1:
        if buf[i] not in (ADDR_MODULE, ADDR_HANDSET, ADDR_BROADCAST, 0xC8):
            i += 1
            continue
        if i + 1 >= len(buf):
            break
        length = buf[i + 1]
        if length < 2 or i + 1 + length >= len(buf):
            break
        frame_end = i + 2 + length
        body      = buf[i + 2 : frame_end - 1]
        recv_crc  = buf[frame_end - 1]
        if crc8(body) != recv_crc:
            i += 1
            continue
        ftype   = body[0]
        payload = body[3:] if len(body) >= 3 else b""
        frames.append((ftype, payload))
        i = frame_end
    return frames, buf[i:]


# ---------------------------------------------------------------------------
# Field / Device model
# ---------------------------------------------------------------------------

class Field:
    """Represents one ELRS parameter field returned by the module."""
    def __init__(self, fid: int):
        self.id       = fid
        self.parent:  Optional[int] = None
        self.ftype:   Optional[int] = None
        self.name     = ""
        self.value:   Optional[int] = None   # int for numeric/select, str for string types
        self.min:     Optional[int] = None
        self.max:     Optional[int] = None
        self.options: List[str] = []
        self.unit     = ""
        self.info     = ""
        self.loaded   = False

    def value_str(self) -> str:
        if self.ftype == FTYPE_SELECT and self.options and self.value is not None:
            idx = int(self.value)
            return self.options[idx] if 0 <= idx < len(self.options) else str(self.value)
        if self.ftype == FTYPE_COMMAND:
            return "[run]"
        if self.ftype in (FTYPE_INFO, FTYPE_STRING):
            return str(self.value or "")
        if self.value is not None:
            return f"{self.value}{self.unit}"
        return "..."

    def can_edit(self) -> bool:
        return self.ftype in (
            FTYPE_UINT8, FTYPE_INT8, FTYPE_UINT16, FTYPE_INT16,
            FTYPE_FLOAT, FTYPE_SELECT, FTYPE_COMMAND,
        )


def _read_str(data: bytes, offset: int) -> Tuple[str, int]:
    """Read a null-terminated string from data at offset. Returns (str, new_offset)."""
    end = data.index(b"\x00", offset) if b"\x00" in data[offset:] else len(data)
    return data[offset:end].decode("utf-8", errors="replace"), end + 1


def _read_u(data: bytes, offset: int, size: int) -> int:
    val = 0
    for i in range(size):
        val = (val << 8) | data[offset + i]
    return val


def parse_param_response(field: Field, payload: bytes) -> None:
    """Decode a TYPE_PARAM_RESPONSE payload into a Field object."""
    if len(payload) < 3:
        return
    field.parent = payload[0] or None
    raw_type     = payload[1] & 0x7F
    field.ftype  = raw_type
    name, off    = _read_str(payload, 2)
    field.name   = name

    if raw_type in (FTYPE_UINT8, FTYPE_INT8):
        sz = 1
        field.value = _read_u(payload, off, sz)
        field.min   = _read_u(payload, off + sz, sz)
        field.max   = _read_u(payload, off + 2 * sz, sz)
        field.unit, _ = _read_str(payload, off + 4 * sz)
        if raw_type == FTYPE_INT8 and field.value >= 128:
            field.value -= 256

    elif raw_type in (FTYPE_UINT16, FTYPE_INT16):
        sz = 2
        field.value = _read_u(payload, off, sz)
        field.min   = _read_u(payload, off + sz, sz)
        field.max   = _read_u(payload, off + 2 * sz, sz)
        field.unit, _ = _read_str(payload, off + 4 * sz)

    elif raw_type == FTYPE_SELECT:
        # options separated by ; then null, then value byte
        raw = payload[off:]
        opts_bytes = raw.split(b"\x00")[0]
        field.options = [o.decode("utf-8", errors="replace")
                         for o in opts_bytes.split(b";") if o]
        val_off = off + len(opts_bytes) + 1
        field.value = payload[val_off] if val_off < len(payload) else 0

    elif raw_type in (FTYPE_STRING, FTYPE_INFO):
        s, _ = _read_str(payload, off)
        field.value = 0   # placeholder — string stored in field.info
        field.info  = s

    elif raw_type == FTYPE_COMMAND:
        field.info = payload[off + 1 :].decode("utf-8", errors="replace").rstrip("\x00")

    field.loaded = True


# ---------------------------------------------------------------------------
# ELRS session — manages serial I/O and field state
# ---------------------------------------------------------------------------

class ElrsSession:
    """
    Owns the serial port, handles request/response cycle for CRSF parameter
    protocol.  Thread-safe: safe to call from curses UI thread.
    """

    POLL_TIMEOUT = 2.0   # seconds to wait for a response

    def __init__(self, port: str, baud: int = CRSF_BAUD):
        self._ser    = serial.Serial(port, baud, timeout=0.05)
        self._lock   = threading.Lock()
        self._rxbuf  = b""
        self.device_name: str = ""
        self.fields: Dict[int, Field] = {}
        self.field_count: int = 0
        self.status: str = "Pinging module..."
        self._chunk_buf: Dict[int, bytes] = {}  # fid -> accumulated chunk data

    def close(self):
        self._ser.close()

    # --- low-level I/O ---

    def _write(self, frame: bytes) -> None:
        with self._lock:
            self._ser.write(frame)

    def _drain(self, timeout: float = 0.1) -> List[Tuple[int, bytes]]:
        """Read available bytes, parse and return complete frames."""
        deadline = time.monotonic() + timeout
        frames: List[Tuple[int, bytes]] = []
        while time.monotonic() < deadline:
            with self._lock:
                chunk = self._ser.read(256)
            if chunk:
                self._rxbuf += chunk
                new_frames, self._rxbuf = parse_ext_frames(self._rxbuf)
                frames.extend(new_frames)
            else:
                time.sleep(0.01)
        return frames

    # --- protocol actions ---

    def ping(self) -> bool:
        """Broadcast device ping, wait for device-info response."""
        frame = make_ext_frame(ADDR_BROADCAST, ADDR_HANDSET,
                               TYPE_PING_DEVICES, b"")
        self._write(frame)
        deadline = time.monotonic() + self.POLL_TIMEOUT
        while time.monotonic() < deadline:
            for ftype, payload in self._drain(0.1):
                if ftype == TYPE_DEVICE_INFO and len(payload) >= 4:
                    name, off = _read_str(payload, 1)
                    self.device_name  = name
                    self.field_count  = payload[off + 12] if off + 12 < len(payload) else 0
                    self.status       = f"Connected: {self.device_name}"
                    return True
        self.status = "No ELRS module responded to ping"
        return False

    def read_field(self, fid: int) -> Optional[Field]:
        """Request and return one parameter field from the module."""
        field = self.fields.setdefault(fid, Field(fid))
        chunk = 0
        accumulated = b""
        deadline = time.monotonic() + self.POLL_TIMEOUT

        while time.monotonic() < deadline:
            req = make_ext_frame(
                ADDR_MODULE, ADDR_HANDSET, TYPE_PARAM_READ,
                bytes([fid, chunk]),
            )
            self._write(req)
            for ftype, payload in self._drain(0.15):
                if ftype != TYPE_PARAM_RESPONSE or len(payload) < 2:
                    continue
                if payload[0] != fid:
                    continue
                chunks_remain = payload[1]
                accumulated  += payload[2:]
                if chunks_remain == 0:
                    parse_param_response(field, accumulated)
                    return field
                chunk += 1
                deadline = time.monotonic() + self.POLL_TIMEOUT
        return None

    def write_field(self, field: Field) -> None:
        """Send a parameter write for numeric or select fields."""
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
            payload = bytes([1])
        else:
            return
        frame = make_ext_frame(
            ADDR_MODULE, ADDR_HANDSET, TYPE_PARAM_WRITE,
            bytes([ADDR_MODULE, ADDR_HANDSET, field.id]) + payload,
        )
        self._write(frame)

    def load_all_fields(self, progress_cb=None) -> None:
        """Load every field from the module sequentially."""
        self.fields.clear()
        for fid in range(1, self.field_count + 1):
            self.read_field(fid)
            if progress_cb:
                progress_cb(fid, self.field_count)


# ---------------------------------------------------------------------------
# Curses TUI
# ---------------------------------------------------------------------------

def _run_tui(stdscr: "Any", session: ElrsSession) -> None:
    curses.curs_set(0)
    stdscr.nodelay(False)
    stdscr.timeout(100)

    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN,  -1)   # header
        curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_CYAN)   # selected
        curses.init_pair(3, curses.COLOR_YELLOW, -1)  # info/readonly
        curses.init_pair(4, curses.COLOR_GREEN,  -1)  # status ok
        curses.init_pair(5, curses.COLOR_RED,    -1)  # status error

    H, W = stdscr.getmaxyx()
    sel  = 0
    msg  = ""

    def status_line(text: str, ok: bool = True) -> None:
        attr = curses.color_pair(4) if ok else curses.color_pair(5)
        stdscr.addnstr(H - 1, 0, text.ljust(W - 1), W - 1, attr)

    def draw() -> None:
        stdscr.erase()
        title = f" SkyDeck — ELRS Config  [{session.device_name or 'connecting...'}] "
        stdscr.addnstr(0, 0, title.center(W), W, curses.color_pair(1) | curses.A_BOLD)
        stdscr.addnstr(1, 0, " Up/Down: navigate  Left/Right: change  Enter: run  r: reload  q: quit ".center(W), W)

        fields = [f for f in session.fields.values() if f.loaded and f.ftype != FTYPE_FOLDER]
        if not fields:
            stdscr.addnstr(3, 2, session.status, W - 2, curses.color_pair(3))
            status_line(session.status)
            stdscr.refresh()
            return

        visible_rows = H - 4
        start = max(0, sel - visible_rows // 2)
        for row, field in enumerate(fields[start: start + visible_rows]):
            y      = row + 2
            is_sel = (row + start) == sel
            attr   = curses.color_pair(2) if is_sel else 0
            if field.ftype in (FTYPE_INFO, FTYPE_STRING):
                attr |= curses.color_pair(3)

            label = f"  {field.name:<28}"
            value = field.value_str()
            if field.ftype == FTYPE_SELECT and field.options and is_sel:
                idx   = int(field.value or 0)
                prev_ = field.options[(idx - 1) % len(field.options)]
                next_ = field.options[(idx + 1) % len(field.options)]
                value = f"< {value} >"
            line  = f"{label} {value}"
            stdscr.addnstr(y, 0, line.ljust(W), W, attr)

        nonlocal msg
        status_line(msg or session.status)
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
        bar_w  = W - 20
        filled = int(bar_w * done / max(total, 1))
        bar    = "[" + "#" * filled + " " * (bar_w - filled) + "]"
        stdscr.addnstr(5, 2, f"{bar}  {done}/{total}", W - 2)
        stdscr.refresh()

    session.load_all_fields(progress_cb=_progress)

    while True:
        draw()
        key = stdscr.getch()
        fields = [f for f in session.fields.values() if f.loaded and f.ftype != FTYPE_FOLDER]
        if not fields:
            if key in (ord("q"), 27):
                break
            continue

        if key in (curses.KEY_UP, ord("k")):
            sel = max(0, sel - 1)
        elif key in (curses.KEY_DOWN, ord("j")):
            sel = min(len(fields) - 1, sel + 1)
        elif key in (curses.KEY_LEFT, ord("h")):
            f = fields[sel]
            if f.can_edit() and f.value is not None:
                if f.ftype == FTYPE_SELECT:
                    f.value = (int(f.value) - 1) % len(f.options)
                elif f.min is not None:
                    f.value = max(f.min, int(f.value) - 1)
                session.write_field(f)
                msg = f"Saved: {f.name} = {f.value_str()}"
        elif key in (curses.KEY_RIGHT, ord("l")):
            f = fields[sel]
            if f.can_edit() and f.value is not None:
                if f.ftype == FTYPE_SELECT:
                    f.value = (int(f.value) + 1) % len(f.options)
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
        elif key in (ord("q"), 27):   # q or Esc
            break


def run_config(port: str, baud: int = CRSF_BAUD) -> None:
    """Open the curses TUI for ELRS config on the given serial port."""
    session = ElrsSession(port, baud)
    try:
        curses.wrapper(_run_tui, session)
    finally:
        session.close()


# ---------------------------------------------------------------------------
# CLI entry point (standalone use)
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="SkyDeck ELRS Configurator — interactive TUI for ExpressLRS TX settings",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("-p", "--port", help="Serial port, e.g. /dev/ttyACM0 (auto-detected if omitted)")
    parser.add_argument("-b", "--baud", type=int, default=CRSF_BAUD, help="Baud rate")
    args = parser.parse_args()

    if not args.port:
        candidates = [f"/dev/ttyACM{i}" for i in range(4)]
        for c in candidates:
            try:
                with serial.Serial(c, args.baud, timeout=0.1):
                    args.port = c
                    break
            except Exception:
                continue
        if not args.port:
            print(f"ERROR: ELRS module not found on {', '.join(candidates)}", file=sys.stderr)
            sys.exit(1)

    print(f"Connecting to ELRS module on {args.port} @ {args.baud} baud...")
    run_config(args.port, args.baud)


if __name__ == "__main__":
    main()

