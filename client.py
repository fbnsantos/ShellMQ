#!/usr/bin/env python3
"""
mole — client (runs on your machine)
Connects to a remote device and opens an interactive bash shell over MQTT.

Usage:
  python client.py --device-id device-abc123 --broker mqtt.example.com
"""

import argparse
import json
import logging
import os
import select
import signal
import sys
import termios
import threading
import time
import tty
import uuid
from typing import Optional

import paho.mqtt.client as mqtt
try:
    from paho.mqtt.enums import CallbackAPIVersion
    _MQTT_V2 = True
except ImportError:
    _MQTT_V2 = False

log = logging.getLogger("mole-client")

# ── raw terminal ──────────────────────────────────────────────────────────────

class RawTerminal:
    """Switches the terminal to raw mode and restores it on exit."""

    def __init__(self, fd=sys.stdin.fileno()):
        self.fd = fd
        self.old_settings = None

    def __enter__(self):
        self.old_settings = termios.tcgetattr(self.fd)
        tty.setraw(self.fd)
        return self

    def __exit__(self, *args):
        if self.old_settings:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old_settings)


# ── client ────────────────────────────────────────────────────────────────────

class MoleClient:
    def __init__(self, device_id: str, broker: str, port: int,
                 username: Optional[str], password: Optional[str],
                 tls: bool, session_id: Optional[str]):
        self.device_id = device_id
        self.broker = broker
        self.port = port
        self.session_id = session_id or uuid.uuid4().hex[:8]
        self.connected_to_broker = threading.Event()
        self.session_ready = threading.Event()
        self._running = True

        if _MQTT_V2:
            self.client = mqtt.Client(
                callback_api_version=CallbackAPIVersion.VERSION2,
                client_id=f"mole-client-{self.session_id}",
            )
        else:
            self.client = mqtt.Client(client_id=f"mole-client-{self.session_id}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

        if username:
            self.client.username_pw_set(username, password)
        if tls:
            self.client.tls_set()

    def _topic_in(self):
        return f"shell/{self.device_id}/session/{self.session_id}/in"

    def _topic_out(self):
        return f"shell/{self.device_id}/session/{self.session_id}/out"

    def _topic_resize(self):
        return f"shell/{self.device_id}/session/{self.session_id}/resize"

    # ── MQTT callbacks ────────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc != 0:
            sys.stderr.write(f"\r\nmole: failed to connect to broker (rc={rc})\r\n")
            self._running = False
            return
        log.debug("Connected to broker")
        self.connected_to_broker.set()
        client.subscribe(self._topic_out(), qos=0)
        client.subscribe(f"shell/{self.device_id}/control/announce", qos=1)

    def _on_disconnect(self, client, userdata, rc, properties=None):
        if self._running:
            sys.stdout.buffer.write(b"\r\n[mole: disconnected from broker]\r\n")
            sys.stdout.buffer.flush()
            self._running = False

    def _on_message(self, client, userdata, msg):
        topic = msg.topic

        # shell output -> write directly to the local terminal
        if topic == self._topic_out():
            sys.stdout.buffer.write(msg.payload)
            sys.stdout.buffer.flush()
            return

        # session creation confirmed by server
        if topic == f"shell/{self.device_id}/control/announce":
            try:
                data = json.loads(msg.payload.decode())
                if data.get("session_id") == self.session_id:
                    self.session_ready.set()
            except Exception:
                pass

    # ── resize ────────────────────────────────────────────────────────────────

    def _send_resize(self):
        """Sends current terminal dimensions to the server."""
        try:
            import shutil
            size = shutil.get_terminal_size((80, 24))
            self.client.publish(
                self._topic_resize(),
                json.dumps({"rows": size.lines, "cols": size.columns}),
                qos=0,
            )
        except Exception as e:
            log.debug("Error sending resize: %s", e)

    def _setup_sigwinch(self):
        """Intercepts SIGWINCH to forward terminal resize events."""
        def handler(sig, frame):
            self._send_resize()
        signal.signal(signal.SIGWINCH, handler)

    # ── stdin loop ────────────────────────────────────────────────────────────

    def _stdin_loop(self):
        """Reads stdin in raw mode and publishes to MQTT."""
        stdin_fd = sys.stdin.fileno()
        while self._running:
            try:
                r, _, _ = select.select([stdin_fd], [], [], 0.05)
                if r:
                    data = os.read(stdin_fd, 256)
                    if not data:
                        break
                    # Ctrl+] to quit (same as telnet)
                    if b"\x1d" in data:
                        self._running = False
                        break
                    self.client.publish(self._topic_in(), data, qos=0)
            except (OSError, ValueError):
                break

        self._running = False

    # ── run ───────────────────────────────────────────────────────────────────

    def run(self):
        sys.stdout.write(f"mole: connecting to {self.broker}:{self.port} ...\r\n")
        sys.stdout.flush()

        self.client.connect(self.broker, self.port, keepalive=30)
        self.client.loop_start()

        # wait for broker connection
        if not self.connected_to_broker.wait(timeout=10):
            sys.stderr.write("mole: timed out connecting to broker\r\n")
            return 1

        sys.stdout.write(
            f"mole: connected. Requesting session {self.session_id} "
            f"on device {self.device_id} ...\r\n"
        )
        sys.stdout.flush()

        # small delay to let subscriptions settle before requesting the session
        time.sleep(0.5)

        # request a new session from the server
        self.client.publish(
            f"shell/{self.device_id}/control/new",
            json.dumps({"session_id": self.session_id}),
            qos=1,
        )

        # wait for session confirmation
        if not self.session_ready.wait(timeout=15):
            sys.stderr.write(
                f"mole: timed out. Is device '{self.device_id}' online?\r\n"
            )
            sys.stderr.write(
                f"  Check: mosquitto_sub -h {self.broker} "
                f"-t 'shell/{self.device_id}/presence'\r\n"
            )
            return 1

        sys.stdout.write(
            f"mole: session {self.session_id} active. "
            f"Press Ctrl+] to quit.\r\n\r\n"
        )
        sys.stdout.flush()

        self._setup_sigwinch()
        self._send_resize()

        with RawTerminal():
            self._stdin_loop()

        sys.stdout.write("\r\nmole: session closed.\r\n")
        self.client.loop_stop()
        self.client.disconnect()
        return 0


# ── device lister ─────────────────────────────────────────────────────────────

class DeviceLister:
    """Lists online devices by subscribing to presence topics."""

    def __init__(self, broker: str, port: int,
                 username: Optional[str], password: Optional[str], tls: bool):
        self.devices = {}
        if _MQTT_V2:
            self.client = mqtt.Client(
                callback_api_version=CallbackAPIVersion.VERSION2,
                client_id=f"mole-list-{uuid.uuid4().hex[:4]}",
            )
        else:
            self.client = mqtt.Client(client_id=f"mole-list-{uuid.uuid4().hex[:4]}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        if username:
            self.client.username_pw_set(username, password)
        if tls:
            self.client.tls_set()
        self.broker = broker
        self.port = port

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        client.subscribe("shell/+/presence", qos=1)

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode())
            device_id = data.get("device_id", "?")
            if data.get("online"):
                self.devices[device_id] = data
        except Exception:
            pass

    def list(self, timeout=3):
        self.client.connect(self.broker, self.port)
        self.client.loop_start()
        time.sleep(timeout)
        self.client.loop_stop()
        self.client.disconnect()
        return self.devices


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="mole client — access a remote bash shell over MQTT",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  # list online devices
  python client.py --broker localhost --list

  # connect to a device
  python client.py --broker localhost --device-id device-abc123

  # with authentication and TLS
  python client.py --broker mqtt.example.com --tls \\
                   --username user --password pass \\
                   --device-id device-abc123
        """,
    )
    parser.add_argument("--broker", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--username", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--tls", action="store_true")
    parser.add_argument("--device-id", default=None,
                        help="Remote device ID (required unless using --list)")
    parser.add_argument("--session-id", default=None,
                        help="Session ID (auto-generated if omitted)")
    parser.add_argument("--list", action="store_true",
                        help="List online devices and exit")
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG,
                            format="%(asctime)s [CLIENT] %(levelname)s %(message)s")

    if args.tls and args.port == 1883:
        args.port = 8883

    if args.list:
        print(f"Scanning for devices on {args.broker}:{args.port} ...")
        lister = DeviceLister(args.broker, args.port, args.username, args.password, args.tls)
        devices = lister.list()
        if not devices:
            print("No online devices found.")
        else:
            print(f"\n{'DEVICE ID':<25} {'SHELL':<15} {'SESSIONS':>8}  LAST SEEN")
            print("-" * 65)
            for did, info in devices.items():
                ts = time.strftime("%H:%M:%S", time.localtime(info.get("timestamp", 0)))
                print(f"{did:<25} {info.get('shell','?'):<15} "
                      f"{info.get('active_sessions',0):>8}  {ts}")
        return 0

    if not args.device_id:
        parser.error("--device-id is required (or use --list to discover devices)")

    client = MoleClient(
        device_id=args.device_id,
        broker=args.broker,
        port=args.port,
        username=args.username,
        password=args.password,
        tls=args.tls,
        session_id=args.session_id,
    )
    return client.run()


if __name__ == "__main__":
    sys.exit(main())
