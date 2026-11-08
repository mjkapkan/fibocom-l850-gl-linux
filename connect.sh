#!/bin/bash
# ==============================================================================
# Fibocom L850-GL / Intel XMM7360 Cellular Connection Script
#
# Usage: connect-cellular [-a APN]
#
# The APN comes from -a, else from APN= in /etc/default/fibocom-l850-gl, else
# from a prompt (interactive runs only), else defaults to "internet". This lets
# the cellular-connect systemd service run it unattended at boot and on resume.
#
# Exit codes: 0 connected (or another attempt is already running)
#             1 failed
#             3 the modem's radio is switched off at the modem level, e.g. by
#               turning Cellular off in Windows; retrying cannot help
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="/etc/default/fibocom-l850-gl"

# Ensure root privileges
if [ "$EUID" -ne 0 ]; then
  echo "Root privileges required to manage cellular connection. Prompting for sudo..."
  exec sudo "$0" "$@"
fi

# One connection attempt at a time: the boot service, the tray and a terminal
# must not drive the modem's RPC port concurrently.
exec 9>/run/fibocom-l850-gl-connect.lock
if ! flock -n 9; then
  echo "Another cellular connection attempt is already running."
  exit 0
fi

# Parse APN argument
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

if [ -z "$APN" ] && [ -r "$CONFIG_FILE" ]; then
  APN="$(sed -n 's/^APN=//p' "$CONFIG_FILE" | tail -n 1 | tr -d "\"'")"
fi

if [ -z "$APN" ] && [ -t 0 ]; then
  read -r -p "Enter your mobile carrier's APN [default: internet]: " USER_INPUT || true
  APN="${USER_INPUT:-}"
fi
APN="${APN:-internet}"

echo "=================================================="
echo "Connecting to Cellular (Fibocom L850-GL / XMM7360)"
echo "=================================================="

SLOT=$(lspci -Dn -d 8086:7360 2>/dev/null | cut -f1 -d" " || echo "0000:05:00.0")
[ -z "$SLOT" ] && SLOT="0000:05:00.0"

# Function: Test if /dev/wwan0xmmrpc0 responds
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

# Function: After the network never allowed the attach, read the modem's
# registration state (read-only). Returns 3 when the modem is deregistered
# (+COPS: 2) with its radio on: the modem refuses to search at all. That is the
# state Windows leaves behind when Cellular is turned off there, because Windows
# stores the setting in the modem; nothing on the Linux side switches it back.
diagnose_no_attach() {
  python3 - <<'PYEOF'
import os, re, sys, time

def at(fd, cmd, timeout):
    os.write(fd, (cmd + '\r\n').encode())
    buf, end = b'', time.time() + timeout
    while time.time() < end:
        try:
            buf += os.read(fd, 4096)
            if b'OK\r\n' in buf or b'ERROR' in buf:
                break
        except BlockingIOError:
            time.sleep(0.05)
    return ' '.join(buf.decode(errors='replace').split())

for port in ('/dev/wwan0at1', '/dev/wwan0at0'):
    try:
        fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    except OSError:
        continue
    try:
        if 'OK' not in at(fd, 'AT', 2):
            continue
        cops, cereg = at(fd, 'AT+COPS?', 5), at(fd, 'AT+CEREG?', 5)
        print('Modem registration state: %s | %s' % (cops, cereg))
        m = re.search(r'\+COPS:\s*(\d)', cops)
        sys.exit(3 if m and m.group(1) == '2' else 0)
    finally:
        os.close(fd)
print('No modem AT port answered; cannot read the registration state.')
sys.exit(0)
PYEOF
}

# Function: Safe unbind/bind recovery
recover_modem() {
  echo "Attempting driver unbind/bind re-initialization..."
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

  sleep 2
  echo "${SLOT}" > /sys/bus/pci/drivers/iosm/bind || true

  for i in {1..15}; do
    [ -c /dev/wwan0xmmrpc0 ] && break
    sleep 1
  done
}

# 1. Ensure system-sleep unbind/bind hook is installed
if [ ! -f /usr/lib/systemd/system-sleep/wwan-resume ]; then
  cat << 'EOF' > /usr/lib/systemd/system-sleep/wwan-resume
#!/bin/bash
SLOT=$(lspci -Dn -d 8086:7360 2>/dev/null | cut -f1 -d" " || echo "0000:05:00.0")
[ -z "$SLOT" ] && SLOT="0000:05:00.0"

case "$1" in
  pre)
    if [ -d "/sys/bus/pci/drivers/iosm/${SLOT}" ]; then
      echo "${SLOT}" > /sys/bus/pci/drivers/iosm/unbind || true
    fi
    ;;
  post)
    sleep 2
    if [ -d "/sys/bus/pci/drivers/iosm" ]; then
      echo "${SLOT}" > /sys/bus/pci/drivers/iosm/bind || true
    fi
    ;;
esac
EOF
  chmod +x /usr/lib/systemd/system-sleep/wwan-resume
fi

# 2. Ensure ModemManager does not interfere (mask only once: masking reloads systemd)
systemctl stop ModemManager >/dev/null 2>&1 || true
if [ "$(systemctl is-enabled ModemManager 2>/dev/null)" != "masked" ]; then
  systemctl mask ModemManager >/dev/null 2>&1 || true
fi

# 3. Check if device exists and is responsive
if [ ! -c /dev/wwan0xmmrpc0 ] || ! check_rpc_port; then
  recover_modem
fi

if ! check_rpc_port; then
  echo "ERROR: /dev/wwan0xmmrpc0 is currently stuck in A-CD_READY phase."
  echo "Because the hardware was suspended without the pre-sleep unbind hook,"
  echo "a one-time cold shutdown (power off completely for 10s) is required to clear it."
  exit 1
fi

echo "Using APN: $APN"

# Execute proprietary RPC connection sequence
echo "Negotiating cellular data session..."
rc=0
python3 "${SCRIPT_DIR}/rpc/open_xdatachannel.py" --apn "$APN" --metric 700 || rc=$?
if [ "$rc" -ne 0 ]; then
  echo "=================================================="
  if [ "$rc" -eq 4 ]; then
    diag=0
    diagnose_no_attach || diag=$?
    if [ "$diag" -eq 3 ]; then
      echo "ERROR: The modem's radio is switched off at the modem level (+COPS: 2),"
      echo "so it will not search for a network."
      echo "This happens when Cellular is turned off in Windows: Windows stores that"
      echo "setting in the modem, and this script cannot switch it back on."
      echo "Fix: boot Windows, turn Cellular on (Settings > Network & internet >"
      echo "Cellular), check that it connects, then shut down and boot Linux again."
      exit 3
    fi
  fi
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
