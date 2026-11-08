#!/usr/bin/env python3
"""
Cellular LTE system tray indicator for the Fibocom L850-GL / Intel XMM7360 on Linux.

- Signal bars from the modem's own measurements (AT+XCESQ: RSRP/RSRQ/SINR on LTE,
  RSCP/Ec/No on 3G), plus access technology and operator (AT+COPS?)
- Live throughput and data used on wwan0
- State of the automatic connection (cellular-connect.service), including a modem
  whose radio was switched off from Windows
- SMS inbox (read, send, delete) with desktop notifications for new messages,
  USSD queries, dialing
- Connect / Disconnect

Modem details need access to /dev/wwan0at* (group "dialout", set up by install.sh).
Without it the indicator still shows the connection state and throughput.
"""

import contextlib
import fcntl
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time

from PyQt5.QtCore import QTimer, Qt
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import (
    QApplication, QSystemTrayIcon, QMenu, QAction, QDialog, QWidget,
    QVBoxLayout, QHBoxLayout, QListWidget, QTextEdit, QLabel,
    QLineEdit, QPushButton, QMessageBox, QSplitter, QInputDialog
)

IFACE = "wwan0"
SERVICE = "cellular-connect.service"
CONNECT_CMD = "/usr/local/bin/connect-cellular"
# On the L850-GL, wwan0at1 answers AT commands; wwan0at0 is often silent.
AT_PORTS = ["/dev/wwan0at1", "/dev/wwan0at0"]
POLL_SECONDS = 10      # modem queries (signal, operator, new SMS)
REFRESH_MS = 2000      # link state, throughput, service state

FINAL_RE = re.compile(r"\r\n(OK|ERROR|\+CM[ES] ERROR:[^\r\n]*)\r\n")
PROMPT_RE = re.compile(r">\s?$|\r\n(ERROR|\+CM[ES] ERROR:[^\r\n]*)\r\n")
PHONE_RE = re.compile(r"^\+?[0-9]{3,20}$")
USSD_RE = re.compile(r"^[0-9*#+]{1,40}$")


# ----------------------------------------------------------------------------
# Modem AT port
# ----------------------------------------------------------------------------

class ModemUnavailable(Exception):
    pass


class ATPort:
    """Serialised access to the modem's AT port.

    Threads in this process share one lock. Other programs (cellular-sms,
    connect-cellular) are kept out with flock() on the port itself, so two
    programs never interleave commands and responses on the same port.
    """

    def __init__(self):
        self.fd = None
        self.port = None
        self.error = None
        self.generation = 0      # bumped on every successful open
        self._lock = threading.RLock()

    def close(self):
        if self.fd is not None:
            with contextlib.suppress(OSError):
                os.close(self.fd)
        self.fd = None

    def _drain(self):
        """Discard stale or unsolicited output before a new command."""
        while True:
            try:
                if not os.read(self.fd, 4096):
                    raise OSError("modem AT port closed")
            except BlockingIOError:
                return

    def _read_until(self, pattern, timeout):
        buf, end = "", time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                chunk = os.read(self.fd, 4096)
            except BlockingIOError:
                time.sleep(0.05)
                continue
            if not chunk:
                raise OSError("modem AT port closed")
            buf += chunk.decode("utf-8", errors="replace")
            if re.search(pattern, buf):
                break
        return buf

    def _open(self):
        if self.fd is not None:
            return
        problems = []
        for path in AT_PORTS:
            if not os.path.exists(path):
                continue
            try:
                fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            except OSError as e:
                problems.append(f"{path}: {e.strerror}")
                continue
            self.fd = fd
            ok = False
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    self._drain()
                    ok = "OK" in self.cmd("AT", 1.5)
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            if ok:
                self.port, self.error = path, None
                self.generation += 1
                return
            self.close()
            problems.append(f"{path}: no answer")
        self.error = "; ".join(problems) or "no modem AT port found"
        raise ModemUnavailable(self.error)

    @contextlib.contextmanager
    def session(self):
        """Exclusive use of the port for a sequence of commands."""
        with self._lock:
            self._open()
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX)
                try:
                    self._drain()
                    yield self
                finally:
                    if self.fd is not None:
                        fcntl.flock(self.fd, fcntl.LOCK_UN)
            except OSError as e:
                self.close()
                raise ModemUnavailable(f"modem AT port error: {e}") from e

    def cmd(self, command, timeout=3.0):
        """Send one command (inside session()) and return the raw response."""
        os.write(self.fd, (command + "\r").encode())
        return self._read_until(FINAL_RE, timeout)

    def cmd_with_text(self, command, text, timeout):
        """Commands that prompt with '>' for a body ended by Ctrl-Z (AT+CMGS)."""
        os.write(self.fd, (command + "\r").encode())
        got = self._read_until(PROMPT_RE, 5)
        if not re.search(r">\s?$", got):
            os.write(self.fd, b"\x1b")   # cancel a half-entered command
            return got
        os.write(self.fd, text.encode("ascii") + b"\x1a")
        return got + self._read_until(FINAL_RE, timeout)


# ----------------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------------

# 3GPP 27.007 <AcT> -> (label, icon suffix)
ACCESS_TECH = {
    0: ("2G", "gprs"), 1: ("2G", "gprs"), 3: ("2G", "edge"), 8: ("2G", "gprs"),
    2: ("3G", "umts"), 4: ("3G HSPA", "hspa"), 5: ("3G HSPA", "hspa"), 6: ("3G HSPA", "hspa"),
    7: ("LTE", "lte"), 9: ("LTE", "lte"), 10: ("LTE", "lte"),
}


def parse_xcesq(resp):
    """+XCESQ: <n>,<rxlev>,<ber>,<rscp>,<ecno>,<rsrq>,<rsrp>,<rssnr> -> values in dB/dBm.

    Scales follow 3GPP 27.007 +CESQ (same as ModemManager's XMM plugin);
    255/99 mean "not measured" and are left out.
    """
    m = re.search(r"\+XCESQ:\s*(-?\d+(?:\s*,\s*-?\d+)*)", resp)
    if not m:
        return {}
    v = [int(x) for x in re.findall(r"-?\d+", m.group(1))]
    if len(v) < 8:
        return {}
    _, rxlev, _, rscp, ecno, rsrq, rsrp, rssnr = v[:8]
    out = {}
    if 0 <= rsrp <= 97:
        out["rsrp"] = rsrp - 141
    if 0 <= rsrq <= 34:
        out["rsrq"] = rsrq / 2 - 20
    if -100 <= rssnr <= 100 and "rsrp" in out:
        out["sinr"] = rssnr / 2
    if 0 <= rscp <= 96:
        out["rscp"] = rscp - 121
    if 0 <= ecno <= 49:
        out["ecno"] = ecno / 2 - 24.5
    if 0 <= rxlev <= 63:
        out["rxlev"] = rxlev - 111
    return out


def signal_percent(sig):
    """0-100 from RSRP (LTE), RSCP (3G) or RxLev (2G); None when not measured."""
    for key, floor, span in (("rsrp", -120, 40), ("rscp", -115, 35), ("rxlev", -110, 40)):
        if key in sig:
            return max(0, min(100, round((sig[key] - floor) * 100 / span)))
    return None


def signal_bars(pct):
    return 0 if pct < 10 else 1 if pct < 35 else 2 if pct < 60 else 3 if pct < 85 else 4


def signal_text(sig):
    if "rsrp" in sig:
        parts = [f"RSRP {sig['rsrp']} dBm"]
        if "rsrq" in sig:
            parts.append(f"RSRQ {sig['rsrq']:.1f} dB")
        if "sinr" in sig:
            parts.append(f"SINR {sig['sinr']:.1f} dB")
        return ", ".join(parts)
    if "rscp" in sig:
        return f"RSCP {sig['rscp']} dBm" + (f", Ec/No {sig['ecno']:.1f} dB" if "ecno" in sig else "")
    if "rxlev" in sig:
        return f"RxLev {sig['rxlev']} dBm"
    return "no measurement"


def parse_cops(resp):
    m = re.search(r'\+COPS:\s*(\d)(?:,\d,"([^"]*)"(?:,(\d+))?)?', resp)
    if not m:
        return None
    return {"mode": int(m.group(1)), "operator": m.group(2) or "",
            "act": int(m.group(3)) if m.group(3) else None}


def parse_cpms_used(resp):
    m = re.search(r'\+CPMS:\s*"[^"]*",\s*(\d+),\s*(\d+)', resp)
    return int(m.group(1)) if m else None


def parse_cmgl(resp):
    """Text-mode +CMGL listing -> [{id, status, sender, timestamp, text}]."""
    body = resp.rsplit("\r\nOK\r\n", 1)[0]
    messages, current = [], None
    for line in body.split("\r\n"):
        m = re.match(r'^\+CMGL:\s*(\d+),"([^"]+)","([^"]*)",[^,]*(?:,"([^"]+)")?', line)
        if m:
            current = {"id": m.group(1), "status": m.group(2), "sender": m.group(3),
                       "timestamp": m.group(4) or "", "lines": []}
            messages.append(current)
        elif current is not None:
            current["lines"].append(line)
    for msg in messages:
        msg["text"] = "\n".join(msg.pop("lines")).strip()
    return messages


# ----------------------------------------------------------------------------
# Modem actions (used by the dialogs; each takes the port for its whole exchange)
# ----------------------------------------------------------------------------

def list_sms(at):
    with at.session() as s:
        s.cmd("AT+CMGF=1")
        return parse_cmgl(s.cmd('AT+CMGL="ALL"', 10))


def send_sms(at, number, text):
    number = re.sub(r"[\s()-]", "", number)
    if not PHONE_RE.match(number):
        return False, "Not a valid phone number (digits, optionally starting with +)."
    if any(ord(c) > 126 or (ord(c) < 32 and c not in "\n\r") for c in text):
        return False, "Only plain ASCII text can be sent for now."
    if len(text) > 160:
        return False, f"Messages are limited to 160 characters (this one has {len(text)})."
    with at.session() as s:
        s.cmd("AT+CMGF=1")
        resp = s.cmd_with_text(f'AT+CMGS="{number}"', text, 30)
    if "+CMGS:" in resp:
        return True, "SMS sent."
    return False, "The modem did not send it: " + " ".join(resp.split())


def delete_sms(at, msg_id):
    with at.session() as s:
        return "OK" in s.cmd(f"AT+CMGD={int(msg_id)}")


def query_ussd(at, code):
    if not USSD_RE.match(code):
        return "Not a valid USSD code (digits, * and #)."
    with at.session() as s:
        resp = s.cmd(f'AT+CUSD=1,"{code}",15', 10)
        if "+CUSD:" not in resp and "ERROR" not in resp:
            resp += s._read_until(r"\+CUSD:[^\r\n]*\r\n", 25)
        m = re.search(r'\+CUSD:\s*(\d)(?:,"([^"]*)"(?:,(\d+))?)?', resp)
        if m and m.group(1) == "1":
            s.cmd("AT+CUSD=2")   # close the menu session the network left open
    if not m:
        return "No answer from the network: " + (" ".join(resp.split()) or "(nothing)")
    text, dcs = m.group(2) or "", int(m.group(3) or 15)
    if dcs == 72 and re.fullmatch(r"(?:[0-9A-Fa-f]{4})+", text):
        with contextlib.suppress(ValueError):
            text = bytes.fromhex(text).decode("utf-16-be")
    return text or "(empty answer)"


def dial(at, number):
    number = re.sub(r"[\s()-]", "", number)
    if not PHONE_RE.match(number):
        return "Not a valid phone number."
    with at.session() as s:
        return " ".join(s.cmd(f"ATD{number};", 15).split())


def hangup(at):
    with at.session() as s:
        return " ".join(s.cmd("ATH").split())


# ----------------------------------------------------------------------------
# System state (no modem access needed)
# ----------------------------------------------------------------------------

def link_info():
    base = f"/sys/class/net/{IFACE}"
    try:
        with open(f"{base}/flags") as f:
            up = bool(int(f.read(), 16) & 0x1)
        with open(f"{base}/ifindex") as f:
            ifindex = int(f.read())
        with open(f"{base}/statistics/rx_bytes") as f:
            rx = int(f.read())
        with open(f"{base}/statistics/tx_bytes") as f:
            tx = int(f.read())
    except (OSError, ValueError):
        return None
    ip = None
    with contextlib.suppress(Exception):
        out = subprocess.run(["ip", "-4", "-o", "addr", "show", "dev", IFACE],
                             capture_output=True, text=True, timeout=2).stdout
        m = re.search(r"\binet (\d+\.\d+\.\d+\.\d+)", out)
        ip = m.group(1) if m else None
    return {"up": up, "ip": ip, "ifindex": ifindex, "rx": rx, "tx": tx}


def service_state():
    with contextlib.suppress(Exception):
        out = subprocess.run(
            ["systemctl", "show", SERVICE, "-p", "LoadState,ActiveState,SubState,Result,ExecMainStatus"],
            capture_output=True, text=True, timeout=3).stdout
        return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    return {}


def fmt_rate(bytes_per_s):
    bits = bytes_per_s * 8
    if bits >= 1e6:
        return f"{bits / 1e6:.1f} Mbit/s"
    if bits >= 1e3:
        return f"{bits / 1e3:.0f} kbit/s"
    return f"{bits:.0f} bit/s"


def fmt_bytes(n):
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.2f} GB"


def pick_icon(names):
    for name in names:
        if QIcon.hasThemeIcon(name):
            return QIcon.fromTheme(name)
    return QIcon.fromTheme(names[-1])


def terminal_command(cmd):
    for term, flag in (("konsole", "-e"), ("gnome-terminal", "--"), ("xfce4-terminal", "-x"),
                       ("x-terminal-emulator", "-e"), ("xterm", "-e")):
        if shutil.which(term):
            return [term, flag, cmd]
    return None


# ----------------------------------------------------------------------------
# Background modem poller
# ----------------------------------------------------------------------------

class ModemPoller(threading.Thread):
    """Queries the modem off the UI thread; the UI reads .snapshot."""

    def __init__(self, at):
        super().__init__(daemon=True)
        self.at = at
        self.snapshot = {}
        self.new_sms = queue.Queue()
        # Messages we notified about. Listing them marks them read on the modem,
        # so the inbox shows these as NEW until they are opened.
        self.notified_ids = set()
        self._wake = threading.Event()
        self._count_lock = threading.Lock()
        self._sms_count = None
        self._configured_generation = None

    def poll_soon(self):
        self._wake.set()

    def note_deleted(self):
        """The inbox deleted a message: lower the baseline, so a message that
        arrives before the next poll still shows up as an increase."""
        with self._count_lock:
            if self._sms_count:
                self._sms_count -= 1

    def run(self):
        while True:
            self.snapshot = self._poll()
            self._wake.wait(POLL_SECONDS)
            self._wake.clear()

    def _poll(self):
        snap = {}
        try:
            with self.at.session() as s:
                if self._configured_generation != self.at.generation:
                    s.cmd("AT+COPS=3,0")   # long operator names in AT+COPS? (no re-registration)
                    s.cmd("AT+CMGF=1")
                    self._configured_generation = self.at.generation
                    self._sms_count = None
                sim = re.search(r"\+CPIN:\s*([^\r\n]+)", s.cmd("AT+CPIN?"))
                snap["sim"] = sim.group(1).strip() if sim else "unknown"
                snap["cops"] = parse_cops(s.cmd("AT+COPS?"))
                snap["signal"] = parse_xcesq(s.cmd("AT+XCESQ?"))
                used = parse_cpms_used(s.cmd("AT+CPMS?"))
                if used is not None:
                    with self._count_lock:
                        grew = self._sms_count is not None and used > self._sms_count
                        self._sms_count = used
                    if grew:
                        s.cmd("AT+CMGF=1")
                        for msg in parse_cmgl(s.cmd('AT+CMGL="REC UNREAD"', 10)):
                            self.notified_ids.add(msg["id"])
                            self.new_sms.put(msg)
        except ModemUnavailable as e:
            snap["error"] = str(e)
        return snap


# ----------------------------------------------------------------------------
# Dialogs
# ----------------------------------------------------------------------------

class BackgroundDialog(QDialog):
    """Dialog whose modem work runs on a thread, so the tray never freezes while
    it waits for the modem (or for another program holding the AT port)."""

    def run_job(self, fn, args, done):
        self.setEnabled(False)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        job = {"result": None, "error": None}

        def work():
            try:
                job["result"] = fn(*args)
            except Exception as e:      # reported to the user in done()
                job["error"] = e

        thread = threading.Thread(target=work, daemon=True)
        timer = QTimer(self)

        def check():
            if thread.is_alive():
                return
            timer.stop()
            QApplication.restoreOverrideCursor()
            self.setEnabled(True)
            done(job["result"], job["error"])

        timer.timeout.connect(check)
        thread.start()
        timer.start(100)


class SMSInboxDialog(BackgroundDialog):
    def __init__(self, at, poller, parent=None):
        super().__init__(parent)
        self.at = at
        self.poller = poller
        self.messages = []
        self.setWindowTitle("Cellular SMS Messages")
        self.resize(700, 450)

        layout = QVBoxLayout(self)
        splitter = QSplitter(Qt.Horizontal)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        self.list_widget = QListWidget()
        self.list_widget.currentRowChanged.connect(self.display_message)
        left_layout.addWidget(QLabel("Messages:"))
        left_layout.addWidget(self.list_widget)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        self.sender_label = QLabel("<b>From:</b> -")
        self.date_label = QLabel("<b>Date:</b> -")
        self.body_view = QTextEdit()
        self.body_view.setReadOnly(True)
        right_layout.addWidget(self.sender_label)
        right_layout.addWidget(self.date_label)
        right_layout.addWidget(self.body_view)

        splitter.addWidget(left)
        splitter.addWidget(right)
        layout.addWidget(splitter)

        buttons = QHBoxLayout()
        for label, slot in (("Refresh", self.load_messages), ("Send SMS...", self.compose_sms),
                            ("Delete", self.delete_sms)):
            btn = QPushButton(label)
            btn.clicked.connect(slot)
            buttons.addWidget(btn)
        buttons.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        buttons.addWidget(close_btn)
        layout.addLayout(buttons)

        self.load_messages()

    def load_messages(self):
        self.list_widget.clear()
        self.body_view.setPlainText("Reading messages...")
        self.run_job(list_sms, (self.at,), self._show_messages)

    def _show_messages(self, messages, error):
        self.messages = messages or []
        if error:
            self.body_view.setPlainText(f"Modem not available: {error}")
            return
        if not self.messages:
            self.sender_label.setText("<b>From:</b> -")
            self.date_label.setText("<b>Date:</b> -")
            self.body_view.setPlainText("No messages.")
            return
        for msg in self.messages:
            preview = msg["text"].replace("\n", " ")
            preview = preview[:27] + "..." if len(preview) > 30 else preview
            new = "NEW · " if msg["id"] in self.poller.notified_ids else ""
            self.list_widget.addItem(f"{new}{msg['sender']} - {preview}")
        self.list_widget.setCurrentRow(0)

    def display_message(self, row):
        if 0 <= row < len(self.messages):
            msg = self.messages[row]
            self.poller.notified_ids.discard(msg["id"])
            self.sender_label.setText(f"<b>From:</b> {msg['sender']} ({msg['status']})")
            self.date_label.setText(f"<b>Date:</b> {msg['timestamp'] or '-'}")
            self.body_view.setPlainText(msg["text"])

    def compose_sms(self):
        number, ok = QInputDialog.getText(self, "Send SMS", "Recipient (international format, e.g. +44...):")
        if not ok or not number.strip():
            return
        text, ok = QInputDialog.getMultiLineText(self, "Send SMS", f"Message to {number.strip()} (max 160, plain text):")
        if not ok or not text.strip():
            return
        self.run_job(send_sms, (self.at, number, text.strip()), self._sent)

    def _sent(self, result, error):
        sent, info = result if not error else (False, f"Modem not available: {error}")
        if sent:
            QMessageBox.information(self, "SMS", info)
        else:
            QMessageBox.critical(self, "SMS not sent", info)

    def delete_sms(self):
        row = self.list_widget.currentRow()
        if not 0 <= row < len(self.messages):
            return
        msg = self.messages[row]
        if QMessageBox.question(self, "Delete SMS", f"Delete the message from {msg['sender']}?") != QMessageBox.Yes:
            return
        self.run_job(delete_sms, (self.at, msg["id"]), self._deleted)

    def _deleted(self, ok, error):
        if error:
            QMessageBox.critical(self, "Delete SMS", f"Modem not available: {error}")
        elif ok:
            self.poller.note_deleted()
        self.load_messages()


class CallDialog(BackgroundDialog):
    def __init__(self, at, parent=None):
        super().__init__(parent)
        self.at = at
        self.setWindowTitle("Cellular Phone & USSD")
        self.resize(450, 320)
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("<b>USSD code (balance, account info):</b>"))
        row = QHBoxLayout()
        self.ussd_input = QLineEdit()
        self.ussd_input.setPlaceholderText("e.g. *100#")
        ussd_btn = QPushButton("Send")
        ussd_btn.clicked.connect(self.query_ussd)
        row.addWidget(self.ussd_input)
        row.addWidget(ussd_btn)
        layout.addLayout(row)
        self.ussd_result = QTextEdit()
        self.ussd_result.setReadOnly(True)
        layout.addWidget(self.ussd_result)

        layout.addWidget(QLabel("<b>Dial:</b>"))
        row = QHBoxLayout()
        self.dial_input = QLineEdit()
        self.dial_input.setPlaceholderText("+<country code><number>")
        dial_btn = QPushButton("Dial")
        dial_btn.clicked.connect(self.dial)
        hangup_btn = QPushButton("Hang up")
        hangup_btn.clicked.connect(self.hangup)
        row.addWidget(self.dial_input)
        row.addWidget(dial_btn)
        row.addWidget(hangup_btn)
        layout.addLayout(row)
        note = QLabel("<small>Laptop WWAN slots carry no voice audio: a call rings, "
                      "but there is no sound. Use VoIP for voice.</small>")
        note.setStyleSheet("color: gray;")
        layout.addWidget(note)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)

    def _run(self, fn, args, show):
        self.run_job(fn, (self.at,) + args,
                     lambda result, error: show(f"Modem not available: {error}" if error else result))

    def query_ussd(self):
        code = self.ussd_input.text().strip()
        if code:
            self.ussd_result.setPlainText("Waiting for the network...")
            self._run(query_ussd, (code,), self.ussd_result.setPlainText)

    def dial(self):
        number = self.dial_input.text().strip()
        if number:
            self._run(dial, (number,), lambda text: QMessageBox.information(self, "Dial", text))

    def hangup(self):
        self._run(hangup, (), lambda text: QMessageBox.information(self, "Hang up", text))


# ----------------------------------------------------------------------------
# Tray
# ----------------------------------------------------------------------------

class CellularTrayApp:
    def __init__(self):
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)

        self.at = ATPort()
        self.poller = ModemPoller(self.at)
        self._prev = None            # (ifindex, time, rx, tx) for throughput
        self._warned_radio_off = False

        self.tray_icon = QSystemTrayIcon()
        self.menu = QMenu()

        self.status_action = self._info_item("Checking cellular status...")
        self.signal_action = self._info_item("")
        self.traffic_action = self._info_item("")
        self.menu.addSeparator()
        self.sms_action = self._action("SMS Messages...", self.open_sms_dialog)
        self.call_action = self._action("Phone & USSD...", self.open_call_dialog)
        self.menu.addSeparator()
        self.connect_action = self._action("Connect Cellular", self.connect_cellular)
        self.disconnect_action = self._action("Disconnect Cellular", self.disconnect_cellular)
        self.menu.addSeparator()
        self._action("Quit Indicator", self.app.quit)
        self.tray_icon.setContextMenu(self.menu)

        self.poller.start()
        self.timer = QTimer()
        self.timer.timeout.connect(self.refresh)
        self.timer.start(REFRESH_MS)
        self.refresh()
        self.tray_icon.show()

    def _info_item(self, text):
        action = QAction(text, self.menu)
        action.setEnabled(False)
        self.menu.addAction(action)
        return action

    def _action(self, text, slot):
        action = QAction(text, self.menu)
        action.triggered.connect(slot)
        self.menu.addAction(action)
        return action

    def _throughput(self, link):
        now = time.monotonic()
        rates = None
        if link and self._prev and self._prev[0] == link["ifindex"]:
            dt = now - self._prev[1]
            if dt > 0:
                rates = (max(0, link["rx"] - self._prev[2]) / dt,
                         max(0, link["tx"] - self._prev[3]) / dt)
        self._prev = (link["ifindex"], now, link["rx"], link["tx"]) if link else None
        return rates

    def refresh(self):
        link = link_info()
        svc = service_state()
        snap = self.poller.snapshot
        rates = self._throughput(link)

        connected = bool(link and link["up"] and link["ip"])
        cops = snap.get("cops") or {}
        sig = snap.get("signal") or {}
        pct = signal_percent(sig)
        tech, suffix = ACCESS_TECH.get(cops.get("act"), ("", ""))
        operator = cops.get("operator") or ""
        # Between failed attempts a Restart=on-failure unit is "activating" too,
        # with SubState=auto-restart; only a real run counts as connecting.
        retrying = svc.get("SubState") == "auto-restart"
        connecting = svc.get("ActiveState") == "activating" and not retrying
        radio_off = (not connected and not connecting
                     and svc.get("Result") == "exit-code" and svc.get("ExecMainStatus") == "3")

        # Status line and icon
        if connected:
            status = "Connected" + (f" · {tech}" if tech else "") + (f" · {operator}" if operator else "")
            if pct is not None:
                step = min(100, round(pct / 20) * 20)
                word = ("none", "weak", "ok", "good", "excellent")[signal_bars(pct)]
                names = [f"network-mobile-{step}-{suffix}", f"network-mobile-{step}",
                         f"network-cellular-signal-{word}-symbolic"]
            else:
                names = ["network-mobile-on", "network-cellular-connected-symbolic"]
        elif connecting:
            status = "Connecting..."
            names = ["network-mobile-available", "network-cellular-acquiring-symbolic"]
        elif radio_off:
            status = "Radio switched off at the modem (turn Cellular on in Windows)"
            names = ["network-mobile-off", "network-cellular-hardware-disabled-symbolic"]
        elif retrying:
            status = "Disconnected (automatic attempt failed, retrying)"
            names = ["network-mobile-off", "network-cellular-offline-symbolic"]
        elif svc.get("Result") not in (None, "", "success") and svc.get("LoadState") == "loaded":
            status = "Disconnected (last automatic attempt failed)"
            names = ["network-mobile-off", "network-cellular-offline-symbolic"]
        else:
            status = "Disconnected"
            names = ["network-mobile-off", "network-cellular-offline-symbolic"]
        names.append("network-cellular-symbolic")
        self.tray_icon.setIcon(pick_icon(names))
        self.status_action.setText(status)

        # Signal line
        if "error" in snap:
            signal_line = "Signal: unavailable (no access to the modem's AT port)"
        elif snap.get("sim") and snap["sim"] != "READY":
            signal_line = f"SIM: {snap['sim']}"
        elif pct is not None:
            signal_line = f"Signal {signal_bars(pct)}/4 ({pct}%): {signal_text(sig)}"
        elif snap:
            signal_line = "Signal: not registered to a network"
        else:
            signal_line = "Signal: checking..."
        self.signal_action.setText(signal_line)

        # Traffic line
        if connected and rates:
            traffic = (f"↓ {fmt_rate(rates[0])}  ↑ {fmt_rate(rates[1])}  ·  "
                       f"{fmt_bytes(link['rx'] + link['tx'])} used")
        elif connected:
            traffic = f"IP {link['ip']}"
        else:
            traffic = ""
        self.traffic_action.setText(traffic)
        self.traffic_action.setVisible(bool(traffic))

        tooltip = ["Cellular", status, signal_line]
        if connected:
            tooltip.append(f"IP {link['ip']}")
            if rates:
                tooltip.append(traffic)
        self.tray_icon.setToolTip("\n".join(tooltip))

        modem_ok = bool(snap) and "error" not in snap
        self.sms_action.setEnabled(modem_ok)
        self.call_action.setEnabled(modem_ok)
        self.connect_action.setEnabled(not connected and not connecting)
        self.disconnect_action.setEnabled(connected)

        # Notifications
        if radio_off and not self._warned_radio_off:
            self._warned_radio_off = True
            self.tray_icon.showMessage(
                "Cellular radio is switched off",
                "The modem's radio was switched off from Windows. Boot Windows, turn Cellular on, "
                "then boot Linux again.", QSystemTrayIcon.Warning, 15000)
        elif connected:
            self._warned_radio_off = False
        while True:
            try:
                msg = self.poller.new_sms.get_nowait()
            except queue.Empty:
                break
            preview = msg["text"].replace("\n", " ")
            self.tray_icon.showMessage(f"SMS from {msg['sender']}",
                                       preview[:117] + "..." if len(preview) > 120 else preview,
                                       QSystemTrayIcon.Information, 10000)

    def open_sms_dialog(self):
        SMSInboxDialog(self.at, self.poller).exec_()

    def open_call_dialog(self):
        CallDialog(self.at).exec_()

    def connect_cellular(self):
        if service_state().get("LoadState") == "loaded":
            cmd = ["pkexec", "/bin/sh", "-c",
                   f"systemctl reset-failed {SERVICE}; exec systemctl start --no-block {SERVICE}"]
        else:
            cmd = terminal_command(CONNECT_CMD)
            if cmd is None:
                self.tray_icon.showMessage("Cellular", f"Run {CONNECT_CMD} in a terminal.",
                                           QSystemTrayIcon.Information, 8000)
                return
        subprocess.Popen(cmd)
        self.poller.poll_soon()

    def disconnect_cellular(self):
        subprocess.Popen(["pkexec", "ip", "link", "set", "dev", IFACE, "down"])

    def run(self):
        sys.exit(self.app.exec_())


if __name__ == "__main__":
    CellularTrayApp().run()
