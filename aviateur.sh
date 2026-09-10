#!/usr/bin/env bash
#
# aviateur.sh — Aviateur FPV launcher for Steam Deck
#
# Aviateur is an XWayland app that needs:
#   1. The XAUTHORITY cookie from the live KDE/Wayland session
#   2. CAP_NET_ADMIN on the AppImage (set by install.sh) so it can
#      claim the RTL8812AU via libusb without sudo
#
# Use this script instead of running the AppImage directly.
# It resolves the correct display credentials from the running
# KDE session regardless of how it is launched (terminal, Steam, .desktop).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPIMAGE="$SCRIPT_DIR/aviateur.AppImage"

if [[ ! -f "$APPIMAGE" ]]; then
  echo "ERROR: $APPIMAGE not found. Run install.sh first." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# Resolve display credentials from the live KDE/XWayland session
# ---------------------------------------------------------------------------
for pid in $(pgrep -u "$(id -u)" -f "kwin_wayland|plasmashell" 2>/dev/null); do
  env_vars=$(cat "/proc/$pid/environ" 2>/dev/null | tr '\0' '\n')
  _XAUTH=$(echo "$env_vars"    | grep "^XAUTHORITY="      | cut -d= -f2-)
  _DISPLAY=$(echo "$env_vars"  | grep "^DISPLAY="         | cut -d= -f2-)
  _WAYLAND=$(echo "$env_vars"  | grep "^WAYLAND_DISPLAY=" | cut -d= -f2-)
  if [[ -n "$_XAUTH" && -f "$_XAUTH" ]]; then
    export XAUTHORITY="$_XAUTH"
    export DISPLAY="${_DISPLAY:-:0}"
    export WAYLAND_DISPLAY="${_WAYLAND:-wayland-0}"
    break
  fi
done

export XDG_RUNTIME_DIR="/run/user/$(id -u)"

# ---------------------------------------------------------------------------
# Keep Aviateur's pid_vid in sync with the current USB device number.
# The RTL8812AU gets a new device number on every replug — Aviateur stores
# it as "RTL8812AU [bus:dev]" and will silently fail to find the adapter
# if the number drifts.
# ---------------------------------------------------------------------------
CONFIG="$HOME/.aviateur/config.ini"
if [[ -f "$CONFIG" ]]; then
  DEV=$(lsusb 2>/dev/null | grep "0bda:8812" | awk '{print $4}' | tr -d : | sed 's/^0*//')
  BUS=$(lsusb 2>/dev/null | grep "0bda:8812" | awk '{print $2}' | sed 's/^0*//')
  if [[ -n "$DEV" && -n "$BUS" ]]; then
    sed -i "s/pid_vid = .*/pid_vid = RTL8812AU [$BUS:$DEV]/" "$CONFIG"
  fi
fi

exec "$APPIMAGE"
