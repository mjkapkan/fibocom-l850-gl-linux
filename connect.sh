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

# Check if RPC character device exists
if [ ! -c /dev/wwan0xmmrpc0 ]; then
  echo "ERROR: /dev/wwan0xmmrpc0 not found."
  echo "Make sure the iosm kernel module is loaded ('sudo modprobe iosm') and the modem is installed."
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
python3 "${SCRIPT_DIR}/rpc/open_xdatachannel.py" --apn "$APN" --metric 700

echo "=================================================="
echo "Verifying Internet Connectivity..."
if ping -I wwan0 -c 3 8.8.8.8 >/dev/null 2>&1; then
  echo "SUCCESS: Cellular connection established and online!"
else
  echo "NOTICE: Interface configured, but ping test failed. Check your APN settings or SIM data plan."
fi
echo "=================================================="
