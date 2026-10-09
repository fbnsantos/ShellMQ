#!/usr/bin/env python3
"""
mole — server (runs on the remote device)
Exposes an interactive bash shell (with full PTY) over MQTT.

Topics:
  shell/<device_id>/session/<session_id>/in   <- input from client
  shell/<device_id>/session/<session_id>/out  -> output to client
  shell/<device_id>/control/new               <- client requests a new session
  shell/<device_id>/control/close             <- client closes a session
  shell/<device_id>/control/attach            <- client reattaches to an open session
  shell/<device_id>/session/<sid>/replay/<n>  -> recent output, answer to an attach
  shell/<device_id>/control/keepalive         <- attached clients, every 30 s
  shell/<device_id>/control/announce/<sid>    -> server confirms session created (retained,
                                                 cleared when the session closes)
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
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional

import paho.mqtt.client as mqtt
try:
    from paho.mqtt.enums import CallbackAPIVersion
    _MQTT_V2 = True
except ImportError:
    _MQTT_V2 = False

# ── logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [SERVER] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mole-server")

# recent output kept per session, replayed to clients that reattach
SCROLLBACK_BYTES = 64 * 1024
# attach reply ids become part of a topic: no wildcards or separators
_REPLY_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

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
    proc: Optional[subprocess.Popen] = None
    reader: Optional[threading.Thread] = None
    # last time a client typed, attached or sent a keepalive
    last_seen: float = field(default_factory=time.time)
    scrollback: bytearray = field(default_factory=bytearray)
    # held while appending + publishing output, so a replay snapshot never
    # misses or duplicates a chunk
    out_lock: threading.Lock = field(default_factory=threading.Lock)

    def topic_in(self):
        return f"shell/{self.device_id}/session/{self.session_id}/in"

    def topic_out(self):
        return f"shell/{self.device_id}/session/{self.session_id}/out"

    def topic_resize(self):
        return f"shell/{self.device_id}/session/{self.session_id}/resize"

    def topic_announce(self):
        return f"shell/{self.device_id}/control/announce/{self.session_id}"

    def topic_replay(self, reply_id: str):
        return f"shell/{self.device_id}/session/{self.session_id}/replay/{reply_id}"


# ── server ────────────────────────────────────────────────────────────────────

class MoleServer:
    def __init__(self, device_id: str, broker: str, port: int,
                 username: Optional[str], password: Optional[str],
                 tls: bool, shell: str, session_timeout: int):
        self.device_id = device_id
        self.session_timeout = session_timeout
        self.broker = broker
        self.port = port
        self.shell = shell
        self.sessions: Dict[str, Session] = {}
        self._lock = threading.Lock()

        if _MQTT_V2:
            self.client = mqtt.Client(
                callback_api_version=CallbackAPIVersion.VERSION2,
                client_id=f"mole-server-{device_id}",
            )
        else:
            self.client = mqtt.Client(client_id=f"mole-server-{device_id}")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

        if username and username.strip():
            self.client.username_pw_set(username, password or None)
        if tls:
            self.client.tls_set()

    # ── MQTT callbacks ────────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc != 0:
            log.error("Failed to connect to broker: rc=%d", rc)
            return
        log.info("Connected to broker %s:%d", self.broker, self.port)
        client.subscribe(f"shell/{self.device_id}/control/new", qos=1)
        client.subscribe(f"shell/{self.device_id}/control/close", qos=1)
        client.subscribe(f"shell/{self.device_id}/control/attach", qos=1)
        client.subscribe(f"shell/{self.device_id}/control/keepalive", qos=0)
        log.info("Listening on shell/%s/control/new", self.device_id)

        # clean session: subscriptions are lost on reconnect, restore them
        # for the sessions that are still open
        with self._lock:
            open_sessions = [s for s in self.sessions.values() if s.alive]
        for session in open_sessions:
            client.subscribe(session.topic_in(), qos=0)
            client.subscribe(session.topic_resize(), qos=0)
        if open_sessions:
            log.info("Resubscribed %d open session(s)", len(open_sessions))

        self._publish_presence()

    def _on_disconnect(self, client, userdata, rc, properties=None):
        if rc == 0:
            log.info("Disconnected from broker")
            return
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

        # client asked to close a session
        if topic == f"shell/{self.device_id}/control/close":
            try:
                session_id = json.loads(payload.decode()).get("session_id")
            except Exception as e:
                log.error("Error processing close request: %s", e)
                return
            with self._lock:
                session = self.sessions.get(session_id)
            if session:
                log.info("Close requested for session %s", session_id)
                self._close_session(session)
            return

        # client wants to reattach: send it the recent output
        if topic == f"shell/{self.device_id}/control/attach":
            try:
                data = json.loads(payload.decode())
                session_id = data.get("session_id")
                reply_id = str(data.get("reply_id", ""))
            except Exception as e:
                log.error("Error processing attach request: %s", e)
                return
            if not _REPLY_ID_RE.match(reply_id):
                log.warning("Attach request with invalid reply_id ignored")
                return
            with self._lock:
                session = self.sessions.get(session_id)
            if session and session.alive:
                log.info("Client attached to session %s", session_id)
                session.last_seen = time.time()
                with session.out_lock:
                    self.client.publish(session.topic_replay(reply_id),
                                        bytes(session.scrollback), qos=1)
            return

        # attached clients say they are still there
        if topic == f"shell/{self.device_id}/control/keepalive":
            try:
                ids = json.loads(payload.decode()).get("session_ids", [])
            except Exception:
                return
            now = time.time()
            with self._lock:
                for sid in ids if isinstance(ids, list) else []:
                    if sid in self.sessions:
                        self.sessions[sid].last_seen = now
            return

        # input or resize for an existing session
        for session in list(self.sessions.values()):
            if topic == session.topic_in():
                session.last_seen = time.time()
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
            log.debug("PTY opened: master=%d slave=%d", master_fd, slave_fd)

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
            log.debug("Shell launched: PID %d", proc.pid)

            session.pid = proc.pid
            session.proc = proc
            session.master_fd = master_fd
            self.sessions[session_id] = session

        # ── outside the lock: subscribe, publish, start thread ──

        # subscribe to this session's topics
        self.client.subscribe(session.topic_in(), qos=0)
        self.client.subscribe(session.topic_resize(), qos=0)

        # announce session to the client (retained so client always receives it)
        log.debug("Publishing announce for session %s", session_id)
        result = self.client.publish(
            session.topic_announce(),
            json.dumps({
                "session_id": session_id,
                "device_id": self.device_id,
                "shell": self.shell,
                "created_at": session.created_at,
            }),
            qos=1,
            retain=True,
        )
        log.debug("Announce publish rc=%s mid=%s", result.rc, result.mid)

        self._publish_presence()

        # start background thread to read PTY output
        t = threading.Thread(
            target=self._read_pty_loop,
            args=(session, proc),
            daemon=True,
            name=f"pty-read-{session_id}",
        )
        session.reader = t
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
                    self._publish_output(session, data)
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
                            self._publish_output(session, data)
                except OSError:
                    pass
                break

        self._finish_session(session)

    def _publish_output(self, session: Session, data: bytes):
        with session.out_lock:
            session.scrollback += data
            if len(session.scrollback) > SCROLLBACK_BYTES:
                del session.scrollback[:len(session.scrollback) - SCROLLBACK_BYTES]
            self.client.publish(session.topic_out(), data, qos=0)

    def _reap_idle_sessions(self):
        """Closes sessions no client has touched for session_timeout seconds
        (tab closed, client crashed...), so shells do not pile up."""
        while True:
            time.sleep(30)
            now = time.time()
            with self._lock:
                idle = [s for s in self.sessions.values()
                        if s.alive and now - s.last_seen > self.session_timeout]
            for session in idle:
                log.info("Session %s idle for more than %d s without clients, closing",
                         session.session_id, self.session_timeout)
                self._close_session(session)

    def _close_session(self, session: Session):
        """Asks a session to close. Safe to call from any thread; the PTY
        reader thread notices within 100 ms and does the actual cleanup."""
        with self._lock:
            if not session.alive:
                return
            session.alive = False
        # interactive bash ignores SIGTERM, SIGHUP makes it exit
        try:
            os.killpg(session.pid, signal.SIGHUP)
        except (OSError, ProcessLookupError):
            pass

    def _finish_session(self, session: Session):
        """Releases everything a session holds. Runs only on its reader thread,
        so the master fd is never closed while it is still being read."""
        with self._lock:
            session.alive = False
        log.info("Session %s closed", session.session_id)

        self.client.unsubscribe(session.topic_in())
        self.client.unsubscribe(session.topic_resize())

        self.client.publish(
            session.topic_out(),
            "\r\n[mole: session closed]\r\n".encode(),
            qos=1,
        )
        # clear the retained announce so it does not stay in the broker forever
        self.client.publish(session.topic_announce(), b"", qos=1, retain=True)

        # closing the master also hangs up the shell
        try:
            os.close(session.master_fd)
        except OSError:
            pass
        try:
            os.killpg(session.pid, signal.SIGHUP)
        except (OSError, ProcessLookupError):
            pass
        if session.proc is not None:
            try:
                session.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(session.pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
                session.proc.wait()

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

        if self.session_timeout > 0:
            threading.Thread(target=self._reap_idle_sessions, daemon=True,
                             name="idle-reaper").start()

        log.info("Connecting to broker %s:%d ...", self.broker, self.port)
        self.client.connect(self.broker, self.port, keepalive=60)

        # systemd stops the service with SIGTERM: shut down the same way as Ctrl+C
        def _on_sigterm(signum, frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, _on_sigterm)

        try:
            self.client.loop_forever()
        except KeyboardInterrupt:
            log.info("Shutting down...")
            # keep the network loop running so the close messages get out
            self.client.loop_start()
            with self._lock:
                open_sessions = list(self.sessions.values())
            for session in open_sessions:
                self._close_session(session)
            for session in open_sessions:
                if session.reader is not None:
                    session.reader.join(timeout=3)
            # a clean disconnect does not trigger the Last Will, mark offline ourselves
            info = self.client.publish(
                f"shell/{self.device_id}/presence",
                json.dumps({"device_id": self.device_id, "online": False}),
                qos=1,
                retain=True,
            )
            info.wait_for_publish(timeout=2)
            self.client.disconnect()
            self.client.loop_stop()


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
    parser.add_argument("--username", default=os.environ.get("MOLE_USERNAME") or None,
                        help="MQTT username (default: $MOLE_USERNAME)")
    parser.add_argument("--password", default=os.environ.get("MOLE_PASSWORD") or None,
                        help="MQTT password (default: $MOLE_PASSWORD; prefer the "
                             "variable, arguments are visible in ps)")
    parser.add_argument("--tls", action="store_true",
                        help="Enable TLS (default port changes to 8883)")
    parser.add_argument("--shell", default="/bin/bash",
                        help="Shell to expose")
    parser.add_argument("--session-timeout", type=int, default=600,
                        help="Close sessions with no attached client for this many "
                             "seconds (default: 600, 0 = never)")
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
        session_timeout=args.session_timeout,
    )
    server.run()


if __name__ == "__main__":
    main()
