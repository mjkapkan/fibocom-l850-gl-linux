#!/bin/bash
# ==============================================================================
# Fibocom L850-GL / Intel XMM7360 Linux Setup & Installer
# ==============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ "$EUID" -ne 0 ]; then
  echo "Root privileges required for installation. Prompting for sudo..."
  exec sudo "$0" "$@"
fi

echo "=================================================="
echo "Installing Fibocom L850-GL / Intel XMM7360 Tools"
echo "=================================================="

# 1. Install system dependencies
echo "Installing system dependencies..."
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq python3 python3-pip python3-pyroute2 python3-dbus python3-pyqt5 >/dev/null 2>&1 || true
fi

# Ensure python libraries via pip as fallback
pip3 install pyroute2 configargparse dbus-python --break-system-packages >/dev/null 2>&1 || true

# 2. Install PCIe Runtime Power Management fix
# Prevents modem firmware from crashing in A-ROM/CD_READY phase after sleep/suspend
echo "Installing PCIe Runtime Power Management fix (/etc/udev/rules.d/99-wwan-nopm.rules)..."
cat << 'EOF' > /etc/udev/rules.d/99-wwan-nopm.rules
ACTION=="add", SUBSYSTEM=="pci", ATTR{vendor}=="0x8086", ATTR{device}=="0x7360", ATTR{power/control}="on"
EOF

udevadm control --reload-rules
udevadm trigger

# Install systemd-sleep hook for automatic hardware recovery after sleep/suspend
echo "Installing sleep/resume recovery hook (/usr/lib/systemd/system-sleep/wwan-resume)..."
mkdir -p /usr/lib/systemd/system-sleep
cat << 'EOF' > /usr/lib/systemd/system-sleep/wwan-resume
#!/bin/bash
case "$1/$2" in
  post/*)
    SLOT=$(lspci -Dn -d 8086:7360 2>/dev/null | cut -f1 -d" " || echo "0000:05:00.0")
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
    ;;
esac
EOF
chmod +x /usr/lib/systemd/system-sleep/wwan-resume

# 3. Install application to /opt/fibocom-l850-gl
INSTALL_DIR="/opt/fibocom-l850-gl"
echo "Installing files to ${INSTALL_DIR}..."
mkdir -p "${INSTALL_DIR}"
cp -r "${SCRIPT_DIR}/connect.sh" "${INSTALL_DIR}/"
cp -r "${SCRIPT_DIR}/disconnect.sh" "${INSTALL_DIR}/"
cp -r "${SCRIPT_DIR}/cellular-tray.py" "${INSTALL_DIR}/"
cp -r "${SCRIPT_DIR}/rpc" "${INSTALL_DIR}/"

chmod +x "${INSTALL_DIR}/connect.sh"
chmod +x "${INSTALL_DIR}/disconnect.sh"
chmod +x "${INSTALL_DIR}/cellular-tray.py"

# Create symlinks in /usr/local/bin
ln -sf "${INSTALL_DIR}/connect.sh" /usr/local/bin/connect-cellular
ln -sf "${INSTALL_DIR}/disconnect.sh" /usr/local/bin/disconnect-cellular
ln -sf "${INSTALL_DIR}/cellular-tray.py" /usr/local/bin/cellular-tray.py

# 4. Install global system tray autostart entry
echo "Installing desktop autostart entry..."
mkdir -p /etc/xdg/autostart
cat << 'EOF' > /etc/xdg/autostart/cellular-tray.desktop
[Desktop Entry]
Type=Application
Name=Cellular LTE Tray Indicator
Comment=System Tray Indicator for Fibocom L850-GL / Intel XMM7360 Cellular Connection
Exec=/usr/local/bin/cellular-tray.py
Icon=network-cellular-connected-symbolic
Terminal=false
Categories=Network;System;
X-KDE-autostart-after=panel
EOF

echo "=================================================="
echo "Installation complete!"
echo ""
echo "Commands available:"
echo "  connect-cellular       - Connect to mobile network (prompts for APN)"
echo "  connect-cellular -a APN - Connect directly with specified APN"
echo "  disconnect-cellular    - Disconnect mobile connection"
echo "  cellular-tray.py       - Launch desktop system tray status indicator"
echo ""
echo "IMPORTANT REQUIREMENTS:"
echo "1. SIM PIN: You MUST disable the SIM PIN (using a phone or Windows)."
echo "2. Cold Reboot: If your modem was previously suspended/sleeping, perform a"
echo "   FULL COLD SHUTDOWN (power off completely for 10 seconds) once."
echo "=================================================="
