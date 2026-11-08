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

# 2. Install PCIe Runtime Power Management & dialout permissions fix
echo "Installing PCIe Runtime Power Management fix (/etc/udev/rules.d/99-wwan-nopm.rules)..."
cat << 'EOF' > /etc/udev/rules.d/99-wwan-nopm.rules
ACTION=="add", SUBSYSTEM=="pci", ATTR{vendor}=="0x8086", ATTR{device}=="0x7360", ATTR{power/control}="on"
SUBSYSTEM=="wwan", GROUP="dialout", MODE="0660"
SUBSYSTEM=="net", KERNEL=="wwan*", GROUP="dialout", MODE="0660"
EOF

udevadm control --reload-rules
udevadm trigger

# Install systemd-sleep hook for automatic hardware recovery across sleep/suspend
echo "Installing sleep/resume recovery hook (/usr/lib/systemd/system-sleep/wwan-resume)..."
mkdir -p /usr/lib/systemd/system-sleep
cat << 'EOF' > /usr/lib/systemd/system-sleep/wwan-resume
#!/bin/bash
SLOT=$(lspci -Dn -d 8086:7360 2>/dev/null | cut -f1 -d" " || echo "0000:05:00.0")
[ -z "$SLOT" ] && SLOT="0000:05:00.0"

case "$1" in
  pre)
    # Unbind driver before sleep so PCIe clock gating does not crash modem firmware
    if [ -d "/sys/bus/pci/drivers/iosm/${SLOT}" ]; then
      echo "${SLOT}" > /sys/bus/pci/drivers/iosm/unbind || true
    fi
    ;;
  post)
    # Rebind driver cleanly on wake
    sleep 2
    if [ -d "/sys/bus/pci/drivers/iosm" ]; then
      echo "${SLOT}" > /sys/bus/pci/drivers/iosm/bind || true
    fi
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
cp -r "${SCRIPT_DIR}/cellular-sms.py" "${INSTALL_DIR}/"
cp -r "${SCRIPT_DIR}/rpc" "${INSTALL_DIR}/"

chmod +x "${INSTALL_DIR}/connect.sh"
chmod +x "${INSTALL_DIR}/disconnect.sh"
chmod +x "${INSTALL_DIR}/cellular-tray.py"
chmod +x "${INSTALL_DIR}/cellular-sms.py"

# Create symlinks in /usr/local/bin
ln -sf "${INSTALL_DIR}/connect.sh" /usr/local/bin/connect-cellular
ln -sf "${INSTALL_DIR}/disconnect.sh" /usr/local/bin/disconnect-cellular
ln -sf "${INSTALL_DIR}/cellular-tray.py" /usr/local/bin/cellular-tray.py
ln -sf "${INSTALL_DIR}/cellular-sms.py" /usr/local/bin/cellular-sms

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

# 5. Auto-connect at boot and after resume
CONFIG_FILE="/etc/default/fibocom-l850-gl"
if [ ! -f "${CONFIG_FILE}" ]; then
  APN=""
  if [ -t 0 ]; then
    read -r -p "Enter your mobile carrier's APN for auto-connect [default: internet]: " APN || true
  fi
  echo "Saving APN to ${CONFIG_FILE}..."
  printf 'APN=%s\n' "${APN:-internet}" > "${CONFIG_FILE}"
fi

echo "Installing auto-connect service (cellular-connect.service)..."
cp "${SCRIPT_DIR}/cellular-connect.service" /etc/systemd/system/cellular-connect.service
systemctl daemon-reload
systemctl enable cellular-connect.service

echo "=================================================="
echo "Installation complete!"
echo ""
echo "Commands available:"
echo "  connect-cellular       - Connect to mobile network (APN from ${CONFIG_FILE})"
echo "  connect-cellular -a APN - Connect directly with specified APN"
echo "  disconnect-cellular    - Disconnect mobile connection"
echo "  cellular-tray.py       - Launch desktop system tray status indicator"
echo ""
echo "The modem now connects automatically at boot and after resume."
echo "  Status/log:  systemctl status cellular-connect; journalctl -u cellular-connect"
echo "  Turn off:    sudo systemctl disable cellular-connect"
echo ""
echo "IMPORTANT REQUIREMENTS:"
echo "1. SIM PIN: You MUST disable the SIM PIN (using a phone or Windows)."
echo "2. Cold Reboot: If your modem was previously suspended/sleeping, perform a"
echo "   FULL COLD SHUTDOWN (power off completely for 10 seconds) once."
echo "=================================================="
