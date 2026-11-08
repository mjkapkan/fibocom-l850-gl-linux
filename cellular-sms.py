#!/usr/bin/env python3
"""
Cellular SMS & USSD Tool for Fibocom L850-GL / Intel XMM7360
Supports listing, reading, sending, and deleting SMS, as well as executing USSD queries.
"""

import sys
import os
import time
import re
import argparse
import fcntl

# On the L850-GL, wwan0at1 answers AT commands; wwan0at0 is often silent.
DEFAULT_PORTS = ["/dev/wwan0at1", "/dev/wwan0at0"]


class ModemAT:
    def __init__(self, port=None, timeout=5):
        self.port = port
        self.timeout = timeout
        self.fd = None
        self._connect()

    def _connect(self):
        ports = [self.port] if self.port else DEFAULT_PORTS
        for p in ports:
            if not os.path.exists(p):
                continue
            try:
                self.fd = os.open(p, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
                self.port = p
                # Hold the port for this whole run, so the tray indicator's
                # polling cannot interleave with our commands.
                fcntl.flock(self.fd, fcntl.LOCK_EX)
                # Test with AT command
                if self._test_at():
                    return
                os.close(self.fd)
                self.fd = None
            except PermissionError:
                print(f"Permission denied accessing {p}.")
                print("Run with sudo or add your user to dialout: 'sudo usermod -aG dialout $USER'")
                sys.exit(1)
            except Exception:
                if self.fd:
                    os.close(self.fd)
                    self.fd = None

        if not self.fd:
            print("ERROR: Could not communicate with modem AT port.")
            print("Ensure the modem is connected and not currently stuck in sleep state.")
            sys.exit(1)

    def _test_at(self):
        try:
            self._write("AT\r\n")
            time.sleep(0.1)
            resp = self._read(timeout=1)
            return "OK" in resp
        except Exception:
            return False

    def _write(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8", errors="ignore")
        os.write(self.fd, data)

    def _read(self, timeout=None):
        timeout = timeout or self.timeout
        start = time.time()
        buf = b""
        while time.time() - start < timeout:
            try:
                chunk = os.read(self.fd, 4096)
                if chunk:
                    buf += chunk
                    text = buf.decode("utf-8", errors="ignore")
                    if "OK\r\n" in text or "ERROR\r\n" in text:
                        return text
            except BlockingIOError:
                time.sleep(0.05)
            except Exception:
                break
        return buf.decode("utf-8", errors="ignore")

    def send_cmd(self, cmd, timeout=None):
        self._write(f"{cmd}\r\n")
        time.sleep(0.1)
        return self._read(timeout=timeout)

    def close(self):
        if self.fd:
            os.close(self.fd)
            self.fd = None


def cmd_list(at, unread_only=False):
    at.send_cmd("AT+CMGF=1")  # Text mode
    status = '"REC UNREAD"' if unread_only else '"ALL"'
    resp = at.send_cmd(f"AT+CMGL={status}", timeout=8)

    lines = resp.splitlines()
    messages = []
    current_msg = None

    for line in lines:
        line = line.strip()
        m = re.match(r'^\+CMGL:\s*(\d+),"([^"]+)","([^"]+)",(?:.*,"([^"]+)")?', line)
        if m:
            if current_msg:
                messages.append(current_msg)
            current_msg = {
                "index": m.group(1),
                "status": m.group(2),
                "sender": m.group(3),
                "timestamp": m.group(4) or "Unknown",
                "text": ""
            }
        elif current_msg and not line.startswith("OK") and not line.startswith("+CMGL"):
            if current_msg["text"]:
                current_msg["text"] += "\n"
            current_msg["text"] += line

    if current_msg:
        messages.append(current_msg)

    if not messages:
        print("No SMS messages found in inbox.")
        return

    print("=" * 65)
    print(f"{'ID':<4} | {'From':<16} | {'Date/Time':<20} | Message")
    print("-" * 65)
    for msg in messages:
        preview = msg["text"].replace("\n", " ")
        if len(preview) > 30:
            preview = preview[:27] + "..."
        print(f"{msg['index']:<4} | {msg['sender']:<16} | {msg['timestamp']:<20} | {preview}")
    print("=" * 65)


def cmd_read(at, index):
    at.send_cmd("AT+CMGF=1")
    resp = at.send_cmd(f"AT+CMGR={index}", timeout=5)
    print(resp)


def cmd_send(at, phone_number, message_text):
    at.send_cmd("AT+CMGF=1")
    # Initiate send
    at._write(f'AT+CMGS="{phone_number}"\r\n')
    time.sleep(0.5)

    # Send message body followed by Ctrl+Z (\x1a)
    payload = f"{message_text}\x1a"
    at._write(payload)

    print(f"Sending SMS to {phone_number}...")
    resp = at._read(timeout=15)
    if "OK" in resp or "+CMGS:" in resp:
        print("SUCCESS: SMS sent successfully!")
    else:
        print("ERROR: Failed to send SMS.")
        print(resp)


def cmd_delete(at, index):
    resp = at.send_cmd(f"AT+CMGD={index}")
    if "OK" in resp:
        print(f"Message {index} deleted.")
    else:
        print(f"ERROR: Could not delete message {index}: {resp}")


def cmd_ussd(at, code):
    print(f"Sending USSD request '{code}' to carrier...")
    at.send_cmd("AT+CMGF=1")
    resp = at.send_cmd(f'AT+CUSD=1,"{code}",15', timeout=10)

    # Wait for unsolicited CUSD response
    time.sleep(1)
    extra = at._read(timeout=8)
    combined = resp + extra

    m = re.search(r'\+CUSD:\s*\d+,"([^"]+)"', combined)
    if m:
        print("=" * 45)
        print("Carrier USSD Response:")
        print(m.group(1))
        print("=" * 45)
    else:
        print(combined)


def cmd_dial(at, number):
    print(f"Dialing {number}...")
    print("NOTE: Laptop WWAN modules do not route voice audio to speakers/mic.")
    print("This will initiate the network cellular signaling/ring.")
    resp = at.send_cmd(f"ATD{number};")
    print(resp)


def cmd_hangup(at):
    print("Hanging up...")
    resp = at.send_cmd("ATH")
    print(resp)


def main():
    parser = argparse.ArgumentParser(description="Cellular SMS & USSD Manager for Fibocom L850-GL")
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # list
    list_p = subparsers.add_parser("list", help="List SMS messages in inbox")
    list_p.add_argument("-u", "--unread", action="store_true", help="Only show unread messages")

    # read
    read_p = subparsers.add_parser("read", help="Read a specific SMS by ID")
    read_p.add_argument("id", type=int, help="Message ID")

    # send
    send_p = subparsers.add_parser("send", help="Send an SMS")
    send_p.add_argument("number", help="Recipient phone number in international format (+<country code><number>)")
    send_p.add_argument("message", help="Message text")

    # delete
    del_p = subparsers.add_parser("delete", help="Delete an SMS by ID")
    del_p.add_argument("id", type=int, help="Message ID")

    # ussd
    ussd_p = subparsers.add_parser("ussd", help="Send USSD code (e.g. *100#)")
    ussd_p.add_argument("code", help="USSD code string")

    # dial / call
    dial_p = subparsers.add_parser("dial", help="Initiate outgoing dial")
    dial_p.add_argument("number", help="Phone number to dial")

    # hangup
    subparsers.add_parser("hangup", help="Hang up active call")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    at = ModemAT()
    try:
        if args.command == "list":
            cmd_list(at, unread_only=args.unread)
        elif args.command == "read":
            cmd_read(at, args.id)
        elif args.command == "send":
            cmd_send(at, args.number, args.message)
        elif args.command == "delete":
            cmd_delete(at, args.id)
        elif args.command == "ussd":
            cmd_ussd(at, args.code)
        elif args.command == "dial":
            cmd_dial(at, args.number)
        elif args.command == "hangup":
            cmd_hangup(at)
    finally:
        at.close()


if __name__ == "__main__":
    main()
