#!/usr/bin/env python3
"""
mole — server (runs on the remote device)
Exposes an interactive bash shell (with full PTY) over MQTT.

Topics:
  shell/<device_id>/session/<session_id>/in   <- input from client
  shell/<device_id>/session/<session_id>/out  -> output to client
  shell/<device_id>/control/new               <- client requests a new session
  shell/<device_id>/control/announce          -> server confirms session created
  shell/<device_id>/presence                  -> device presence (retained)
"""

import argparse
import json
import logging
import os
import pty
import select
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional

import paho.mqtt.client as mqtt

# ── logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SERVER] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mole-server")

# ── session dataclass ─────────────────────────────────────────────────────────

@dataclass
class Session:
    session_id: str
    device_id: str
    shell: str
    created_at: float = field(default_factory=time.time)
    pid: int = 0
    master_fd: int = -1
    alive: bool = True

    def topic_in(self):
        return f"shell/{self.device_id}/session/{self.session_id}/in"

    def topic_out(self):
        return f"shell/{self.device_id}/session/{self.session_id}/out"

    def topic_resize(self):
        return f"shell/{self.device_id}/session/{self.session_id}/resize"


# ── server ────────────────────────────────────────────────────────────────────

class MoleServer:
    def __init__(self, device_id: str, broker: str, port: int,
                 username: Optional[str], password: Optional[str],
                 tls: bool, shell: str):
        self.device_id = device_id
        self.broker = broker
        self.port = port
        self.shell = shell
        self.sessions: Dict[str, Session] = {}
        self._lock = threading.Lock()

        self.client = mqtt.Client(client_id=f"mole-server-{device_id}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

        if username:
            self.client.username_pw_set(username, password)
        if tls:
            self.client.tls_set()

    # ── MQTT callbacks ────────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc):
        if rc != 0:
            log.error("Failed to connect to broker: rc=%d", rc)
            return
        log.info("Connected to broker %s:%d", self.broker, self.port)
        client.subscribe(f"shell/{self.device_id}/control/new", qos=1)
        log.info("Listening on shell/%s/control/new", self.device_id)
        self._publish_presence()

    def _on_disconnect(self, client, userdata, rc):
        log.warning("Disconnected from broker (rc=%d), reconnecting...", rc)

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        payload = msg.payload

        # new session requested by a client
        if topic == f"shell/{self.device_id}/control/new":
            try:
                data = json.loads(payload.decode())
                session_id = data.get("session_id", str(uuid.uuid4())[:8])
                self._create_session(session_id)
            except Exception as e:
                log.error("Error processing new session request: %s", e)
            return

        # input or resize for an existing session
        for session in list(self.sessions.values()):
            if topic == session.topic_in():
                self._write_to_pty(session, payload)
                return
            if topic == session.topic_resize():
                try:
                    data = json.loads(payload.decode())
                    self._resize_pty(session, data.get("rows", 24), data.get("cols", 80))
                except Exception as e:
                    log.error("Error resizing PTY: %s", e)
                return

    # ── session management ────────────────────────────────────────────────────

    def _create_session(self, session_id: str):
        with self._lock:
            if session_id in self.sessions:
                log.warning("Session %s already exists", session_id)
                return

            log.info("Creating session %s", session_id)

            session = Session(
                session_id=session_id,
                device_id=self.device_id,
                shell=self.shell,
            )

            # open a PTY pair
            master_fd, slave_fd = pty.openpty()

            # launch the shell attached to the PTY
            env = os.environ.copy()
            env["TERM"] = "xterm-256color"
            env["MOLE_SESSION_ID"] = session_id

            proc = subprocess.Popen(
                [self.shell],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
                env=env,
                preexec_fn=os.setsid,
            )
            os.close(slave_fd)

            session.pid = proc.pid
            session.master_fd = master_fd
            self.sessions[session_id] = session

            # subscribe to this session's topics
            self.client.subscribe(session.topic_in(), qos=0)
            self.client.subscribe(session.topic_resize(), qos=0)

            # announce session to the client
            self.client.publish(
                f"shell/{self.device_id}/control/announce",
                json.dumps({
                    "session_id": session_id,
                    "device_id": self.device_id,
                    "shell": self.shell,
                    "created_at": session.created_at,
                }),
                qos=1,
            )

            self._publish_presence()

            # start background thread to read PTY output
            t = threading.Thread(
                target=self._read_pty_loop,
                args=(session, proc),
                daemon=True,
                name=f"pty-read-{session_id}",
            )
            t.start()

            log.info("Session %s started (PID %d)", session_id, proc.pid)

    def _write_to_pty(self, session: Session, data: bytes):
        if not session.alive:
            return
        try:
            os.write(session.master_fd, data)
        except OSError as e:
            log.warning("Error writing to PTY (session %s): %s", session.session_id, e)
            self._close_session(session)

    def _resize_pty(self, session: Session, rows: int, cols: int):
        if not session.alive:
            return
        import fcntl, termios, struct
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(session.master_fd, termios.TIOCSWINSZ, winsize)
            os.killpg(os.getpgid(session.pid), signal.SIGWINCH)
            log.debug("Session %s resized to %dx%d", session.session_id, cols, rows)
        except Exception as e:
            log.warning("Error resizing PTY: %s", e)

    def _read_pty_loop(self, session: Session, proc: subprocess.Popen):
        """Reads output from the PTY master and publishes it over MQTT."""
        fd = session.master_fd
        topic_out = session.topic_out()

        while session.alive:
            try:
                r, _, _ = select.select([fd], [], [], 0.1)
                if r:
                    try:
                        data = os.read(fd, 4096)
                    except OSError:
                        break
                    if not data:
                        break
                    self.client.publish(topic_out, data, qos=0)
            except Exception as e:
                log.error("PTY read loop error: %s", e)
                break

            # check if the shell process has exited
            if proc.poll() is not None:
                time.sleep(0.1)
                try:
                    r, _, _ = select.select([fd], [], [], 0.2)
                    if r:
                        data = os.read(fd, 4096)
                        if data:
                            self.client.publish(topic_out, data, qos=0)
                except OSError:
                    pass
                break

        self._close_session(session)

    def _close_session(self, session: Session):
        if not session.alive:
            return
        session.alive = False
        log.info("Session %s closed", session.session_id)

        try:
            os.close(session.master_fd)
        except OSError:
            pass

        try:
            os.killpg(os.getpgid(session.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError):
            pass

        self.client.unsubscribe(session.topic_in())
        self.client.unsubscribe(session.topic_resize())

        self.client.publish(
            session.topic_out(),
            "\r\n[mole: session closed]\r\n".encode(),
            qos=1,
        )

        with self._lock:
            self.sessions.pop(session.session_id, None)

        self._publish_presence()

    # ── presence ──────────────────────────────────────────────────────────────

    def _publish_presence(self):
        with self._lock:
            sessions_info = [
                {"session_id": s.session_id, "created_at": s.created_at}
                for s in self.sessions.values()
                if s.alive
            ]
        payload = json.dumps({
            "device_id": self.device_id,
            "online": True,
            "shell": self.shell,
            "active_sessions": len(sessions_info),
            "sessions": sessions_info,
            "timestamp": time.time(),
        })
        self.client.publish(
            f"shell/{self.device_id}/presence",
            payload,
            qos=1,
            retain=True,
        )

    # ── startup ───────────────────────────────────────────────────────────────

    def run(self):
        # Last Will: marks device offline if it disconnects unexpectedly
        self.client.will_set(
            f"shell/{self.device_id}/presence",
            json.dumps({"device_id": self.device_id, "online": False}),
            qos=1,
            retain=True,
        )

        log.info("Connecting to broker %s:%d ...", self.broker, self.port)
        self.client.connect(self.broker, self.port, keepalive=60)

        try:
            self.client.loop_forever()
        except KeyboardInterrupt:
            log.info("Shutting down...")
            with self._lock:
                for session in list(self.sessions.values()):
                    self._close_session(session)
            self.client.disconnect()


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="mole server — exposes a bash shell over MQTT"
    )
    parser.add_argument("--device-id", default=f"device-{uuid.uuid4().hex[:6]}",
                        help="Unique device identifier")
    parser.add_argument("--broker", default="localhost",
                        help="MQTT broker address")
    parser.add_argument("--port", type=int, default=1883,
                        help="MQTT broker port")
    parser.add_argument("--username", default=None,
                        help="MQTT username")
    parser.add_argument("--password", default=None,
                        help="MQTT password")
    parser.add_argument("--tls", action="store_true",
                        help="Enable TLS (default port changes to 8883)")
    parser.add_argument("--shell", default="/bin/bash",
                        help="Shell to expose")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug logging")
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.tls and args.port == 1883:
        args.port = 8883

    log.info("Device ID: %s", args.device_id)

    server = MoleServer(
        device_id=args.device_id,
        broker=args.broker,
        port=args.port,
        username=args.username,
        password=args.password,
        tls=args.tls,
        shell=args.shell,
    )
    server.run()


if __name__ == "__main__":
    main()
