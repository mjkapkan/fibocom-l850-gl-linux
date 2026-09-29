#!/bin/bash
# ==============================================================================
# Fibocom L850-GL / Intel XMM7360 Cellular Disconnection Script
# ==============================================================================

set -e

if [ "$EUID" -ne 0 ]; then
  exec sudo "$0" "$@"
fi

echo "Disconnecting cellular interface (wwan0)..."
if ip link show wwan0 >/dev/null 2>&1; then
  ip link set dev wwan0 down
  echo "Interface wwan0 brought down."
else
  echo "Interface wwan0 not found."
fi
