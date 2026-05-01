# mole

Interactive bash shell (with full PTY) over MQTT. Works behind NAT/firewalls —
the device only needs outbound access to the broker.

```
You (client)          MQTT Broker           Remote device
────────────          ───────────           ─────────────
terminal / browser <-> mosquitto <-> server.py -> /bin/bash (PTY)
```

## Files

| File               | Description                                           |
|--------------------|-------------------------------------------------------|
| `server.py`        | Runs on the remote device; exposes bash over MQTT     |
| `client.py`        | Interactive CLI client (raw terminal with PTY resize) |
| `web_client.py`    | Minimal web server with an xterm.js browser terminal  |
| `install.sh`       | Installer — sets up a systemd service on Linux        |
| `requirements.txt` | Python dependencies                                   |

---

## Server installation (Linux)

The installer copies `server.py` to `/opt/mole`, creates an isolated Python
virtual environment, and registers a systemd service that starts on boot.

Supported: **Ubuntu**, **Zorin OS**, **Raspberry Pi OS (Raspbian)** and any
other Debian-based system with systemd.

### Prerequisites

```bash
# Ubuntu / Zorin OS / Raspberry Pi OS
sudo apt update
sudo apt install -y python3 python3-pip python3-venv
```

### Install

```bash
# basic install
sudo bash install.sh --broker BROKER_IP --device-id my-device

# with MQTT authentication
sudo bash install.sh \
  --broker mqtt.example.com \
  --device-id my-device \
  --username myuser \
  --password 'mypassword'

# with TLS
sudo bash install.sh \
  --broker mqtt.example.com \
  --tls \
  --device-id my-device \
  --username myuser \
  --password 'mypassword'
```

> **Note:** if your password contains `$`, `!`, or `#`, always wrap it in
> single quotes: `--password '$ecret#2025!'`

### Installer options

| Option          | Default       | Description                     |
|-----------------|---------------|---------------------------------|
| `--broker`      | `localhost`   | MQTT broker address             |
| `--port`        | `1883`        | MQTT broker port                |
| `--device-id`   | hostname      | Unique device identifier        |
| `--username`    | *(none)*      | MQTT username                   |
| `--password`    | *(none)*      | MQTT password                   |
| `--shell`       | `/bin/bash`   | Shell to expose                 |
| `--tls`         | off           | Enable TLS                      |
| `--user`        | current user  | System user the service runs as |
| `--install-dir` | `/opt/mole`   | Installation directory          |

### Managing the service

```bash
systemctl status mole          # check status
journalctl -u mole -f          # follow logs
sudo systemctl restart mole    # restart
sudo systemctl stop mole       # stop
sudo systemctl disable mole    # disable autostart
sudo bash install.sh --uninstall  # remove completely
```

### Updating configuration

Re-run `install.sh` with the new options — it overwrites the unit and restarts
the service automatically.

### Platform notes

**Ubuntu 22.04 / 24.04 and Zorin OS 16 / 17**

Ships with Python 3.10+ and systemd. Everything works out of the box.
If `pip3` is missing: `sudo apt install python3-pip`.

**Raspberry Pi OS (Raspbian) — 32-bit and 64-bit**

Works on Pi 3, 4, and 5. PTY and SIGWINCH resize are fully supported.
On older Buster (Python 3.7), upgrade Python first:

```bash
sudo apt install -y python3.9 python3.9-venv
sudo update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.9 1
```

On Pi Zero / Zero 2 W, the service may take a few extra seconds on boot while
the network comes up — this is normal, the service retries automatically.

---

## Quick start without installer

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install paho-mqtt
python3 server.py --broker BROKER_IP --device-id my-device
```

---

## Broker setup (Mosquitto)

```bash
# Ubuntu / Zorin / Raspbian
sudo apt install -y mosquitto mosquitto-clients

# macOS
brew install mosquitto
```

Append to `/etc/mosquitto/mosquitto.conf`:

```
listener 1883
listener 9001
protocol websockets
allow_anonymous true
```

```bash
sudo systemctl restart mosquitto
```

---

## Client usage

### CLI client

```bash
# list online devices
python3 client.py --broker BROKER_IP --list

# connect
python3 client.py --broker BROKER_IP --device-id my-device
```

**Key bindings:**
- `Ctrl+]` — quit session
- `Ctrl+C` — sent to the remote process

### Web client

```bash
pip install aiohttp
python3 web_client.py --port 8080
# open http://localhost:8080
```

Enter broker address (WebSocket port, usually 9001), click **Connect**,
select a device, click **New Session**.

---

## MQTT topics

| Topic | Direction | Description |
|-------|-----------|-------------|
| `shell/<device>/control/new` | client→server | Request a new session |
| `shell/<device>/control/announce/<session>` | server→client | Session confirmed (retained) |
| `shell/<device>/presence` | server→all | Device presence (retained) |
| `shell/<device>/session/<id>/in` | client→server | User input |
| `shell/<device>/session/<id>/out` | server→client | Shell output |
| `shell/<device>/session/<id>/resize` | client→server | Terminal resize |

## Multiple sessions

Each session has a random ID. Multiple clients can connect to the same device simultaneously.

## Security

In production always use:

1. **TLS on the broker** (port 8883 for MQTT, 9883 for WSS)
2. **Authentication** via username/password or client certificates
3. **ACLs on the broker** to restrict who can subscribe to `shell/#`

Example `mosquitto.conf` with TLS:

```
listener 8883
cafile /etc/mosquitto/ca.crt
certfile /etc/mosquitto/server.crt
keyfile /etc/mosquitto/server.key
require_certificate true

listener 9883
protocol websockets
cafile /etc/mosquitto/ca.crt
certfile /etc/mosquitto/server.crt
keyfile /etc/mosquitto/server.key
```

Free public broker for testing: `broker.hivemq.com:1883` / WS port: `8000`

## Interactive programs (vim, htop, etc.)

These work correctly because a real PTY is used (`pty.openpty()`).
Terminal resize (SIGWINCH) is forwarded automatically.
