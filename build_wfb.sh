#!/usr/bin/env bash
#
# build_wfb.sh — Build and install the wfb-ng patched RTL8812AU driver
#                on SteamOS (immutable rootfs).
#
# The wfb-ng project forks the RTL8812AU kernel driver and patches it with
# monitor mode + packet injection support required by wfb_rx.
# This is the only piece that needs kernel-level work.
#
# SteamOS notes:
#   - The rootfs is immutable — disable it first (re-enables on reboot).
#   - DKMS is not available on SteamOS. We build the .ko manually and
#     install it to the current kernel's extra modules directory.
#   - The build must be re-run after every SteamOS kernel update.
#     Run: ./build_wfb.sh --reinstall
#
# Alternatives if this fails:
#   - Use Mode A (Aviateur AppImage) or Mode C (direct UDP) instead.
#   - Plug the RTL8812AU into a Fedora/Ubuntu machine and receive there.
#
# Usage:
#   sudo ./build_wfb.sh             # first install
#   sudo ./build_wfb.sh --reinstall # rebuild after kernel update

set -euo pipefail
IFS=$'\n\t'

[[ "$(id -u)" == "0" ]] || { echo "Run as root: sudo $0 $*" >&2; exit 1; }

REINSTALL=0
for arg in "$@"; do [[ "$arg" == "--reinstall" ]] && REINSTALL=1; done

KVER=$(uname -r)
BUILD_DIR="/tmp/rtl8812au-wfb-build"
MODULE_DEST="/lib/modules/$KVER/extra"

log() { echo "[build_wfb] $*"; }
die() { echo "[build_wfb] ERROR: $*" >&2; exit 1; }

# Check if already installed
if [[ -f "$MODULE_DEST/88XXau_wfb.ko" ]] && (( ! REINSTALL )); then
  log "Driver already installed for kernel $KVER. Use --reinstall to rebuild."
  modprobe 88XXau_wfb && log "Module loaded." || log "Warning: modprobe failed."
  exit 0
fi

log "Building rtl8812au wfb-ng driver for kernel $KVER..."

# Need build tools — temporarily unlock rootfs
steamos-readonly status | grep -q enabled && {
  log "Disabling SteamOS read-only filesystem..."
  steamos-readonly disable
  RELOCK=1
}

# Install kernel headers and build tools if missing
pacman -S --noconfirm --needed linux-neptune-headers base-devel 2>/dev/null || \
  log "Warning: pacman install may have had issues — continuing..."

# Clone / update the wfb-ng rtl8812au fork
if [[ -d "$BUILD_DIR/.git" ]]; then
  log "Updating rtl8812au source..."
  git -C "$BUILD_DIR" pull --ff-only
else
  log "Cloning svpcom/rtl8812au (wfb-ng patched driver)..."
  git clone --depth 1 -b v5.2.20 https://github.com/svpcom/rtl8812au.git "$BUILD_DIR"
fi

cd "$BUILD_DIR"

# Build against the current running kernel
make -j"$(nproc)" ARCH=x86_64 KSRC="/lib/modules/$KVER/build"

# Install the module
mkdir -p "$MODULE_DEST"
cp 88XXau.ko "$MODULE_DEST/88XXau_wfb.ko"
depmod -a "$KVER"
log "Module installed: $MODULE_DEST/88XXau_wfb.ko"

# Blacklist the stock modules (keep our wfb variant)
cat > /etc/modprobe.d/wfb-rtl8812au.conf << 'EOF'
# wfb-ng patched driver takes priority over stock rtw88/88XXau
blacklist rtw88_8812au
blacklist rtw88_8812a
blacklist rtw88_usb
blacklist 88XXau
blacklist 8812au
EOF
log "Blacklist written: /etc/modprobe.d/wfb-rtl8812au.conf"

# Add to atomic-update keep-list
ATOMIC_CONF="/etc/atomic-update.conf.d/skydeck.conf"
mkdir -p "$(dirname "$ATOMIC_CONF")"
for path in "$MODULE_DEST/88XXau_wfb.ko" "/etc/modprobe.d/wfb-rtl8812au.conf"; do
  grep -qxF "$path" "$ATOMIC_CONF" 2>/dev/null || echo "$path" >> "$ATOMIC_CONF"
done
log "Paths added to atomic-update keep-list."

# Re-lock rootfs
[[ "${RELOCK:-0}" == "1" ]] && steamos-readonly enable && log "Read-only filesystem re-enabled."

# Load module
modprobe 88XXau_wfb && log "Module loaded successfully." || \
  log "Warning: modprobe failed — try rebooting."

log ""
log "=== Done ==="
log "Kernel module: $MODULE_DEST/88XXau_wfb.ko"
log "Run ./video.sh --mode b to use wfb-ng video pipeline."
log ""
log "NOTE: After a SteamOS update, re-run: sudo ./build_wfb.sh --reinstall"
