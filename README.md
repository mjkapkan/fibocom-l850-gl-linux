# Fibocom L850-GL (Intel XMM7360) Linux Driver & Connection Manager

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A complete, ISP-agnostic solution for running the **Fibocom L850-GL** / **Intel XMM7360** LTE modem (PCI ID `8086:7360`) on modern Linux distributions (Ubuntu 22.04 / 24.04, Debian, Fedora, Arch, etc.).

Includes:
- **Automatic LTE Data Channel Setup** via proprietary Intel RPC protocol.
- **PCIe Runtime Power Management Fix** (stops the modem firmware from crashing after sleep/resume).
- **Intelligent Routing:** Configured with route metric `700` so your system automatically uses Wi-Fi when available, and instantly falls back to Cellular LTE when Wi-Fi disconnects.
- **Native Desktop System Tray Indicator:** Lightweight status applet for KDE Plasma, GNOME, XFCE, and other Freedesktop environments showing live cellular signal bars, IP address, and quick connect/disconnect controls.
- **100% ISP Agnostic:** Works with any mobile carrier worldwide.

---

## The Problem with L850-GL on Linux

If you have a laptop with an internal Fibocom L850-GL (Intel XMM7360) LTE card, you've likely encountered these frustrating issues:

1. **`sim-missing` or `unhandled port type` in ModemManager:**
   Ubuntu 22.04/24.04 ships with ModemManager versions earlier than 1.26. The Fibocom L850-GL uses Intel's proprietary **XMMRPC protocol**, which older ModemManager releases do not support. ModemManager attempts to probe AT serial ports that time out, leading to false "SIM not inserted" errors.
2. **Firmware Crash on Sleep/Resume (`PORT open refused, phase A-ROM`):**
   Linux kernel PCIe Runtime Power Management powers down the M.2 PCIe slot during system sleep. Upon waking, the modem gets trapped in a pre-boot ROM phase and refuses to open communication ports until a complete hardware power cycle.
3. **Soft Reboots Don't Fix It:**
   A standard `sudo reboot` leaves power supplied to the M.2 PCIe slot, keeping the crashed firmware state active across reboots.

---

## Prerequisites

Before using this tool:

1. **Disable SIM PIN (MANDATORY):**
   The XMM7360 RPC stack does not support interactive PIN entry. You must remove the PIN lock from your SIM card before using it (insert the SIM into a phone or boot Windows, and toggle off "SIM PIN Lock").
2. **One-Time Cold Reboot:**
   If your modem previously crashed during a sleep/suspend cycle, you must perform a **cold shutdown** (power off completely, wait 10 seconds for motherboard capacitors to drain, and power on).

---

## Quick Installation

Clone this repository and run the installer:

```bash
git clone https://github.com/mjkapkan/fibocom-l850-gl-linux.git
cd fibocom-l850-gl-linux
sudo ./install.sh
```

### What `install.sh` does:
1. Installs the PCIe power management udev rule (`/etc/udev/rules.d/99-wwan-nopm.rules`) to keep the modem's PCIe link active, permanently preventing sleep/resume crashes.
2. Installs required Python dependencies (`pyroute2`, `configargparse`, `dbus-python`, `PyQt5`).
3. Installs the connection tool to `/opt/fibocom-l850-gl/` and symlinks helper binaries to `/usr/local/bin/`.
4. Installs the desktop autostart entry so the cellular system tray indicator launches automatically when you log into your desktop.

---

## Usage

### 1. Connect to Cellular
```bash
connect-cellular
```
The script will prompt for your mobile carrier's **APN** (default: `internet`).

You can also pass your APN directly without prompts:
```bash
connect-cellular -a your.carrier.apn
```

### 2. Disconnect
```bash
disconnect-cellular
```

### 3. Desktop System Tray Applet
The tray applet starts automatically upon login. You can also start it manually at any time:
```bash
cellular-tray.py &
```

**Features:**
- **Signal Icon:** Displays native cellular signal bars when connected, or offline icon when disconnected.
- **Tooltip:** Hover over the icon to see connection status and assigned IP address.
- **Context Menu:** Click to view status, connect, or disconnect.

---

## How It Works Under the Hood

- **Kernel Driver:** Leverages the mainline `iosm` kernel module included in Linux 5.14+.
- **Protocol:** Uses Intel RPC calls (`UtaMsCallPsConnectReq`, `UtaRPCPsConnectToDatachannelReq`, `UtaRPCPSConnectSetupReq`) to negotiate the packet-switched data session directly over `/dev/wwan0xmmrpc0`.
- **Network Interface:** Brings up `wwan0`, assigns the cellular IP, configures DNS servers (`resolvectl` and `/etc/resolv.conf`), and installs a default route with metric `700`.

---

## Contributing & License

Contributions, bug reports, and improvements are welcome!

Distributed under the **MIT License**. See `LICENSE` for details.
