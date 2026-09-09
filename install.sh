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
    if 'linux' in a['name'].lower() and a['name'].endswith('.AppImage'):
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
# ---------------------------------------------------------------------------
log "Installing udev rules for RTL8812AU adapter..."

RULES_CONTENT="$(curl -fsSL "$UDEV_RULES_URL")"
if [[ -z "$RULES_CONTENT" ]]; then
  die "Failed to download udev rules from $UDEV_RULES_URL"
fi

if [[ -f "$UDEV_RULES_FILE" ]] && [[ "$(cat "$UDEV_RULES_FILE")" == "$RULES_CONTENT" ]]; then
  log "udev rules already up to date — skipping."
else
  echo "$RULES_CONTENT" | sudo tee "$UDEV_RULES_FILE" > /dev/null
  sudo udevadm control --reload-rules
  sudo udevadm trigger
  log "udev rules installed and reloaded: $UDEV_RULES_FILE"
fi

# ---------------------------------------------------------------------------
# Step 5: Install .desktop file for Steam / KDE application launcher
# ---------------------------------------------------------------------------
DESKTOP_SRC="$SCRIPT_DIR/skydeck.desktop"
DESKTOP_DEST="$HOME/.local/share/applications/skydeck.desktop"

if [[ -f "$DESKTOP_SRC" ]]; then
  # Substitute actual project path into the Exec line before installing
  sed "s|/home/deck/SkyDeck|$SCRIPT_DIR|g" "$DESKTOP_SRC" > "$DESKTOP_DEST"
  chmod +x "$DESKTOP_DEST"
  log "Desktop entry installed: $DESKTOP_DEST"
else
  log "Warning: skydeck.desktop not found — skipping desktop entry installation."
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
log ""
log "=== Installation complete ==="
log "  Virtual env  : $VENV_DIR"
log "  Aviateur     : $AVIATEUR_APPIMAGE"
log "  udev rules   : $UDEV_RULES_FILE"
log "  Desktop entry: $DESKTOP_DEST"
log ""
log "To run SkyDeck manually:"
log "  ./deck.sh"
log ""
log "To launch Aviateur (FPV video):"
log "  $AVIATEUR_APPIMAGE"
log ""
log "For Steam Gaming Mode: add 'deck.sh' as a Non-Steam Game,"
log "  or install the .desktop entry above via Steam ROM Manager."
