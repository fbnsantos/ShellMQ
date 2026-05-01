# mole

Interactive bash shell (with full PTY) over MQTT. Works behind NAT/firewalls —
the device only needs outbound access to the broker.

```
You (client)          MQTT Broker           Remote device
────────────          ───────────           ─────────────
terminal / browser <-> mosquitto <-> server.py -> /bin/bash (PTY)
```

## Files

| File             | Description                                             |
|------------------|---------------------------------------------------------|
| `server.py`      | Runs on the remote device; exposes bash over MQTT       |
| `client.py`      | Interactive CLI client (raw terminal with PTY resize)   |
| `web_client.py`  | Minimal web server with an xterm.js browser terminal   |
| `requirements.txt` | Python dependencies                                   |

## Installation

```bash
pip install -r requirements.txt
# For the web client also:
pip install aiohttp
```

## Quick start (local broker)

### 1. Mosquitto broker with WebSockets

```bash
# install
sudo apt install mosquitto   # or: brew install mosquitto

# /etc/mosquitto/mosquitto.conf (append):
listener 1883
listener 9001
protocol websockets
allow_anonymous true
```

```bash
sudo systemctl restart mosquitto
```

### 2. Server (on the remote device)

```bash
python server.py --broker BROKER_IP --device-id my-pi
```

Options:
```
--broker      MQTT broker address          (default: localhost)
--port        MQTT port                    (default: 1883)
--device-id   Unique device name           (auto-generated if omitted)
--shell       Shell to expose              (default: /bin/bash)
--username    MQTT username
--password    MQTT password
--tls         Enable TLS (port changes to 8883)
--debug       Verbose logging
```

### 3a. CLI client (on your machine)

```bash
# list online devices
python client.py --broker BROKER_IP --list

# connect
python client.py --broker BROKER_IP --device-id my-pi
```

**Key bindings:**
- `Ctrl+]` — quit session (same as telnet)
- `Ctrl+C` — sent to the remote process (does not kill the client)

### 3b. Web client (browser)

```bash
python web_client.py --port 8080
# open http://localhost:8080
```

In the browser: enter the broker address (WebSocket port, usually 9001),
click "Connect", select a device, click "New Session".

## MQTT topics

| Topic | Direction | Description |
|-------|-----------|-------------|
| `shell/<device>/control/new` | client→server | Request a new session |
| `shell/<device>/control/announce` | server→client | Session created confirmation |
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

Free public broker for testing (no guarantees): `broker.hivemq.com:1883` / WS: `8000`

## Interactive programs (vim, htop, etc.)

These work correctly because a real PTY is used (`pty.openpty()`).
Terminal resize (SIGWINCH) is also forwarded automatically.
