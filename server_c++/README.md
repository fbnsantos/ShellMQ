# mole

Interactive bash shell (with full PTY) over MQTT. Works behind NAT/firewalls —
the device only needs outbound access to the broker.

```
You (client)          MQTT Broker           Remote device
────────────          ───────────           ─────────────
terminal / browser <-> mosquitto <-> server (C++ or Python) -> /bin/bash (PTY)
```

---

## Repository layout

### Python implementation (recommended for quick start)

| File               | Description                                           |
|--------------------|-------------------------------------------------------|
| `server.py`        | Server — runs on the remote device                    |
| `client.py`        | CLI client — interactive terminal on your machine     |
| `web_client.py`    | Web client — xterm.js terminal in the browser         |
| `install.sh`       | Installer — systemd service (Python)                  |
| `requirements.txt` | Python dependencies (`paho-mqtt`, `aiohttp`)          |

### C++ implementation (recommended for embedded / Pi Zero)

| File              | Description                                            |
|-------------------|--------------------------------------------------------|
| `mole-cpp/server.cpp`     | Server — single C++ source file               |
| `mole-cpp/CMakeLists.txt` | CMake build system                            |
| `mole-cpp/install.sh`     | Installer — builds from source + systemd      |

The **client** (`client.py` and `web_client.py`) is always Python — it runs on
your machine where Python is trivial to install. Only the **server** has a C++
alternative.

---

## Server: Python vs C++

| | Python server | C++ server |
|---|---|---|
| **Dependencies** | Python 3, paho-mqtt (pip) | libpaho-mqttpp, cmake |
| **Binary size** | ~30 MB (venv) | ~2 MB |
| **RAM idle** | ~30 MB | ~5 MB |
| **Startup time** | ~1 s | <100 ms |
| **Best for** | Any Linux with Python | Pi Zero, embedded, containers |
| **Client compatibility** | ✓ | ✓ (identical MQTT topics) |

---

## Python server — installation

### Prerequisites

```bash
# Ubuntu / Zorin OS / Raspberry Pi OS
sudo apt update
sudo apt install -y python3 python3-pip python3-venv
```

### Install as a systemd service

```bash
# basic
sudo bash install.sh --broker BROKER_IP --device-id my-device

# with authentication
sudo bash install.sh \
  --broker mqtt.example.com \
  --device-id my-device \
  --username myuser \
  --password 'mypassword'

# with TLS
sudo bash install.sh \
  --broker mqtt.example.com --tls \
  --device-id my-device \
  --username myuser \
  --password 'mypassword'
```

> **Note:** if your password contains `$`, `!`, or `#`, wrap it in single
> quotes: `--password '$ecret#2025!'`

### Run manually (without installer)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install paho-mqtt
python3 server.py --broker BROKER_IP --device-id my-device
```

### Platform notes

**Ubuntu 22.04 / 24.04 and Zorin OS 16 / 17**
Ships with Python 3.10+ and systemd. Works out of the box.

**Raspberry Pi OS — 32-bit and 64-bit**
Works on Pi 3, 4, 5. On older Buster (Python 3.7):
```bash
sudo apt install -y python3.9 python3.9-venv
sudo update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.9 1
```
On Pi Zero / Zero 2 W the service retries automatically while the network comes up.

---

## C++ server — installation

### Prerequisites

```bash
# Ubuntu / Zorin OS / Raspberry Pi OS
sudo apt update
sudo apt install -y \
    build-essential cmake libssl-dev uuid-dev \
    libpaho-mqtt-dev libpaho-mqttpp-dev \
    nlohmann-json3-dev
```

### Install as a systemd service

```bash
cd mole-cpp
sudo bash install.sh \
  --broker mqtt.example.com \
  --device-id my-device \
  --username myuser \
  --password 'mypassword'
```

The installer compiles the binary, copies it to `/opt/mole/bin/mole-server`,
and registers a systemd service.

### Build manually

```bash
cd mole-cpp
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build --parallel $(nproc)

# run
./build/mole-server --broker BROKER_IP --device-id my-device
```

### Cross-compile for Raspberry Pi (from x86 machine)

```bash
sudo apt install -y gcc-aarch64-linux-gnu g++-aarch64-linux-gnu

cmake -S . -B build-arm \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_TOOLCHAIN_FILE=arm64-toolchain.cmake

cmake --build build-arm --parallel $(nproc)
# copy ./build-arm/mole-server to the Pi
```

---

## Installer options (both Python and C++)

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
| `--uninstall`   | —             | Remove the service              |

---

## Managing the service

```bash
systemctl status mole             # check status
journalctl -u mole -f             # follow logs in real time
sudo systemctl restart mole       # restart
sudo systemctl stop mole          # stop
sudo systemctl disable mole       # disable autostart
sudo bash install.sh --uninstall  # remove completely
```

Credentials are stored in `/opt/mole/mole.env` (permissions 600).
Re-run `install.sh` with new options to update configuration.

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

Enter the broker address (WebSocket port, usually 9001), click **Connect**,
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
Both the Python and C++ servers support multiple concurrent sessions.

---

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

---

## Interactive programs (vim, htop, tmux, etc.)

Both servers use a real PTY (`forkpty()` in C++, `pty.openpty()` in Python).
Terminal resize (SIGWINCH) is forwarded automatically.
Programs like `vim`, `htop`, `mc`, and `tmux` work correctly.
