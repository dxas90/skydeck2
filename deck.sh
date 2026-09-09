#!/usr/bin/env bash
#
# deck.sh — SkyDeck launcher for Steam Deck
#
#   Handles the full startup sequence:
#     1. Temporarily rebinds Steam Deck controllers from hid_steam → hid_generic
#        so the Python sender can read raw joystick events via evdev/inputs.
#     2. Waits for the ExpressLRS USB-CDC serial device (/dev/ttyACM*).
#     3. Activates the uv virtual environment.
#     4. Runs skydeck_joystick_sender.py with auto-restart on crash.
#     5. Restores hid_steam bindings on exit (SIGINT / SIGTERM / normal exit).
#
# Usage:
#   ./deck.sh [/dev/ttyACM0] [--log] [--help] [--version]
#
# Arguments:
#   /dev/ttyACM*   Explicit serial port (skips auto-detection)
#   --log          Also print log lines to stdout (in addition to deck.log)
#   --help / -h    Show this help and exit
#   --version / -v Show version and exit

set -euo pipefail
IFS=$'\n\t'

VERSION="2.0"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOGFILE="$SCRIPT_DIR/deck.log"
VENV_DIR="$SCRIPT_DIR/skydeck_env"
SENDER="$SCRIPT_DIR/skydeck_joystick_sender.py"
PORT=""
SHOW_LOG=0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
show_version() { echo "deck.sh v$VERSION — SkyDeck Steam Deck Launcher"; exit 0; }

show_help() {
  cat <<EOF
Usage: $0 [/dev/ttyACM0] [--log] [--help] [--version]

  Rebinds the Steam Deck gamepad to hid_generic, starts the CRSF joystick
  sender with auto-restart, and restores bindings on exit.

Arguments:
  /dev/ttyACM*   Explicit serial port for the ExpressLRS TX module
  --log          Print log output to screen as well as deck.log
  --help / -h    Show this help
  --version / -v Show version

The project directory is: $SCRIPT_DIR
Log file: $LOGFILE
EOF
  exit 0
}

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
for arg in "$@"; do
  case $arg in
    --help|-h)    show_help ;;
    --version|-v) show_version ;;
    --log)        SHOW_LOG=1 ;;
    /dev/ttyACM*) PORT="$arg" ;;
  esac
done

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log() {
  local ts msg
  ts="$(date +'%Y-%m-%d %H:%M:%S')"
  msg="[$ts] $*"
  if (( SHOW_LOG )); then
    echo "$msg" | tee -a "$LOGFILE"
  else
    echo "$msg" >> "$LOGFILE"
  fi
}

# ---------------------------------------------------------------------------
# Sender version fingerprint (sha1 + mtime)
# ---------------------------------------------------------------------------
log_sender_version() {
  if [[ ! -f "$SENDER" ]]; then
    log "ERROR: $SENDER not found!"
    exit 2
  fi
  local sha dt
  sha=$(sha1sum "$SENDER" | cut -d' ' -f1)
  dt=$(stat -c '%y' "$SENDER" | cut -d'.' -f1)
  log "Sender version: $sha  ($dt)"
}

# ---------------------------------------------------------------------------
# Steam Deck HID driver switch
#   hid_steam owns the controllers in Gaming Mode; we need hid_generic so
#   Linux exposes standard joystick/evdev nodes that inputs/evdev can read.
# ---------------------------------------------------------------------------
mapfile -t STEAM_IDS < <(ls /sys/bus/hid/drivers/hid_steam/ 2>/dev/null | grep -E '\.' || true)

if (( ${#STEAM_IDS[@]} )); then
  log "Found hid_steam devices: ${STEAM_IDS[*]}"
else
  log "No hid_steam devices found — driver switch skipped."
fi

cleanup() {
  log "Restoring hid_steam bindings..."
  for id in "${STEAM_IDS[@]:-}"; do
    if [[ -e "/sys/bus/hid/drivers/hid_generic/$id" ]]; then
      sudo tee "/sys/bus/hid/drivers/hid_generic/unbind" <<< "$id" >/dev/null || true
    fi
    sudo tee "/sys/bus/hid/drivers/hid_steam/bind" <<< "$id" >/dev/null || true
    log "  $id -> hid_steam"
  done
  log "Restore complete."
}
trap cleanup EXIT INT TERM

if (( ${#STEAM_IDS[@]} )); then
  log "Loading hid_generic module..."
  sudo modprobe hid_generic
  for id in "${STEAM_IDS[@]}"; do
    log "  Unbinding $id from hid_steam"
    sudo tee "/sys/bus/hid/drivers/hid_steam/unbind" <<< "$id" >/dev/null
    log "  Binding   $id to hid_generic"
    sudo tee "/sys/bus/hid/drivers/hid_generic/bind" <<< "$id" >/dev/null
  done
  log "Controller available via hid_generic."
fi

# ---------------------------------------------------------------------------
# Wait for the ExpressLRS USB-CDC serial device
# ---------------------------------------------------------------------------
wait_for_port() {
  if [[ -n "$PORT" ]]; then
    log "Using specified port: $PORT"
    return
  fi
  log "Waiting for /dev/ttyACM* (plug in the ExpressLRS module)..."
  while true; do
    local devs
    mapfile -t devs < <(compgen -G '/dev/ttyACM*' || true)
    if [[ ${#devs[@]} -gt 0 ]]; then
      PORT="${devs[0]}"
      log "Serial device found: $PORT"
      break
    fi
    sleep 1
  done
}

# ---------------------------------------------------------------------------
# Activate venv and launch sender
# ---------------------------------------------------------------------------
run_sender() {
  log "Starting sender on $PORT ..."

  if [[ -f "$VENV_DIR/bin/activate" ]]; then
    # shellcheck source=/dev/null
    source "$VENV_DIR/bin/activate"
    log "Virtual environment activated: $VENV_DIR"
  else
    log "WARNING: virtual environment not found at $VENV_DIR — run install.sh first"
  fi

  log_sender_version
  [[ -f "$SENDER" ]] || { log "ERROR: $SENDER not found"; exit 1; }

  exec python3 "$SENDER" -p "$PORT" >> "$LOGFILE" 2>&1
}

# ---------------------------------------------------------------------------
# Main loop — auto-restart sender on crash
# ---------------------------------------------------------------------------
log "==== deck.sh v$VERSION starting ===="
while true; do
  wait_for_port
  run_sender
  exit_code=$?
  log "$SENDER exited with code $exit_code — restarting in 1s"
  sleep 1
done
