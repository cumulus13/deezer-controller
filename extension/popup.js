/**
 * File: extension\popup.js
 * Author: Hadi Cahyadi <cumulus13@gmail.com>
 * Date: 2026-04-18
 * Description: Deezer Controller Bridge - Popup Script
 * License: MIT
 */

/**
 * Deezer Controller Bridge - Popup Script
 */

const $ = id => document.getElementById(id);

let logEntries = [];

function log(msg, type = 'info') {
  const box = $('logBox');
  const entry = document.createElement('div');
  const time = new Date().toLocaleTimeString('en-US', { hour12: false });
  entry.className = `log-entry ${type}`;
  entry.textContent = `[${time}] ${msg}`;
  box.appendChild(entry);
  box.scrollTop = box.scrollHeight;
  // Keep last 50
  while (box.children.length > 50) box.removeChild(box.firstChild);
}

async function updateStatus() {
  const data = await chrome.storage.local.get(['connectionState', 'wsPort', 'wsHost']);
  const state = data.connectionState || 'disconnected';
  const port  = data.wsPort || 8765;
  const host  = data.wsHost || 'localhost';

  const dot   = $('statusDot');
  const label = $('statusLabel');
  const detail = $('statusDetail');

  dot.className = `status-dot ${state}`;
  label.className = `status-label ${state}`;
  label.textContent = state.toUpperCase();

  const msgs = {
    connected:    'Relay server active',
    connecting:   'Attempting connection...',
    disconnected: 'Start the relay server',
  };
  detail.textContent = msgs[state] || '';

  $('footerHost').textContent = host;
  $('footerPort').textContent = port;
  $('wsHost').value = host;
  $('wsPort').value = port;
}

// Send command via background messaging
async function sendCommand(action, params = {}) {
  // We can't directly message the WebSocket from popup,
  // so we store a command request and the background picks it up,
  // OR we use chrome.runtime.sendMessage to background
  return new Promise((resolve) => {
    chrome.runtime.sendMessage({ type: 'quick_command', action, params }, (resp) => {
      if (chrome.runtime.lastError) {
        resolve({ error: chrome.runtime.lastError.message });
      } else {
        resolve(resp || {});
      }
    });
  });
}

// Load config
async function loadConfig() {
  const data = await chrome.storage.local.get(['wsPort', 'wsHost']);
  if (data.wsPort) $('wsPort').value = data.wsPort;
  if (data.wsHost) $('wsHost').value = data.wsHost;
}

// Save config
$('saveBtn').addEventListener('click', async () => {
  const port = parseInt($('wsPort').value);
  const host = $('wsHost').value.trim();
  if (!host || !port) return;
  await chrome.storage.local.set({ wsPort: port, wsHost: host });
  log(`Config saved: ${host}:${port}`, 'info');
  updateStatus();
});

// Quick control buttons
const controlMap = [
  ['btnPlay',    'play',     '▶ Play'],
  ['btnPause',   'pause',    '⏸ Pause'],
  ['btnNext',    'next',     '⏭ Next'],
  ['btnPrev',    'previous', '⏮ Previous'],
  ['btnShuffle', 'shuffle',  '🔀 Shuffle'],
  ['btnLike',    'like',     '♥ Like'],
];

controlMap.forEach(([id, action, label]) => {
  $(id).addEventListener('click', async () => {
    log(`→ ${label}`, 'info');
    const resp = await sendCommand(action);
    if (resp.error) {
      log(`✕ ${resp.error}`, 'err');
    } else {
      log(`✓ ${label} OK`, 'ok');
    }
  });
});

$('reconnectBtn').addEventListener('click', () => {
  chrome.runtime.sendMessage({ type: 'reconnect' });
  log('Reconnect requested...', 'info');
});

// Poll status every second
updateStatus();
loadConfig();
setInterval(updateStatus, 1500);
