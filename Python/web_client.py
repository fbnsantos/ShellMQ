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
  .device-dot.offline { background: var(--muted); }
  .device-session { font-size: 10px; color: var(--accent2); margin-left: auto; }

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
    overflow: hidden;
    position: relative;
  }

  /* one host per session; only the selected device's is visible */
  .term-host {
    position: absolute;
    inset: 4px;
    display: none;
  }

  .term-host.visible { display: block; }

  .session-picker {
    position: absolute;
    inset: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 10px;
    font-size: 12px;
    color: var(--text-dim);
  }

  .picker-title { font-size: 13px; margin-bottom: 6px; }
  .picker-row   { display: flex; align-items: center; gap: 12px; }
  .picker-age   { min-width: 160px; }

  .btn-ghost {
    background: transparent;
    color: var(--accent2);
    border: 1px solid var(--accent2);
  }

  .btn-small {
    width: auto;
    padding: 3px 12px;
    margin-top: 0;
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
        <label class="field-label" style="display:flex;align-items:center;gap:6px;cursor:pointer">
          <input type="checkbox" id="broker-tls"> WSS (TLS)
        </label>
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
        <button class="btn btn-ghost btn-small" id="btn-detach-session" style="display:none"
                title="Leave the shell running on the device and show its open sessions">
          Sessions
        </button>
        <button class="btn btn-danger btn-small" id="btn-close-session" style="display:none">
          Close Session
        </button>
        <button class="btn btn-primary" id="btn-test-input"
                style="width:auto;padding:3px 12px;display:none;margin-left:6px;background:var(--accent2)">
          Test
        </button>
      </div>
      <div id="terminal-container">
        <div class="session-picker" id="session-picker" style="display:none"></div>
        <div class="splash" id="splash">
          <div class="splash-logo">mqtt<span>://</span>shell</div>
          <div id="splash-text">Connect to a broker and select a device</div>
        </div>
      </div>
      <div class="log-panel" id="log-panel"></div>
    </div>
  </main>
</div>

<script>
// ── estado global ────────────────────────────────────────────────────────────
let mqttClient = null;
let selectedDevice = null;
const devices = {};    // device_id -> last presence payload (online devices only)
const sessions = {};   // device_id -> session (one per device)

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

// ── sessions ─────────────────────────────────────────────────────────────────
// Each session owns its terminal and remembers its device and topics, so
// switching devices never touches another session's subscriptions.

function sessionTopics(device, id) {
  return {
    in:       `shell/${device}/session/${id}/in`,
    out:      `shell/${device}/session/${id}/out`,
    resize:   `shell/${device}/session/${id}/resize`,
    announce: `shell/${device}/control/announce/${id}`,
  };
}

function createTerminal(s) {
  s.el = document.createElement('div');
  s.el.className = 'term-host';
  document.getElementById('terminal-container').appendChild(s.el);

  s.term = new Terminal({
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
  s.fit = (typeof FitAddon === "function") ? new FitAddon() : new FitAddon.FitAddon();
  s.term.loadAddon(s.fit);
  s.term.open(s.el);

  s.term.onData(data => {
    if (!s.active || !mqttClient) return;
    mqttClient.publish(s.topics.in, data, { qos: 0 });
  });
}

function newSessionObject(device, id) {
  return { device, id, topics: sessionTopics(device, id),
           active: false, ended: false, attaching: false, replyId: null };
}

function openSession() {
  if (!selectedDevice || !mqttClient) return;
  const device = selectedDevice;

  // the current session keeps running on the device; it can be reattached
  if (sessions[device]) detachSession(device);

  const id = Math.random().toString(36).substr(2, 8);
  const s = newSessionObject(device, id);
  sessions[device] = s;
  createTerminal(s);

  // subscribe to session output and session-specific retained announce
  mqttClient.subscribe(s.topics.out, { qos: 0 });
  mqttClient.subscribe(s.topics.announce, { qos: 1 });

  // small delay so subscriptions are confirmed before requesting the session
  setTimeout(() => {
    mqttClient.publish(
      `shell/${device}/control/new`,
      JSON.stringify({ session_id: id }),
      { qos: 1 }
    );
    logLine(`Session request sent for ${id}`);
  }, 300);

  showSession(device);
  s.term.write(`\x1b[90mConnecting to ${device}...\x1b[0m\r\n`);
  logLine(`Requesting session ${id} on ${device}...`);
  renderDevices();
}

// Reattaches to a session that is already open on the device: the server
// answers on a private replay topic with the recent output of the shell.
function attachSession(device, id) {
  if (!mqttClient) return;
  if (sessions[device]) detachSession(device);

  const s = newSessionObject(device, id);
  s.attaching = true;
  s.replyId = Math.random().toString(36).substr(2, 10);
  s.topics.replay = `shell/${device}/session/${id}/replay/${s.replyId}`;
  sessions[device] = s;
  createTerminal(s);

  mqttClient.subscribe(s.topics.replay, { qos: 1 });
  mqttClient.subscribe(s.topics.out, { qos: 0 });
  mqttClient.subscribe(s.topics.announce, { qos: 1 });

  setTimeout(() => {
    mqttClient.publish(`shell/${device}/control/attach`,
                       JSON.stringify({ session_id: id, reply_id: s.replyId }), { qos: 1 });
  }, 300);
  setTimeout(() => {
    if (s.attaching && sessions[device] === s) markSessionEnded(s, 'session not found on the device');
  }, 6000);

  showSession(device);
  logLine(`Attaching to session ${id} on ${device}...`);
  renderDevices();
}

function handleReplay(s, payload) {
  if (!s.attaching) return;
  s.attaching = false;
  s.active = true;
  mqttClient.unsubscribe(s.topics.replay);
  s.term.reset();
  s.term.write(payload);
  s.term.write('\r\n\x1b[90m[reattached — press Enter if the prompt is not visible]\x1b[0m\r\n');
  logLine(`Session ${s.id} on ${s.device} reattached`, 'ok');
  sendResize(s);
  if (s.device === selectedDevice) setTimeout(() => s.term.focus(), 100);
  updateSessionControls();
  renderDevices();
}

// Stops showing a session locally; the shell keeps running on the device
// (until the server's idle timeout) and can be reattached later.
function detachSession(device) {
  const s = sessions[device];
  if (!s) return;
  if (mqttClient) {
    mqttClient.unsubscribe(s.topics.out);
    mqttClient.unsubscribe(s.topics.announce);
    if (s.topics.replay) mqttClient.unsubscribe(s.topics.replay);
  }
  s.term.dispose();
  s.el.remove();
  delete sessions[device];
  if (!s.ended) logLine(`Detached from session ${s.id} on ${device} (still running there)`);
  if (device === selectedDevice) showSession(device);
  renderDevices();
}

// Closes the device's session: tells the server to kill the shell (unless it
// already ended) and drops the local terminal.
function closeSession(device) {
  const s = sessions[device];
  if (!s) return;
  if (mqttClient && !s.ended) {
    mqttClient.publish(`shell/${device}/control/close`,
                       JSON.stringify({ session_id: s.id }), { qos: 1 });
  }
  s.ended = true;
  logLine(`Session ${s.id} on ${device} closed`);
  detachSession(device);
}

// The server closed the session (shell exited, close request, device restart).
function markSessionEnded(s, reason) {
  if (s.ended) return;
  s.active = false;
  s.attaching = false;
  s.ended = true;
  if (mqttClient) mqttClient.unsubscribe(s.topics.out);
  s.term.write(`\r\n\x1b[90m[${reason} — click New Session to start another]\x1b[0m\r\n`);
  logLine(`Session ${s.id} on ${s.device} ended (${reason})`, 'warn');
  updateSessionControls();
  renderDevices();
}

function formatAge(createdAt) {
  if (!createdAt) return '';
  const min = Math.max(0, Math.round((Date.now() / 1000 - createdAt) / 60));
  if (min < 1) return 'opened just now';
  if (min < 60) return `opened ${min} min ago`;
  return `opened ${Math.floor(min / 60)} h ${min % 60} min ago`;
}

// Lists the sessions open on the device (from its presence) so one can be reattached.
function renderSessionPicker(device) {
  const picker = document.getElementById('session-picker');
  picker.innerHTML = '';
  const open = (devices[device] && devices[device].sessions) || [];

  const title = document.createElement('div');
  title.className = 'picker-title';
  title.textContent = open.length
    ? `Open sessions on ${device}`
    : `No open sessions on ${device} — click New Session`;
  picker.appendChild(title);

  open.forEach(x => {
    const row = document.createElement('div');
    row.className = 'picker-row';
    const label = document.createElement('span');
    label.className = 'session-badge';
    label.textContent = x.session_id;
    const age = document.createElement('span');
    age.className = 'picker-age';
    age.textContent = formatAge(x.created_at);
    const btn = document.createElement('button');
    btn.className = 'btn btn-primary btn-small';
    btn.textContent = 'Attach';
    btn.onclick = () => attachSession(device, x.session_id);
    row.append(label, age, btn);
    picker.appendChild(row);
  });
}

function showSession(device) {
  Object.values(sessions).forEach(x => x.el.classList.remove('visible'));
  const s = sessions[device];
  const splash = document.getElementById('splash');
  const picker = document.getElementById('session-picker');

  if (!s) {
    if (device && devices[device]) {
      splash.style.display = 'none';
      picker.style.display = '';
      renderSessionPicker(device);
    } else {
      picker.style.display = 'none';
      splash.style.display = '';
      document.getElementById('splash-text').textContent = device
        ? `${device} is offline`
        : 'Connect to a broker and select a device';
    }
    updateSessionControls();
    return;
  }

  splash.style.display = 'none';
  picker.style.display = 'none';
  s.el.classList.add('visible');
  s.fit.fit();
  sendResize(s);
  s.term.focus();
  updateSessionControls();
}

function currentSession() {
  return selectedDevice ? sessions[selectedDevice] : null;
}

function updateSessionControls() {
  const s = currentSession();
  document.getElementById('session-label').textContent =
    s ? `${s.id}${s.ended ? ' (ended)' : ''}` : '—';
  const hasDevice = !!selectedDevice && !!mqttClient;
  document.getElementById('btn-open-session').style.display = hasDevice ? 'inline-block' : 'none';
  document.getElementById('btn-test-input').style.display = hasDevice ? 'inline-block' : 'none';
  document.getElementById('btn-detach-session').style.display = s ? 'inline-block' : 'none';
  document.getElementById('btn-close-session').style.display = s ? 'inline-block' : 'none';
}

function sendResize(s) {
  if (!s || !s.active || !mqttClient) return;
  mqttClient.publish(s.topics.resize, JSON.stringify({
    rows: s.term.rows,
    cols: s.term.cols,
  }), { qos: 0 });
}

// ── device list UI ───────────────────────────────────────────────────────────
function renderDevices() {
  const container = document.getElementById('devices-list');
  // online devices, plus devices we still hold a session for
  const ids = [...new Set([...Object.keys(devices), ...Object.keys(sessions)])].sort();

  if (ids.length === 0) {
    container.innerHTML = '<div class="no-devices">No online devices</div>';
    return;
  }

  container.innerHTML = '';
  ids.forEach(id => {
    const info = devices[id];
    const s = sessions[id];
    const el = document.createElement('div');
    el.className = `device-item${selectedDevice === id ? ' selected' : ''}`;

    const dot = document.createElement('div');
    dot.className = `device-dot${info ? '' : ' offline'}`;
    const text = document.createElement('div');
    const name = document.createElement('div');
    name.className = 'device-name';
    name.textContent = id;
    const meta = document.createElement('div');
    meta.className = 'device-meta';
    meta.textContent = info
      ? `${info.shell || 'bash'} · ${info.active_sessions || 0} sessions`
      : 'offline';
    text.append(name, meta);
    el.append(dot, text);

    if (s) {
      const badge = document.createElement('div');
      badge.className = 'device-session';
      badge.textContent = s.ended ? 'ended' : (s.active ? 'open' : '...');
      el.appendChild(badge);
    }

    el.onclick = () => selectDevice(id);
    container.appendChild(el);
  });
}

function selectDevice(id) {
  selectedDevice = id;
  renderDevices();
  showSession(id);
  logLine(`Device selected: ${id}`, 'ok');
}

// ── MQTT ─────────────────────────────────────────────────────────────────────
function handlePresence(device, payload) {
  let data;
  try { data = JSON.parse(payload.toString()); } catch (e) { return; }
  const s = sessions[device];

  if (data.online) {
    const wasOffline = !devices[device];
    devices[device] = data;
    if (wasOffline) logLine(`Device online: ${device}`, 'ok');
    // the server lists its open sessions; if ours is gone the shell died
    // (e.g. the server restarted while we were away)
    if (s && s.active && Array.isArray(data.sessions) &&
        !data.sessions.some(x => x.session_id === s.id)) {
      markSessionEnded(s, 'session no longer exists on the device');
    }
  } else {
    if (devices[device]) logLine(`Device offline: ${device}`, 'warn');
    delete devices[device];
    if (s && s.active) {
      s.term.write('\r\n\x1b[33m[device offline — waiting for it to come back]\x1b[0m\r\n');
    }
  }
  renderDevices();
  if (device === selectedDevice && !s) showSession(device);  // refresh the picker
}

function handleAnnounce(s, payload) {
  // empty retained message = the server cleared the announce on close
  if (payload.length === 0) {
    if (s.active) markSessionEnded(s, 'session closed');
    return;
  }
  let data;
  try { data = JSON.parse(payload.toString()); } catch (e) { return; }
  // an attach is confirmed by the replay, not by the announce
  if (data.session_id !== s.id || s.active || s.ended || s.attaching) return;

  s.active = true;
  const sep = '─'.repeat(50);
  s.term.write(`\r\n\x1b[1;32m${sep}\x1b[0m\r\n`);
  s.term.write(`\x1b[1;32m  mole connected\x1b[0m  \x1b[90mdevice:\x1b[0m ${s.device}\r\n`);
  s.term.write(`\x1b[1;32m${sep}\x1b[0m\r\n\r\n`);
  logLine(`Session ${s.id} active`, 'ok');
  sendResize(s);
  if (s.device === selectedDevice) {
    // force focus with small delay to let the DOM settle
    setTimeout(() => s.term.focus(), 100);
  }
  updateSessionControls();
  renderDevices();

  // send distinctive PS1
  const ps1 = 'export PS1="' +
            '\\[\\033[1;33m\\][mole:' + s.device + '\\[\\033[0m\\] \\w \\$ "' +
            '\n';
  setTimeout(() => {
    if (s.active && mqttClient) mqttClient.publish(s.topics.in, ps1, { qos: 0 });
  }, 300);
}

function connectBroker() {
  const host = document.getElementById('broker-host').value.trim() || 'localhost';
  const port = parseInt(document.getElementById('broker-port').value) || 9001;
  const user = document.getElementById('broker-user').value.trim();
  const pass = document.getElementById('broker-pass').value;

  // never leave a previous client reconnecting in the background
  if (mqttClient) mqttClient.end(true);

  const clientId = `mqtt-shell-web-${Math.random().toString(36).substr(2, 6)}`;
  const tls = document.getElementById('broker-tls').checked;
  const url = `${tls ? 'wss' : 'ws'}://${host}:${port}/mqtt`;

  logLine(`Connecting to ${url} as ${clientId} ...`);
  setBrokerStatus('', 'connecting...');

  const opts = { clientId };
  if (user) { opts.username = user; opts.password = pass; }

  const client = mqtt.connect(url, opts);
  mqttClient = client;
  let everConnected = false;
  let attempts = 0;

  client.on('connect', () => {
    setBrokerStatus('connected', `${host}:${port}`);
    if (everConnected) {
      // mqtt.js resubscribes our topics automatically
      logLine(`Reconnected to broker ${host}:${port} after ${attempts} attempt(s)`, 'ok');
    } else {
      logLine(`Connected to broker ${host}:${port}`, 'ok');
      // subscribe to all device presence topics
      client.subscribe('shell/+/presence', { qos: 1 });
      // announce topics are session-specific and retained — subscribed per session in openSession()
      document.getElementById('devices-list').innerHTML =
        '<div class="no-devices">Waiting for devices...</div>';
    }
    everConnected = true;
    attempts = 0;
    document.getElementById('btn-connect').style.display = 'none';
    document.getElementById('btn-disconnect').style.display = 'block';
    updateSessionControls();
  });

  client.on('message', (topic, payload) => {
    let m = topic.match(/^shell\/([^/]+)\/presence$/);
    if (m) { handlePresence(m[1], payload); return; }

    m = topic.match(/^shell\/([^/]+)\/control\/announce\/([^/]+)$/);
    if (m) {
      const s = sessions[m[1]];
      if (s && s.id === m[2]) handleAnnounce(s, payload);
      return;
    }

    // session output -> write to that session's terminal (visible or not);
    // while attaching it is dropped, the replay already contains it
    m = topic.match(/^shell\/([^/]+)\/session\/([^/]+)\/out$/);
    if (m) {
      const s = sessions[m[1]];
      if (s && s.id === m[2] && !s.attaching) s.term.write(payload);
      return;
    }

    m = topic.match(/^shell\/([^/]+)\/session\/([^/]+)\/replay\/([^/]+)$/);
    if (m) {
      const s = sessions[m[1]];
      if (s && s.id === m[2] && s.replyId === m[3]) handleReplay(s, payload);
      return;
    }
  });

  client.on('error', err => {
    setBrokerStatus('error', 'error');
    logLine(`MQTT error: ${err.message}${err.code !== undefined ? ` (code ${err.code})` : ''}`, 'err');
  });

  // MQTT 5 brokers may say why they disconnect us
  client.on('disconnect', packet => {
    const rc = packet && packet.reasonCode;
    const why = packet && packet.properties && packet.properties.reasonString;
    logLine(`Broker sent DISCONNECT (reason ${rc}${why ? `: ${why}` : ''})`, 'err');
  });

  client.on('close', () => {
    if (!everConnected) return;  // failed attempts are reported by 'reconnect'
    logLine('Connection to broker closed', 'warn');
  });

  client.on('offline', () => {
    setBrokerStatus('', 'reconnecting...');
    logLine('Broker offline — mqtt.js will keep retrying', 'warn');
  });

  client.on('reconnect', () => {
    attempts += 1;
    setBrokerStatus('', `reconnecting (${attempts})...`);
    // log the first attempts, then only every 10th to avoid flooding
    if (attempts <= 3 || attempts % 10 === 0) {
      logLine(`Reconnecting to ${url} (attempt ${attempts})...`, 'warn');
    }
  });

  client.on('end', () => {
    if (mqttClient !== client) return;  // replaced by a newer connection
    setBrokerStatus('', 'disconnected');
    logLine('Disconnected from broker', 'warn');
    document.getElementById('btn-connect').style.display = 'block';
    document.getElementById('btn-disconnect').style.display = 'none';
    mqttClient = null;
    updateSessionControls();
  });
}

function disconnectBroker() {
  if (!mqttClient) return;
  // shells keep running on the devices and can be reattached after reconnecting
  const n = Object.keys(sessions).length;
  Object.keys(sessions).forEach(detachSession);
  if (n) logLine(`${n} session(s) left running; reattach them after connecting again`);
  Object.keys(devices).forEach(k => delete devices[k]);
  selectedDevice = null;
  showSession(null);
  renderDevices();
  mqttClient.end();
}

// Every 30 s tell each device which of its sessions we are attached to, so
// the server does not close them as idle. Tabs that are closed stop sending
// and their sessions are closed after the server's --session-timeout.
setInterval(() => {
  if (!mqttClient || !mqttClient.connected) return;
  Object.values(sessions).forEach(s => {
    if (s.ended) return;
    mqttClient.publish(`shell/${s.device}/control/keepalive`,
                       JSON.stringify({ session_ids: [s.id] }), { qos: 0 });
  });
}, 30000);

// ── eventos ──────────────────────────────────────────────────────────────────
// registered once; they always act on the currently visible session

document.getElementById('btn-connect').onclick = connectBroker;

document.getElementById('btn-test-input').onclick = () => {
  const s = currentSession();
  if (!s || !s.active || !mqttClient) {
    logLine('Session not active', 'err');
    return;
  }
  logLine('Sending test: echo hello', 'ok');
  mqttClient.publish(s.topics.in, 'echo hello_from_web\n', { qos: 0 });
};

document.getElementById('btn-disconnect').onclick = disconnectBroker;
document.getElementById('btn-open-session').onclick = openSession;
document.getElementById('btn-close-session').onclick = () => {
  if (selectedDevice) closeSession(selectedDevice);
};
document.getElementById('btn-detach-session').onclick = () => {
  if (selectedDevice) detachSession(selectedDevice);
};

// Fallback: if nothing has focus, typing goes to the visible terminal
document.addEventListener('keydown', () => {
  const s = currentSession();
  if (!s || !s.active) return;
  const a = document.activeElement;
  if (!a || a === document.body) s.term.focus();
});

document.getElementById('terminal-container').addEventListener('click', () => {
  const s = currentSession();
  if (s) s.term.focus();
});

window.addEventListener('resize', () => {
  const s = currentSession();
  if (s) {
    s.fit.fit();
    sendResize(s);
  }
});

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
