#!/usr/bin/env python3
"""
Cellular LTE System Tray Applet for Linux
Provides connection status, IP information, and quick connect/disconnect controls.
Compatible with KDE Plasma, GNOME, XFCE, and other Freedesktop-compliant desktops.
"""

import sys
import subprocess
import os
from PyQt5.QtWidgets import QApplication, QSystemTrayIcon, QMenu, QAction
from PyQt5.QtGui import QIcon
from PyQt5.QtCore import QTimer

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONNECT_SCRIPT = os.path.join(SCRIPT_DIR, "connect.sh")
DISCONNECT_SCRIPT = os.path.join(SCRIPT_DIR, "disconnect.sh")


class CellularTrayApp:
    def __init__(self):
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)

        self.tray_icon = QSystemTrayIcon()
        self.menu = QMenu()

        # Status item
        self.status_action = QAction("Checking cellular status...", self.menu)
        self.status_action.setEnabled(False)
        self.menu.addAction(self.status_action)

        self.menu.addSeparator()

        # Connect action
        self.connect_action = QAction("Connect Cellular", self.menu)
        self.connect_action.triggered.connect(self.connect_cellular)
        self.menu.addAction(self.connect_action)

        # Disconnect action
        self.disconnect_action = QAction("Disconnect Cellular", self.menu)
        self.disconnect_action.triggered.connect(self.disconnect_cellular)
        self.menu.addAction(self.disconnect_action)

        self.menu.addSeparator()

        # Quit action
        self.quit_action = QAction("Quit Indicator", self.menu)
        self.quit_action.triggered.connect(self.app.quit)
        self.menu.addAction(self.quit_action)

        self.tray_icon.setContextMenu(self.menu)

        # Timer to monitor status
        self.timer = QTimer()
        self.timer.timeout.connect(self.update_status)
        self.timer.start(3000)

        # Initial check
        self.update_status()
        self.tray_icon.show()

    def get_cellular_ip(self):
        try:
            out = subprocess.check_output(
                ["ip", "-4", "addr", "show", "wwan0"], stderr=subprocess.DEVNULL
            ).decode()
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("inet "):
                    return line.split()[1].split("/")[0]
        except Exception:
            pass
        return None

    def update_status(self):
        ip = self.get_cellular_ip()
        if ip:
            self.tray_icon.setIcon(
                QIcon.fromTheme(
                    "network-cellular-signal-excellent-symbolic",
                    QIcon.fromTheme("network-cellular-connected-symbolic"),
                )
            )
            self.tray_icon.setToolTip(f"Cellular LTE\nStatus: Connected\nIP: {ip}")
            self.status_action.setText(f"Connected: {ip} (LTE)")
            self.connect_action.setEnabled(False)
            self.disconnect_action.setEnabled(True)
        else:
            self.tray_icon.setIcon(
                QIcon.fromTheme(
                    "network-cellular-offline-symbolic",
                    QIcon.fromTheme("network-cellular-disabled-symbolic"),
                )
            )
            self.tray_icon.setToolTip("Cellular LTE\nStatus: Disconnected")
            self.status_action.setText("Cellular: Disconnected")
            self.connect_action.setEnabled(True)
            self.disconnect_action.setEnabled(False)

    def connect_cellular(self):
        # Open in terminal if available
        terminal_candidates = ["konsole", "gnome-terminal", "x-terminal-emulator", "xfce4-terminal", "xterm"]
        for term in terminal_candidates:
            if subprocess.call(["which", term], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0:
                subprocess.Popen([term, "-e", CONNECT_SCRIPT])
                return
        subprocess.Popen(["pkexec", CONNECT_SCRIPT])

    def disconnect_cellular(self):
        if os.path.exists(DISCONNECT_SCRIPT):
            subprocess.Popen(["pkexec", DISCONNECT_SCRIPT])
        else:
            subprocess.Popen(["pkexec", "ip", "link", "set", "dev", "wwan0", "down"])

    def run(self):
        sys.exit(self.app.exec_())


if __name__ == "__main__":
    app = CellularTrayApp()
    app.run()
