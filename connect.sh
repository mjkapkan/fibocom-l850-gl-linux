#!/bin/bash
# ==============================================================================
# Fibocom L850-GL / Intel XMM7360 Cellular Connection Script
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Ensure root privileges
if [ "$EUID" -ne 0 ]; then
  echo "Root privileges required to manage cellular connection. Prompting for sudo..."
  exec sudo "$0" "$@"
fi

echo "=================================================="
echo "Connecting to Cellular (Fibocom L850-GL / XMM7360)"
echo "=================================================="

# Function: Hardware ACPI reset for PCI slot if stuck in A-CD_READY after sleep
reset_modem_hardware() {
  echo "Modem is in unresponsive / crashed phase (A-CD_READY). Triggering PCIe ACPI reset..."
  SLOT=$(lspci -Dn -d 8086:7360 2>/dev/null | cut -f1 -d" " || echo "0000:05:00.0")
  [ -z "$SLOT" ] && SLOT="0000:05:00.0"

  if [ -d "/sys/bus/pci/drivers/iosm/${SLOT}" ]; then
    echo "${SLOT}" > /sys/bus/pci/drivers/iosm/unbind || true
    sleep 1
  fi

  if [ -f "/sys/bus/pci/devices/${SLOT}/reset_method" ]; then
    echo "acpi" > "/sys/bus/pci/devices/${SLOT}/reset_method" || true
  fi

  if [ -f "/sys/bus/pci/devices/${SLOT}/reset" ]; then
    echo 1 > "/sys/bus/pci/devices/${SLOT}/reset" || true
  fi

  sleep 3
  echo "${SLOT}" > /sys/bus/pci/drivers/iosm/bind || true

  for i in {1..15}; do
    [ -c /dev/wwan0xmmrpc0 ] && break
    sleep 1
  done
}

# Function: Test if /dev/wwan0xmmrpc0 responds or throws EIO
check_rpc_port() {
  python3 -c "
import os, sys
try:
    fd = os.open('/dev/wwan0xmmrpc0', os.O_RDWR)
    os.close(fd)
    sys.exit(0)
except Exception:
    sys.exit(1)
" >/dev/null 2>&1
}

# 1. Ensure ModemManager does not interfere
systemctl stop ModemManager >/dev/null 2>&1 || true
systemctl mask ModemManager >/dev/null 2>&1 || true

# 2. Check if device exists and is responsive
if [ ! -c /dev/wwan0xmmrpc0 ]; then
  echo "Device /dev/wwan0xmmrpc0 not found. Attempting hardware reset..."
  reset_modem_hardware
fi

if ! check_rpc_port; then
  reset_modem_hardware
fi

if ! check_rpc_port; then
  echo "ERROR: /dev/wwan0xmmrpc0 is unresponsive. A cold power cycle (power off for 10s) may be needed."
  exit 1
fi

# Parse APN argument or prompt user
APN=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -a|--apn)
      APN="$2"
      shift 2
      ;;
    *)
      shift
      ;;
  esac
done

if [ -z "$APN" ]; then
  read -p "Enter your mobile carrier's APN [default: internet]: " USER_INPUT
  APN="${USER_INPUT:-internet}"
fi

echo "Using APN: $APN"

# Ensure required python dependencies
pip3 install pyroute2 configargparse dbus-python >/dev/null 2>&1 || true

# Execute proprietary RPC connection sequence
echo "Negotiating cellular data session..."
if ! python3 "${SCRIPT_DIR}/rpc/open_xdatachannel.py" --apn "$APN" --metric 700; then
  echo "=================================================="
  echo "ERROR: Failed to establish data session with carrier."
  echo "Please verify your SIM card, APN settings, and network coverage."
  exit 1
fi

echo "=================================================="
echo "Verifying Internet Connectivity..."
if ping -I wwan0 -c 3 8.8.8.8 >/dev/null 2>&1; then
  echo "SUCCESS: Cellular connection established and online!"
else
  echo "NOTICE: Interface configured, but ping test failed. Check your APN settings or SIM data plan."
fi
echo "=================================================="
