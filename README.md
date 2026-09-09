# SkyDeck GCU

**SkyDeck** turns a Steam Deck into a self-contained FPV Ground Control Unit:
the Python script sends CRSF frames **directly** to an ExpressLRS TX module
over USB, and Aviateur provides low-latency live video via an RTL8812AU WiFi
adapter — no intermediate microcontroller needed.

---

## Hardware

| Part | Purpose |
|------|---------|
| Steam Deck | Host computer + display + controller |
| ExpressLRS TX module (e.g. Happymodel ES24TX, BetaFPV ELRS Nano) | RC transmitter — receives CRSF over USB |
| RTL8812AU USB WiFi adapter | Aviateur video RX (OpenIPC WFB-NG link) |

---

## Architecture

```
Steam Deck
  inputs (evdev)
      |
  skydeck_joystick_sender.py   (uv venv, 150 Hz)
      |  CRSF binary frames @ 400 000 baud  (USB-CDC)
  ExpressLRS TX module
      |  915 / 2.4 GHz RF
  Drone FC (ELRS RX)

RTL8812AU USB WiFi
      |
  Aviateur AppImage   (OpenIPC WFB-NG video)
      |
  Steam Deck display
```

---

## Quick Start

### 1. One-time Setup

```bash
git clone <your-fork-url> ~/SkyDeck
cd ~/SkyDeck
chmod +x install.sh deck.sh
./install.sh
```

`install.sh` will:
- Create a `uv` Python virtual environment (`skydeck_env/`)
- Install `pyserial` and `inputs` into it
- Download the latest Aviateur `.AppImage` from GitHub releases
- Install the RTL8812AU udev rule (`/etc/udev/rules.d/80-my8812au.rules`)
- Install the `.desktop` launcher for KDE / Steam

### 2. Run

```bash
# Auto-detect the ELRS module port:
./deck.sh

# Specify port explicitly:
./deck.sh /dev/ttyACM0

# With live log output on screen:
./deck.sh --log
```

`deck.sh` will:
1. Rebind the Steam Deck controller from `hid_steam` to `hid_generic`
2. Wait for the ELRS USB-CDC device to appear (`/dev/ttyACM*`)
3. Activate the `skydeck_env` virtual environment
4. Launch `skydeck_joystick_sender.py` with auto-restart on crash

### 3. Launch Aviateur (FPV video)

```bash
./aviateur.AppImage
```

Or launch it from the KDE application menu / Steam as a Non-Steam Game.

---

## Adding to Steam (Gaming Mode)

1. Open Steam in Desktop Mode.
2. **Games → Add a Non-Steam Game → Browse** — select `deck.sh`.
3. Repeat for `aviateur.AppImage`.
4. Both will appear in your Steam library and launch from Gaming Mode.

---

## CRSF Frame Format

The sender transmits standard 26-byte CRSF RC_CHANNELS_PACKED frames at
150 Hz directly understood by every ExpressLRS module:

```
Byte  0     0xEE  — destination (CRSF_ADDRESS_MODULE)
Byte  1     0x18  — payload length (24)
Byte  2     0x16  — frame type RC_CHANNELS_PACKED
Bytes 3-24        — 16 × 11-bit channel values, LSB-first packed
Byte  25          — CRC-8/DVB-S2 of bytes [2..24]
```

### Channel mapping

| Ch | Input | Description |
|----|-------|-------------|
| 1  | Left stick Y  | Throttle (Mode 2) / Pitch (Mode 1) |
| 2  | Left stick X  | Yaw |
| 3  | Right stick Y | Pitch (Mode 2) / Throttle (Mode 1) |
| 4  | Right stick X | Roll |
| 5  | **Left bumper (toggle)** | **ARM — press to arm, press again to disarm** |
| 6  | Right bumper  | Aux 2 / flight-mode switch |
| 7  | Left trigger  | Aux 3 |
| 8  | Right trigger | Aux 4 |
| 9-16 | — | Parked at CRSF mid (991) |

> FC setup: assign AUX1 (Ch5) as your arm switch with arm threshold above ~1700.
> LB is a software toggle: first press = 1811 (armed), second press = 172 (disarmed).
> The terminal log prints "Arm toggle: ARMED / DISARMED" on every state change.

All active channels map to CRSF range **172 (min) … 991 (mid) … 1811 (max)**.

---

## File Structure

```
skydeck2/
  install.sh                    # one-time setup script
  deck.sh                       # runtime launcher
  skydeck.desktop               # KDE / Steam desktop entry
  skydeck_joystick_sender.py    # Python CRSF sender
  skydeck_env/                  # uv venv (created by install.sh, not tracked)
  aviateur.AppImage             # downloaded by install.sh, not tracked
  deck.log                      # runtime log (not tracked)
```

---

## Dependencies

**Python (managed by `uv` venv):**
- `pyserial` — serial port communication
- `inputs` — cross-platform gamepad / evdev reading

**System:**
- `uv` (installed automatically by `install.sh` if missing)
- `sudo` — required for `modprobe` and udev rule installation

**External:**
- [Aviateur](https://github.com/OpenIPC/aviateur) — downloaded by `install.sh`

---

## License

MIT
