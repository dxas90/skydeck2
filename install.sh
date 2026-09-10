#!/usr/bin/env bash
#
# install.sh — SkyDeck Ground Control Unit setup for Steam Deck
#
# This script:
#   1. Creates a uv virtual environment and installs Python dependencies
#   2. Downloads the Aviateur AppImage (OpenIPC FPV video ground station)
#   3. Installs persistent udev rules for the RTL8812AU WiFi adapter (used by Aviateur)
#   4. Registers SkyDeck as a non-Steam game so it launches from Gaming Mode
#
# Run once from the project directory:
#   chmod +x install.sh && ./install.sh

set -euo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
VENV_DIR="$SCRIPT_DIR/skydeck_env"
AVIATEUR_APPIMAGE="$SCRIPT_DIR/aviateur.AppImage"
UDEV_RULES_FILE="/etc/udev/rules.d/80-my8812au.rules"
UDEV_RULES_URL="https://raw.githubusercontent.com/OpenIPC/aviateur/refs/heads/main/80-my8812au.rules"
ATOMIC_UPDATE_CONF="/etc/atomic-update.conf.d/skydeck.conf"
RTW_BLACKLIST="/etc/modprobe.d/aviateur-rtl8812au.conf"
AVIATEUR_API="https://api.github.com/repos/OpenIPC/aviateur/releases/latest"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log()  { echo "[install] $*"; }
die()  { echo "[install] ERROR: $*" >&2; exit 1; }
need() { command -v "$1" &>/dev/null || die "'$1' is required but not found. Install it first."; }

# ---------------------------------------------------------------------------
# Step 1: Prerequisites check
# ---------------------------------------------------------------------------
log "Checking prerequisites..."
need curl
need python3

# uv is optional — we install it if missing
if ! command -v uv &>/dev/null; then
  log "uv not found — installing via pip..."
  python3 -m pip install --user uv || die "Failed to install uv"
  export PATH="$HOME/.local/bin:$PATH"
fi
need uv

# ---------------------------------------------------------------------------
# Step 2: Create uv virtual environment and install Python deps
# ---------------------------------------------------------------------------
log "Setting up Python virtual environment at $VENV_DIR ..."
if [[ ! -d "$VENV_DIR" ]]; then
  uv venv "$VENV_DIR"
  log "Virtual environment created."
else
  log "Virtual environment already exists — skipping creation."
fi

log "Installing Python dependencies..."
uv pip install --python "$VENV_DIR/bin/python" pyserial inputs

log "Python environment ready."

# ---------------------------------------------------------------------------
# Step 3: Download Aviateur AppImage
# ---------------------------------------------------------------------------
if [[ -f "$AVIATEUR_APPIMAGE" ]]; then
  log "Aviateur AppImage already present at $AVIATEUR_APPIMAGE — skipping download."
else
  log "Fetching latest Aviateur release info..."
  APPIMAGE_URL=$(
    curl -fsSL "$AVIATEUR_API" \
      | python3 -c "
import json, sys
data = json.load(sys.stdin)
assets = data.get('assets', [])
for a in assets:
    name = a['name']
    # Match e.g. Aviateur_0.3.3_linux_x86_64.AppImage
    if name.endswith('.AppImage') and 'linux' in name.lower() and 'x86_64' in name:
        print(a['browser_download_url'])
        break
" 2>/dev/null
  )

  if [[ -z "$APPIMAGE_URL" ]]; then
    die "Could not determine Aviateur AppImage download URL. Check https://github.com/OpenIPC/aviateur/releases"
  fi

  log "Downloading Aviateur from: $APPIMAGE_URL"
  curl -L --progress-bar -o "$AVIATEUR_APPIMAGE" "$APPIMAGE_URL"
  chmod +x "$AVIATEUR_APPIMAGE"
  log "Aviateur AppImage saved to $AVIATEUR_APPIMAGE"
fi

# ---------------------------------------------------------------------------
# Step 4: Install udev rules for RTL8812AU (Aviateur WiFi adapter)
#
# SteamOS uses atomic-update for its immutable rootfs — /etc is normally
# wiped on system updates.  The correct SteamOS-native way to keep a file
# persistent is to add its path to /etc/atomic-update.conf.d/*.conf.
# atomic-update then treats that path as user-owned and preserves it across
# every OS update, no boot services or backup copies needed.
# ---------------------------------------------------------------------------
log "Installing udev rules for RTL8812AU adapter..."

RULES_CONTENT="$(curl -fsSL "$UDEV_RULES_URL")"
if [[ -z "$RULES_CONTENT" ]]; then
  die "Failed to download udev rules from $UDEV_RULES_URL"
fi

# Write the rules file
if [[ -f "$UDEV_RULES_FILE" ]] && [[ "$(cat "$UDEV_RULES_FILE")" == "$RULES_CONTENT" ]]; then
  log "udev rules already up to date — skipping write."
else
  echo "$RULES_CONTENT" | sudo tee "$UDEV_RULES_FILE" > /dev/null
  sudo udevadm control --reload-rules
  sudo udevadm trigger
  log "udev rules installed and reloaded: $UDEV_RULES_FILE"
fi

# Register the path in atomic-update keep-list so it survives SteamOS updates
sudo mkdir -p "$(dirname "$ATOMIC_UPDATE_CONF")"
if grep -qxF "$UDEV_RULES_FILE" "$ATOMIC_UPDATE_CONF" 2>/dev/null; then
  log "atomic-update keep-list already contains $UDEV_RULES_FILE — skipping."
else
  echo "$UDEV_RULES_FILE" | sudo tee -a "$ATOMIC_UPDATE_CONF" > /dev/null
  log "Added $UDEV_RULES_FILE to $ATOMIC_UPDATE_CONF (persistent across SteamOS updates)."
fi

# ---------------------------------------------------------------------------
# Step 5: Aviateur RTL8812AU driver setup
#
# Aviateur uses its own userspace driver (devourer/libusb) to talk to the
# RTL8812AU directly.  The kernel's rtw88_8812au module must NOT be loaded —
# if it binds first, libusb cannot claim the device and Aviateur silently
# fails to start the feed.
#
# Fix: blacklist the kernel modules so they never auto-load.
# The blacklist file is also added to the atomic-update keep-list.
# ---------------------------------------------------------------------------
log "Blacklisting rtw88_8812au kernel module (Aviateur uses its own driver)..."

if [[ -f "$RTW_BLACKLIST" ]]; then
  log "rtw88_8812au blacklist already present — skipping."
else
  printf '%s\n' \
    "# Aviateur uses its own userspace RTL8812AU driver (devourer/libusb)." \
    "# The kernel rtw88 modules must not bind the adapter." \
    "blacklist rtw88_8812au" \
    "blacklist rtw88_8812a" \
    "blacklist rtw88_usb" \
    | sudo tee "$RTW_BLACKLIST" > /dev/null
  log "Blacklist written: $RTW_BLACKLIST"

  # Unload if currently loaded
  sudo modprobe -r rtw88_8812au rtw88_8812a rtw88_usb 2>/dev/null || true
  log "rtw88_8812au unloaded (if it was running)."
fi

# Add blacklist to atomic-update keep-list
if grep -qxF "$RTW_BLACKLIST" "$ATOMIC_UPDATE_CONF" 2>/dev/null; then
  log "atomic-update keep-list already contains $RTW_BLACKLIST — skipping."
else
  echo "$RTW_BLACKLIST" | sudo tee -a "$ATOMIC_UPDATE_CONF" > /dev/null
  log "Added $RTW_BLACKLIST to $ATOMIC_UPDATE_CONF."
fi

# ---------------------------------------------------------------------------
# Step 6: Grant Aviateur CAP_NET_ADMIN so it can set the adapter to
#         monitor/raw-RX mode without running as root.
# ---------------------------------------------------------------------------
log "Setting network capabilities on Aviateur AppImage..."
if getcap "$AVIATEUR_APPIMAGE" 2>/dev/null | grep -q "cap_net_admin"; then
  log "CAP_NET_ADMIN already set — skipping."
else
  sudo setcap cap_net_admin,cap_net_raw=eip "$AVIATEUR_APPIMAGE"
  log "cap_net_admin,cap_net_raw=eip set on $AVIATEUR_APPIMAGE"
fi

# ---------------------------------------------------------------------------
# Step 7: Copy WFB-NG gs.key if present alongside this script.
#
# Aviateur uses a Curve25519 keypair for WFB-NG packet decryption.
# The drone ships a matching key at /etc/drone.key — copy it here as gs.key
# so install.sh can push it into ~/.aviateur/gs.key automatically.
#
# How to get the key from the drone (RunCam WiFiLink 2 / any OpenIPC device):
#   sudo ip addr add 192.168.1.2/24 dev <ethernet-iface>
#   scp root@192.168.1.10:/etc/drone.key ./gs.key    (password: 12345)
# Then re-run install.sh.
# ---------------------------------------------------------------------------
GS_KEY_SRC="$SCRIPT_DIR/gs.key"
GS_KEY_DEST="$HOME/.aviateur/gs.key"
mkdir -p "$HOME/.aviateur"

if [[ -f "$GS_KEY_SRC" ]]; then
  if [[ -f "$GS_KEY_DEST" ]] && cmp -s "$GS_KEY_SRC" "$GS_KEY_DEST"; then
    log "gs.key already in place and up to date — skipping."
  else
    cp "$GS_KEY_SRC" "$GS_KEY_DEST"
    log "gs.key installed: $GS_KEY_DEST"
  fi

  # Point Aviateur config at the key
  AVIATEUR_CONFIG="$HOME/.aviateur/config.ini"
  if [[ -f "$AVIATEUR_CONFIG" ]]; then
    sed -i "s|key = .*|key = $GS_KEY_DEST|" "$AVIATEUR_CONFIG"
    log "Aviateur config updated to use $GS_KEY_DEST"
  fi
else
  log "No gs.key found next to install.sh."
  log "  To fix 'Unable to decrypt' errors, copy the drone key:"
  log "    sudo ip addr add 192.168.1.2/24 dev <ethernet-iface>"
  log "    scp root@192.168.1.10:/etc/drone.key $SCRIPT_DIR/gs.key"
  log "  Then re-run install.sh."
fi

# ---------------------------------------------------------------------------
# Step 8: Install launcher scripts and desktop entries
# ---------------------------------------------------------------------------
DESKTOP_SRC="$SCRIPT_DIR/skydeck.desktop"
DESKTOP_DEST="$HOME/.local/share/applications/skydeck.desktop"
AVIATEUR_DESKTOP_SRC="$SCRIPT_DIR/aviateur.desktop"
AVIATEUR_DESKTOP_DEST="$HOME/.local/share/applications/aviateur.desktop"
AVIATEUR_LAUNCHER="$SCRIPT_DIR/aviateur.sh"

mkdir -p "$HOME/.local/share/applications"

if [[ -f "$DESKTOP_SRC" ]]; then
  sed "s|/home/deck/SkyDeck|$SCRIPT_DIR|g" "$DESKTOP_SRC" > "$DESKTOP_DEST"
  chmod +x "$DESKTOP_DEST"
  log "Desktop entry installed: $DESKTOP_DEST"
else
  log "Warning: skydeck.desktop not found — skipping."
fi

if [[ -f "$AVIATEUR_DESKTOP_SRC" ]]; then
  sed "s|/home/deck/SkyDeck|$SCRIPT_DIR|g" "$AVIATEUR_DESKTOP_SRC" > "$AVIATEUR_DESKTOP_DEST"
  chmod +x "$AVIATEUR_DESKTOP_DEST"
  log "Aviateur desktop entry installed: $AVIATEUR_DESKTOP_DEST"
fi

if [[ -f "$AVIATEUR_LAUNCHER" ]]; then
  chmod +x "$AVIATEUR_LAUNCHER"
  log "Aviateur launcher ready: $AVIATEUR_LAUNCHER"
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
log ""
log "=== Installation complete ==="
log "  Done         : $(date)"
log "  Virtual env  : $VENV_DIR"
log "  Aviateur     : $AVIATEUR_APPIMAGE"
log "  udev rules   : $UDEV_RULES_FILE"
log "  rtw blacklist: $RTW_BLACKLIST"
log "  keep-list    : $ATOMIC_UPDATE_CONF (persistent across SteamOS updates)"
log "  Desktop entry: $DESKTOP_DEST"
log ""
log "To run SkyDeck (RC sender):"
log "  ./deck.sh"
log ""
log "To launch Aviateur (FPV video):"
log "  ./aviateur.sh"
log ""
log "IMPORTANT — USB port for RTL8812AU:"
log "  Plug the adapter into the Steam Deck USB-C port DIRECTLY"
log "  (not through a hub/dock). The internal USB 2.0 hub limits"
log "  bulk transfer size and causes 'Unable to decrypt' errors."
log ""
log "IMPORTANT — WFB-NG key:"
log "  If Aviateur shows a green screen or 'Unable to decrypt',"
log "  copy the key from your drone and re-run install.sh:"
log "    sudo ip addr add 192.168.1.2/24 dev <ethernet-iface>"
log "    scp root@192.168.1.10:/etc/drone.key ./gs.key  (pw: 12345)"
log "    ./install.sh"
log ""
log "For Steam Gaming Mode: add 'deck.sh' and 'aviateur.sh' as Non-Steam Games."
