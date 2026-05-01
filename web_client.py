#!/usr/bin/env python3
"""
mqtt-shell — cliente web
Serve um terminal web (xterm.js) que comunica com o servidor via MQTT sobre WebSockets.

Requer: pip install paho-mqtt websockets aiohttp
"""

import argparse
import asyncio
import json
import logging
import os
import uuid
from pathlib import Path

from aiohttp import web

log = logging.getLogger("mqtt-shell-web")

HTML = r"""<!DOCTYPE html>
<html lang="pt">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>mqtt-shell</title>
<!-- xterm.js 5.3.0 -->
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.min.css">
<script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.min.js"></script>
<!-- mqtt.js -->
<script src="https://cdn.jsdelivr.net/npm/mqtt@5.3.4/dist/mqtt.min.js"></script>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg: #0d0f14;
    --surface: #141720;
    --border: #1e2433;
    --accent: #4af2a1;
    --accent2: #2dd4bf;
    --muted: #4a5568;
    --text: #e2e8f0;
    --text-dim: #718096;
    --red: #fc8181;
    --yellow: #f6e05e;
  }

  html, body {
    height: 100%;
    background: var(--bg);
    color: var(--text);
    font-family: 'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace;
    overflow: hidden;
  }

  #app {
    display: grid;
    grid-template-rows: 48px 1fr;
    height: 100vh;
  }

  /* ── header ── */
  header {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 0 20px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    position: relative;
    z-index: 10;
  }

  .logo {
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 0.08em;
    color: var(--accent);
    text-transform: uppercase;
    white-space: nowrap;
  }

  .logo span { color: var(--text-dim); }

  .spacer { flex: 1; }

  .status-pill {
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 11px;
    color: var(--text-dim);
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 20px;
    padding: 3px 10px;
    transition: border-color 0.3s;
  }

  .status-pill.connected { border-color: var(--accent); color: var(--accent); }
  .status-pill.error     { border-color: var(--red);    color: var(--red);    }

  .dot {
    width: 6px; height: 6px;
    border-radius: 50%;
    background: currentColor;
  }

  .dot.blink { animation: blink 1.2s infinite; }

  @keyframes blink {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.2; }
  }

  /* ── main area ── */
  main {
    display: grid;
    grid-template-columns: 260px 1fr;
    overflow: hidden;
  }

  /* ── sidebar ── */
  aside {
    background: var(--surface);
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    overflow: hidden;
  }

  .sidebar-section {
    padding: 12px 16px 8px;
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.12em;
    color: var(--muted);
    text-transform: uppercase;
    border-bottom: 1px solid var(--border);
  }

  .connect-form {
    padding: 14px 16px;
    display: flex;
    flex-direction: column;
    gap: 8px;
    border-bottom: 1px solid var(--border);
  }

  .field-label {
    font-size: 10px;
    color: var(--text-dim);
    margin-bottom: 3px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }

  input[type="text"], input[type="password"], input[type="number"] {
    width: 100%;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 6px 9px;
    font-family: inherit;
    font-size: 12px;
    color: var(--text);
    outline: none;
    transition: border-color 0.2s;
  }

  input:focus { border-color: var(--accent2); }

  .btn {
    width: 100%;
    padding: 8px;
    border-radius: 4px;
    border: none;
    font-family: inherit;
    font-size: 12px;
    font-weight: 700;
    cursor: pointer;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    transition: opacity 0.2s, transform 0.1s;
  }

  .btn:active { transform: scale(0.97); }

  .btn-primary {
    background: var(--accent);
    color: #0d0f14;
  }

  .btn-primary:disabled { opacity: 0.4; cursor: default; }

  .btn-danger {
    background: transparent;
    color: var(--red);
    border: 1px solid var(--red);
    margin-top: 4px;
  }

  .devices-list {
    flex: 1;
    overflow-y: auto;
    padding: 8px 0;
  }

  .device-item {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 16px;
    cursor: pointer;
    border-left: 3px solid transparent;
    transition: background 0.15s, border-color 0.15s;
    font-size: 12px;
  }

  .device-item:hover { background: rgba(74,242,161,0.05); }

  .device-item.selected {
    background: rgba(74,242,161,0.08);
    border-left-color: var(--accent);
  }

  .device-name { color: var(--text); }
  .device-meta { font-size: 10px; color: var(--text-dim); }
  .device-dot  { width: 6px; height: 6px; border-radius: 50%; background: var(--accent); flex-shrink: 0; }

  .no-devices {
    padding: 20px 16px;
    font-size: 12px;
    color: var(--text-dim);
    text-align: center;
  }

  /* ── terminal pane ── */
  .terminal-pane {
    display: flex;
    flex-direction: column;
    overflow: hidden;
    position: relative;
  }

  .terminal-topbar {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 6px 14px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    font-size: 11px;
    color: var(--text-dim);
  }

  .session-badge {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 3px;
    padding: 1px 7px;
    font-size: 11px;
    font-family: inherit;
    color: var(--accent2);
  }

  #terminal-container {
    flex: 1;
    padding: 4px;
    overflow: hidden;
  }

  .splash {
    position: absolute;
    inset: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 12px;
    color: var(--text-dim);
    font-size: 13px;
    pointer-events: none;
  }

  .splash-logo {
    font-size: 32px;
    font-weight: 900;
    color: var(--accent);
    letter-spacing: -0.02em;
  }

  .splash-logo span { color: var(--text-dim); }

  .log-panel {
    max-height: 90px;
    overflow-y: auto;
    border-top: 1px solid var(--border);
    background: var(--bg);
    padding: 6px 14px;
    font-size: 10px;
    color: var(--text-dim);
    font-family: inherit;
  }

  .log-line { line-height: 1.6; }
  .log-line.ok     { color: var(--accent); }
  .log-line.err    { color: var(--red);    }
  .log-line.warn   { color: var(--yellow); }
</style>
</head>
<body>
<div id="app">
  <header>
    <div class="logo">mqtt<span>·</span>shell</div>
    <div class="spacer"></div>
    <div class="status-pill" id="broker-status">
      <div class="dot blink"></div>
      <span id="broker-status-text">disconnected</span>
    </div>
  </header>

  <main>
    <aside>
      <div class="sidebar-section">Broker MQTT</div>
      <div class="connect-form">
        <div>
          <div class="field-label">Address</div>
          <input type="text" id="broker-host" value="localhost" placeholder="broker.exemplo.com">
        </div>
        <div>
          <div class="field-label">Porta WebSocket</div>
          <input type="number" id="broker-port" value="9001" placeholder="9001">
        </div>
        <div>
          <div class="field-label">Username (opcional)</div>
          <input type="text" id="broker-user" placeholder="">
        </div>
        <div>
          <div class="field-label">Password (opcional)</div>
          <input type="password" id="broker-pass" placeholder="">
        </div>
        <button class="btn btn-primary" id="btn-connect">Connect</button>
        <button class="btn btn-danger" id="btn-disconnect" style="display:none">Disconnect</button>
      </div>

      <div class="sidebar-section">Dispositivos</div>
      <div class="devices-list" id="devices-list">
        <div class="no-devices">Liga ao broker para ver dispositivos</div>
      </div>
    </aside>

    <div class="terminal-pane">
      <div class="terminal-topbar">
        <span>session:</span>
        <span class="session-badge" id="session-label">—</span>
        <span style="flex:1"></span>
        <button class="btn btn-primary" id="btn-open-session"
                style="width:auto;padding:3px 12px;display:none">
          New Session
        </button>
        <button class="btn btn-primary" id="btn-test-input"
                style="width:auto;padding:3px 12px;display:none;margin-left:6px;background:var(--accent2)">
          Test
        </button>
      </div>
      <div id="terminal-container">
        <div class="splash" id="splash">
          <div class="splash-logo">mqtt<span>://</span>shell</div>
          <div>Connect to a broker and select a device</div>
        </div>
      </div>
      <div class="log-panel" id="log-panel"></div>
    </div>
  </main>
</div>

<script>
// ── estado global ────────────────────────────────────────────────────────────
let mqttClient = null;
let term = null;
let fitAddon = null;
let selectedDevice = null;
let sessionId = null;
let sessionActive = false;
const devices = {};

// ── logging ──────────────────────────────────────────────────────────────────
function logLine(msg, type = '') {
  const panel = document.getElementById('log-panel');
  const d = document.createElement('div');
  d.className = `log-line ${type}`;
  const ts = new Date().toLocaleTimeString('pt-PT', { hour12: false });
  d.textContent = `[${ts}] ${msg}`;
  panel.appendChild(d);
  panel.scrollTop = panel.scrollHeight;
}

// ── broker status pill ───────────────────────────────────────────────────────
function setBrokerStatus(state, text) {
  const pill = document.getElementById('broker-status');
  const label = document.getElementById('broker-status-text');
  pill.className = `status-pill ${state}`;
  const dot = pill.querySelector('.dot');
  dot.className = state === 'connected' ? 'dot' : 'dot blink';
  label.textContent = text;
}

// ── terminal ─────────────────────────────────────────────────────────────────
function initTerminal() {
  if (term) { term.dispose(); }

  const container = document.getElementById('terminal-container');
  document.getElementById('splash').style.display = 'none';

  term = new Terminal({
    fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
    fontSize: 14,
    lineHeight: 1.3,
    theme: {
      background: '#0d0f14',
      foreground: '#e2e8f0',
      cursor: '#4af2a1',
      cursorAccent: '#0d0f14',
      black: '#1a1f2e',
      red: '#fc8181',
      green: '#4af2a1',
      yellow: '#f6e05e',
      blue: '#63b3ed',
      magenta: '#d6bcfa',
      cyan: '#2dd4bf',
      white: '#e2e8f0',
      brightBlack: '#4a5568',
      brightGreen: '#68d391',
    },
    cursorBlink: true,
    scrollback: 5000,
    allowTransparency: true,
  });

  // xterm-addon-fit via jsdelivr exports constructor as window.FitAddon
  fitAddon = (typeof FitAddon === "function") ? new FitAddon() : new FitAddon.FitAddon();
  term.loadAddon(fitAddon);
  term.open(container);
  fitAddon.fit();
  term.focus();

  term.onData(data => {
    if (!sessionActive || !mqttClient) return;
    const topic = `shell/${selectedDevice}/session/${sessionId}/in`;
    mqttClient.publish(topic, data, { qos: 0 });
  });

  // Fallback: if xterm loses focus, document keydown re-focuses and replays the key
  document.addEventListener('keydown', (e) => {
    if (!sessionActive) return;
    // If the active element is not inside the terminal, re-focus and send the key
    const termEl = container.querySelector('.xterm');
    if (termEl && !termEl.contains(document.activeElement)) {
      term.focus();
    }
  });

  container.addEventListener('click', () => term.focus());

  window.addEventListener('resize', () => {
    if (fitAddon) {
      fitAddon.fit();
      sendResize();
    }
  });
}

function sendResize() {
  if (!sessionActive || !mqttClient || !term) return;
  const topic = `shell/${selectedDevice}/session/${sessionId}/resize`;
  mqttClient.publish(topic, JSON.stringify({
    rows: term.rows,
    cols: term.cols,
  }), { qos: 0 });
}

// ── device list UI ───────────────────────────────────────────────────────────
function renderDevices() {
  const container = document.getElementById('devices-list');
  const ids = Object.keys(devices).filter(id => devices[id].online);

  if (ids.length === 0) {
    container.innerHTML = '<div class="no-devices">No online devices</div>';
    return;
  }

  container.innerHTML = '';
  ids.forEach(id => {
    const info = devices[id];
    const el = document.createElement('div');
    el.className = `device-item${selectedDevice === id ? ' selected' : ''}`;
    el.innerHTML = `
      <div class="device-dot"></div>
      <div>
        <div class="device-name">${id}</div>
        <div class="device-meta">${info.shell || 'bash'} · ${info.active_sessions || 0} sessions</div>
      </div>`;
    el.onclick = () => selectDevice(id);
    container.appendChild(el);
  });
}

function selectDevice(id) {
  selectedDevice = id;
  renderDevices();
  document.getElementById('btn-open-session').style.display = 'inline-block';
  document.getElementById('btn-test-input').style.display = 'inline-block';
  logLine(`Device selected: ${id}`, 'ok');
}

// ── session ───────────────────────────────────────────────────────────────────
function openSession() {
  if (!selectedDevice || !mqttClient) return;

  // close previous session
  if (sessionActive) {
    mqttClient.unsubscribe(`shell/${selectedDevice}/session/${sessionId}/out`);
    sessionActive = false;
  }

  sessionId = Math.random().toString(36).substr(2, 8);
  document.getElementById('session-label').textContent = sessionId;

  // subscribe to session output and session-specific retained announce
  mqttClient.subscribe(`shell/${selectedDevice}/session/${sessionId}/out`, { qos: 0 });
  mqttClient.subscribe(`shell/${selectedDevice}/control/announce/${sessionId}`, { qos: 1 });

  // small delay so subscriptions are confirmed before requesting the session
  setTimeout(() => {
    mqttClient.publish(
      `shell/${selectedDevice}/control/new`,
      JSON.stringify({ session_id: sessionId }),
      { qos: 1 }
    );
    logLine(`Session request sent for ${sessionId}`);
  }, 300);

  initTerminal();
  term.write(`\x1b[90mConnecting to ${selectedDevice}...\x1b[0m\r\n`);
  logLine(`Requesting session ${sessionId} on ${selectedDevice}...`);
}

// ── MQTT ─────────────────────────────────────────────────────────────────────
function connectBroker() {
  const host = document.getElementById('broker-host').value.trim() || 'localhost';
  const port = parseInt(document.getElementById('broker-port').value) || 9001;
  const user = document.getElementById('broker-user').value.trim();
  const pass = document.getElementById('broker-pass').value;

  const clientId = `mqtt-shell-web-${Math.random().toString(36).substr(2, 6)}`;
  const url = `ws://${host}:${port}/mqtt`;

  logLine(`Connecting to ${url} ...`);
  setBrokerStatus('', 'connecting...');

  const opts = { clientId };
  if (user) { opts.username = user; opts.password = pass; }

  mqttClient = mqtt.connect(url, opts);

  mqttClient.on('connect', () => {
    setBrokerStatus('connected', `${host}:${port}`);
    logLine(`Connected to broker ${host}:${port}`, 'ok');

    // subscribe to all device presence topics
    mqttClient.subscribe('shell/+/presence', { qos: 1 });
    // announce topics are session-specific and retained — subscribed per session in openSession()

    document.getElementById('btn-connect').style.display = 'none';
    document.getElementById('btn-disconnect').style.display = 'block';
    document.getElementById('devices-list').innerHTML =
      '<div class="no-devices">Waiting for devices...</div>';
  });

  mqttClient.on('message', (topic, payload) => {
    // device presence
    if (topic.match(/^shell\/[^/]+\/presence$/)) {
      try {
        const data = JSON.parse(payload.toString());
        const id = data.device_id;
        if (id) {
          if (data.online) {
            devices[id] = data;
            logLine(`Device online: ${id}`, 'ok');
          } else {
            delete devices[id];
          }
          renderDevices();
        }
      } catch (e) {}
      return;
    }

    // session creation confirmed (session-specific retained topic)
    if (sessionId && topic === `shell/${selectedDevice}/control/announce/${sessionId}`) {
      try {
        const data = JSON.parse(payload.toString());
        if (data.session_id === sessionId) {
          sessionActive = true;
          // force focus with small delay to let the DOM settle
          setTimeout(() => { term.focus(); }, 100);
          setTimeout(() => { term.focus(); }, 500);
          const sep = '─'.repeat(50);
          term.write(`\r\n\x1b[1;32m${sep}\x1b[0m\r\n`);
          term.write(`\x1b[1;32m  mole connected\x1b[0m  \x1b[90mdevice:\x1b[0m ${selectedDevice}\r\n`);
          term.write(`\x1b[1;32m${sep}\x1b[0m\r\n\r\n`);
          logLine(`Session ${sessionId} active`, 'ok');
          sendResize();
          // send distinctive PS1
          const ps1 = 'export PS1="' +
                    '\\[\\033[1;33m\\][mole:' + selectedDevice + '\\[\\033[0m\\] \\w \\$ "' +
                    '\n';
          setTimeout(() => mqttClient.publish(
            `shell/${selectedDevice}/session/${sessionId}/in`,
            ps1, { qos: 0 }
          ), 300);
        }
      } catch (e) {}
      return;
    }

    // session output -> write to terminal
    if (sessionId && topic === `shell/${selectedDevice}/session/${sessionId}/out`) {
      if (term) term.write(payload);
      return;
    }
  });

  mqttClient.on('error', err => {
    setBrokerStatus('error', 'error');
    logLine(`MQTT error: ${err.message}`, 'err');
  });

  let _lastDisconnectLog = 0;
  mqttClient.on('close', () => {
    setBrokerStatus('', 'disconnected');
    // throttle disconnect log to avoid spam on repeated reconnect attempts
    const now = Date.now();
    if (now - _lastDisconnectLog > 5000) {
      logLine('Disconnected from broker', 'warn');
      _lastDisconnectLog = now;
    }
    sessionActive = false;
    document.getElementById('btn-connect').style.display = 'block';
    document.getElementById('btn-disconnect').style.display = 'none';
  });
}

function disconnectBroker() {
  if (mqttClient) { mqttClient.end(); }
  sessionActive = false;
  Object.keys(devices).forEach(k => delete devices[k]);
  renderDevices();
}

// ── eventos ──────────────────────────────────────────────────────────────────
document.getElementById('btn-connect').onclick = connectBroker;

document.getElementById('btn-test-input').onclick = () => {
  if (!sessionActive || !mqttClient) {
    logLine('Session not active', 'err');
    return;
  }
  logLine('Sending test: echo hello', 'ok');
  mqttClient.publish(
    `shell/${selectedDevice}/session/${sessionId}/in`,
    'echo hello_from_web\n',
    { qos: 0 }
  );
};

document.getElementById('btn-disconnect').onclick = disconnectBroker;
document.getElementById('btn-open-session').onclick = openSession;
</script>
</body>
</html>
"""


async def handle_index(request):
    return web.Response(text=HTML, content_type="text/html")


def main():
    parser = argparse.ArgumentParser(description="mqtt-shell web client")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [WEB] %(levelname)s %(message)s")

    app = web.Application()
    app.router.add_get("/", handle_index)

    log.info("Web client available at http://localhost:%d", args.port)
    log.info("(O broker MQTT precisa de WebSockets activos, tipicamente porta 9001)")
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
