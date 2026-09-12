#!/usr/bin/env bash
#
# video.sh — SkyDeck FPV video receiver
#
# Two modes (auto-detected by what's available):
#
#   MODE A — Aviateur (default, easiest):
#     ./aviateur.sh
#
#   MODE B — wfb-ng + devourer + GStreamer (no Aviateur dependency):
#     RTL8812AU → devourer rxdemo → wfb_rx (C binary, libpcap) → GStreamer/mpv
#
#     Requires the rtl88xxau_wfb patched kernel driver (wfb-ng fork) OR
#     devourer creating a virtual monitor interface.
#     See: https://github.com/svpcom/wfb-ng
#
#   MODE C — Direct UDP (drone connected via Ethernet/USB):
#     If the drone is reachable over IP (e.g. wfb_tun tunnel or Ethernet),
#     wfb_rx on the drone forwards decoded video to this machine's UDP port.
#     gst-launch-1.0 udpsrc port=5600 ! h265parse ! <decoder> ! autovideosink
#
# Usage:
#   ./video.sh                  # auto-detect best mode
#   ./video.sh --mode a         # force Aviateur
#   ./video.sh --mode b         # force wfb-ng + devourer
#   ./video.sh --mode c         # direct UDP (drone on network)
#   ./video.sh --port 5600      # override UDP port (mode C)
#   ./video.sh --codec h264     # h264 or h265 (default: h265)
#   ./video.sh --help

set -euo pipefail
IFS=$'\n\t'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/skydeck_env"

# Defaults
MODE="auto"
UDP_PORT=5600
CODEC="h265"

for arg in "$@"; do
  case $arg in
    --mode)       shift; MODE="$1" ;;
    --mode=*)     MODE="${arg#--mode=}" ;;
    --port=*)     UDP_PORT="${arg#--port=}" ;;
    --codec=*)    CODEC="${arg#--codec=}" ;;
    --help|-h)
      sed -n 's/^# \{0,1\}//p; /^[^#]/q' "$0" | head -40
      exit 0
      ;;
  esac
done

log() { echo "[video] $*"; }
die() { echo "[video] ERROR: $*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Video player picker: prefer mpv (lower latency), fall back to gstreamer
# ---------------------------------------------------------------------------
make_player_cmd() {
  local port="$1" codec="$2"
  local upper_codec
  upper_codec="${codec^^}"

  if command -v mpv &>/dev/null; then
    # mpv with minimal latency settings
    echo "mpv --no-cache --untimed --no-demuxer-lavf-probe-info \
--demuxer=rawvideo --no-correct-pts \
udp://127.0.0.1:${port}?overrun_nonfatal=1&fifo_size=50000000"
  elif command -v gst-launch-1.0 &>/dev/null; then
    # Pick hardware decoder (Steam Deck has AMD VAAPI)
    local decode
    if gst-inspect-1.0 "vaapi${codec}dec" &>/dev/null 2>&1; then
      decode="vaapi${codec}dec"
    elif gst-inspect-1.0 "avdec_${codec}" &>/dev/null 2>&1; then
      decode="avdec_${codec}"
    else
      decode="decodebin"
    fi
    log "GStreamer decoder: $decode"
    echo "gst-launch-1.0 -v \
udpsrc port=${port} \
! ${codec}parse \
! ${decode} \
! autovideosink sync=false"
  else
    die "No video player found. Install mpv or gstreamer1.0-tools."
  fi
}

# ---------------------------------------------------------------------------
# MODE A — Aviateur
# ---------------------------------------------------------------------------
run_aviateur() {
  log "Mode A: launching Aviateur..."
  exec "$SCRIPT_DIR/aviateur.sh"
}

# ---------------------------------------------------------------------------
# MODE B — devourer rxdemo + wfb_rx
#
# Uses the pre-built rxdemo binary (downloaded by install.sh from CI release).
# rxdemo is configured entirely via environment variables — no CLI flags.
# See: https://github.com/OpenIPC/devourer (src/DeviceConfig.h for full list)
#
# Stack:
#   rxdemo  (devourer, libusb)  →  raw 802.11 frames on UDP
#   wfb_rx  (wfb-ng C binary)  →  decrypted RTP video on UDP 5600
#   player  (mpv / gstreamer)  →  display
# ---------------------------------------------------------------------------
run_wfb_ng() {
  log "Mode B: devourer rxdemo + wfb_rx + player"

  RXDEMO="$SCRIPT_DIR/rxdemo"
  [[ -x "$RXDEMO" ]] || die "rxdemo not found at $SCRIPT_DIR/rxdemo. Run install.sh first."

  GS_KEY="${HOME}/.aviateur/gs.key"
  [[ -f "$SCRIPT_DIR/gs.key" ]] && GS_KEY="$SCRIPT_DIR/gs.key"

  # devourer config via environment variables
  export DEVOURER_CHANNEL=161
  export DEVOURER_BW=20                      # 20 MHz — matches RunCam WiFiLink 2
  export DEVOURER_OUT_PORT="$WFB_RX_PORT"    # raw frames → wfb_rx
  [[ -f "$GS_KEY" ]] && export DEVOURER_KEY="$GS_KEY"

  WFB_RX_PORT=5800   # devourer → wfb_rx raw input port
  PLAYER_CMD=$(make_player_cmd "$UDP_PORT" "$CODEC")

  PIDS=()
  cleanup() { for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done; }
  trap cleanup EXIT INT TERM

  log "Starting rxdemo (channel $DEVOURER_CHANNEL, BW ${DEVOURER_BW}MHz)..."
  "$RXDEMO" &
  PIDS+=($!)
  sleep 2

  if command -v wfb_rx &>/dev/null; then
    log "Starting wfb_rx → UDP $UDP_PORT..."
    GS_KEY_ARGS=()
    [[ -f "$GS_KEY" ]] && GS_KEY_ARGS=(-K "$GS_KEY")
    wfb_rx "${GS_KEY_ARGS[@]}" -u "$UDP_PORT" -a "$WFB_RX_PORT" &
    PIDS+=($!)
    sleep 1
  else
    log "wfb_rx not found — install wfb-ng for full decryption."
    log "Trying direct UDP passthrough on port $UDP_PORT..."
  fi

  log "Starting player on UDP $UDP_PORT..."
  eval "$PLAYER_CMD"
}

# ---------------------------------------------------------------------------
# MODE C — Direct UDP (drone on network or wfb_tun tunnel)
#
# The RunCam WiFiLink 2 runs wfb_tun which creates a 10.5.0.x tunnel.
# When Ethernet-connected, the drone forwards decoded video as RTP/UDP.
# We can also receive it directly from the drone's wfb_rx output port.
#
# Pipeline:
#   majestic (drone) → udp://127.0.0.1:5600 (on drone)
#                    → wfb_tx (encoded over RF)
#                    → [GS receives, wfb_rx decodes]
#                    → udp://GS_IP:5600
#
# For local testing with drone on Ethernet, use RTSP directly:
#   mpv rtsp://192.168.1.10:554
# ---------------------------------------------------------------------------
run_direct_udp() {
  log "Mode C: direct UDP on port $UDP_PORT"
  log "Waiting for video on udp://127.0.0.1:$UDP_PORT ..."
  log "(If drone is on Ethernet, try: mpv rtsp://192.168.1.10:554)"

  PLAYER_CMD=$(make_player_cmd "$UDP_PORT" "$CODEC")
  log "Player: $PLAYER_CMD"
  eval "$PLAYER_CMD"
}

# ---------------------------------------------------------------------------
# Auto-detect mode
# ---------------------------------------------------------------------------
if [[ "$MODE" == "auto" ]]; then
  if [[ -f "$SCRIPT_DIR/aviateur.AppImage" ]]; then
    MODE="a"
  elif command -v wfb_rx &>/dev/null; then
    MODE="b"
  else
    MODE="c"
  fi
  log "Auto-detected mode: $MODE"
fi

case "$MODE" in
  a|aviateur) run_aviateur ;;
  b|wfb)      run_wfb_ng ;;
  c|udp)      run_direct_udp ;;
  *)          die "Unknown mode '$MODE'. Use a, b, or c." ;;
esac
