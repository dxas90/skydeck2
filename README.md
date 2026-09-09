# SkyDeck GCU

**SkyDeck** turns a Steam Deck into a self-contained FPV Ground Control Unit:
ExpressLRS RC link, CRSF channel encoding on an ESP32-S2 bridge, and
low-latency live video via the OpenIPC Aviateur AppImage — all in one device.

---

## Hardware

| Part | Purpose |
|------|---------|
| Steam Deck | Host computer + display + controller |
| Happymodel ES24TX (or any ExpressLRS nano TX) | RC transmitter module |
| ESP32-S2 Mini | USB-CDC to CRSF bridge |
| RTL8812AU USB WiFi adapter | Aviateur video RX (OpenIPC WFB-NG link) |

3D-printable backpack / mount CAD:
https://cad.onshape.com/documents/0a85f5b80c6099a2fc1cf05d/w/0408ca52d32ec3c9c9f8f564/e/62ef1ad992c53a1e1d5da3ef

---

## Architecture

```
Steam Deck
  inputs (evdev)
      |
  skydeck_joystick_sender.py   (100 Hz, uv venv)
      |  USB-CDC ASCII packets
  ESP32-S2
      |  CRSF @ 400 kbaud UART
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

### 2. Flash the ESP32-S2

Open `skydeck_esp32/skydeck_esp32.ino` in the Arduino IDE (or PlatformIO)
with the ESP32 Arduino core installed, select your board, and flash.

### 3. Run

```bash
# From the project directory:
./deck.sh

# Or specify a port explicitly:
./deck.sh /dev/ttyACM0

# With live log output:
./deck.sh --log
```

`deck.sh` will:
1. Rebind the Steam Deck controller from `hid_steam` to `hid_generic`
2. Wait for the ESP32 serial device to appear
3. Activate the `skydeck_env` virtual environment
4. Launch `skydeck_joystick_sender.py` with auto-restart

### 4. Launch Aviateur (FPV video)

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

## Serial Packet Format

```
"LY LX RY RX LT RT LB RB :"
```

Each channel is a zero-padded 3-digit decimal in the range `[000 .. 800]`,
followed by `:` as the frame delimiter. Total: 25 bytes per packet @ 100 Hz.

| Channel | Input | Range | Description |
|---------|-------|-------|-------------|
| 1 (LY) | Left stick Y | 0-800 | Throttle / Pitch |
| 2 (LX) | Left stick X | 0-800 | Yaw |
| 3 (RY) | Right stick Y | 0-800 | Pitch / Throttle |
| 4 (RX) | Right stick X | 0-800 | Roll |
| 5 (LT) | Left trigger | 0-800 | Aux |
| 6 (RT) | Right trigger | 0-800 | Aux |
| 7 (LB) | Left bumper | 0 / 800 | Arm / Mode |
| 8 (RB) | Right bumper | 0 / 800 | Aux |

The ESP32 maps each value from `[0..800]` to CRSF range `[172..1811]` and packs
all 16 channels into a standard CRSF RC-channels frame transmitted at 500 Hz.

---

## File Structure

```
skydeck2/
  install.sh                    # one-time setup script
  deck.sh                       # runtime launcher
  skydeck.desktop               # KDE / Steam desktop entry
  skydeck_joystick_sender.py    # Python host sender
  skydeck_esp32/
    skydeck_esp32.ino           # ESP32-S2 firmware (Arduino)
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
