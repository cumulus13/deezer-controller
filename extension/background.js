/**
 * File: extension\background.js
 * Author: Hadi Cahyadi <cumulus13@gmail.com>
 * Date: 2026-04-18
 * Description: Deezer Controller Bridge - Background Service Worker Connects to local WebSocket relay server and executes commands on Deezer tab
 * License: MIT
 */

/**
 * Deezer Controller Bridge - Background Service Worker
 * Connects to local WebSocket relay server and executes commands on Deezer tab
 * 
 * @version 2.0.0
 */

const CONFIG_DEFAULTS = {
  wsPort: 8765,
  wsHost: 'localhost',
  reconnectDelay: 2000,
  reconnectMaxDelay: 30000,
  heartbeatInterval: 15000,
};

let ws = null;
let reconnectTimer = null;
let heartbeatTimer = null;
let reconnectDelay = CONFIG_DEFAULTS.reconnectDelay;
let connectionState = 'disconnected'; // disconnected | connecting | connected
let config = { ...CONFIG_DEFAULTS };

// ─── State ────────────────────────────────────────────────────────────────────

async function loadConfig() {
  const stored = await chrome.storage.local.get(['wsPort', 'wsHost']);
  config = { ...CONFIG_DEFAULTS, ...stored };
}

function setConnectionState(state) {
  connectionState = state;
  chrome.storage.local.set({ connectionState: state, lastStateChange: Date.now() });
  // Update icon badge
  const badges = {
    connected:    { text: '●', color: '#1DB954' },
    connecting:   { text: '…', color: '#F59E0B' },
    disconnected: { text: '✕', color: '#EF4444' },
  };
  const badge = badges[state] || badges.disconnected;
  chrome.action.setBadgeText({ text: badge.text });
  chrome.action.setBadgeBackgroundColor({ color: badge.color });
}

// ─── WebSocket Connection ──────────────────────────────────────────────────────

function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

  setConnectionState('connecting');
  const url = `ws://${config.wsHost}:${config.wsPort}`;

  try {
    ws = new WebSocket(url);
  } catch (e) {
    console.error('[Bridge] WebSocket creation failed:', e);
    scheduleReconnect();
    return;
  }

  ws.onopen = () => {
    console.log('[Bridge] Connected to relay server');
    reconnectDelay = CONFIG_DEFAULTS.reconnectDelay;
    setConnectionState('connected');
    startHeartbeat();
    // Announce ourselves
    send({ type: 'hello', client: 'extension', version: '2.0.0' });
  };

  ws.onmessage = async (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch {
      console.warn('[Bridge] Invalid JSON received:', event.data);
      return;
    }

    if (msg.type === 'ping') {
      send({ type: 'pong', ts: msg.ts });
      return;
    }

    if (msg.type === 'command') {
      await handleCommand(msg);
    }
  };

  ws.onerror = (e) => {
    console.error('[Bridge] WebSocket error:', e);
  };

  ws.onclose = (e) => {
    console.warn(`[Bridge] Disconnected (code=${e.code})`);
    stopHeartbeat();
    setConnectionState('disconnected');
    ws = null;
    scheduleReconnect();
  };
}

function send(data) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(data));
    return true;
  }
  return false;
}

function scheduleReconnect() {
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(() => {
    console.log(`[Bridge] Reconnecting...`);
    connect();
  }, reconnectDelay);
  // Exponential backoff capped at max
  reconnectDelay = Math.min(reconnectDelay * 1.5, CONFIG_DEFAULTS.reconnectMaxDelay);
}

function startHeartbeat() {
  stopHeartbeat();
  heartbeatTimer = setInterval(() => {
    send({ type: 'ping', ts: Date.now() });
  }, CONFIG_DEFAULTS.heartbeatInterval);
}

function stopHeartbeat() {
  clearInterval(heartbeatTimer);
}

// ─── Deezer Tab Finder ────────────────────────────────────────────────────────

async function findDeezerTab() {
  const tabs = await chrome.tabs.query({ url: '*://*.deezer.com/*' });
  if (tabs.length === 0) return null;
  // Prefer active tab, fallback to first
  return tabs.find(t => t.active) || tabs[0];
}

async function execInDeezer(func, args = []) {
  const tab = await findDeezerTab();
  if (!tab) throw new Error('No Deezer tab found. Please open deezer.com in a tab.');

  const results = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func,
    args,
  });

  return results?.[0]?.result;
}

// ─── Deezer DOM Scripts (run inside tab context) ───────────────────────────────

const DeezerScripts = {
  play: () => {
    const btn = document.querySelector('button[data-testid="play_button_play"]');
    if (!btn) throw new Error('Play button not found');
    btn.click();
    return { ok: true };
  },

  pause: () => {
    const btn = document.querySelector('button[data-testid="play_button_pause"]');
    if (!btn) throw new Error('Pause button not found');
    btn.click();
    return { ok: true };
  },

  next: () => {
    const btn = document.querySelector('button[data-testid="next_track_button"]');
    if (!btn) throw new Error('Next button not found');
    btn.click();
    return { ok: true };
  },

  previous: () => {
    const btn = document.querySelector('button[data-testid="previous_track_button"]');
    if (!btn) throw new Error('Previous button not found');
    btn.click();
    return { ok: true };
  },

  getRepeatStatus: () => {
    const btn = document.querySelector('button[data-testid*="repeat_button_"]');
    if (!btn) return { status: 'unknown' };
    const testId = btn.getAttribute('data-testid');
    const map = {
      'repeat_button_all': 'all',
      'repeat_button_single': 'one',
      'repeat_button_off': 'off',
    };
    return { status: map[testId] || testId };
  },

  setRepeat: (targetStatus) => {
    const map = {
      'all': 'repeat_button_all',
      'one': 'repeat_button_single',
      'off': 'repeat_button_off',
    };
    const targetTestId = map[targetStatus];
    if (!targetTestId) throw new Error(`Invalid repeat status: ${targetStatus}`);

    let attempts = 0;
    while (attempts < 4) {
      const btn = document.querySelector('button[data-testid*="repeat_button_"]');
      if (!btn) throw new Error('Repeat button not found');
      const current = btn.getAttribute('data-testid');
      if (current === targetTestId) return { status: targetStatus, changed: attempts > 0 };
      btn.click();
      attempts++;
    }
    throw new Error(`Could not set repeat to ${targetStatus} after ${attempts} attempts`);
  },

  getVolume: () => {
    const slider = document.querySelector('[data-testid="volume_slider"] input, input[aria-label*="volume" i], input[aria-label*="Volume" i]');
    if (!slider) return { volume: null };
    return { volume: parseFloat(slider.value) };
  },

  setVolume: (level) => {
    const slider = document.querySelector('[data-testid="volume_slider"] input, input[aria-label*="volume" i]');
    if (!slider) throw new Error('Volume slider not found');
    const nativeInputSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    nativeInputSetter.call(slider, level);
    slider.dispatchEvent(new Event('input', { bubbles: true }));
    slider.dispatchEvent(new Event('change', { bubbles: true }));
    return { volume: level };
  },

  getCurrentTrack: () => {
    const title = document.querySelector('[data-testid="item_title"]')?.textContent?.trim()
      || document.querySelector('.track-link')?.textContent?.trim();
    const artist = document.querySelector('[data-testid="item_subtitle"] a, .player-track-artist a')?.textContent?.trim();
    const coverEl = document.querySelector('[data-testid="cover"] img, .player-track-cover img');
    const cover = coverEl?.src || null;
    const progressEl = document.querySelector('[data-testid="progress_bar"] input, input[aria-label*="timeline" i]');
    const durationEl = document.querySelector('[data-testid="time_duration"], .player-track-duration');
    const positionEl = document.querySelector('[data-testid="time_current"], .player-track-position');

    return {
      title: title || null,
      artist: artist || null,
      cover: cover || null,
      duration: durationEl?.textContent?.trim() || null,
      position: positionEl?.textContent?.trim() || null,
      progress: progressEl ? parseFloat(progressEl.value) : null,
    };
  },

  getPlaylist: () => {
    // Ensure queue is open
    const queueBtn = document.querySelector('button[data-testid="queue_list_button"]');
    const isActive = queueBtn?.hasAttribute('data-active');
    if (queueBtn && !isActive) queueBtn.click();

    const container = document.querySelector('.queuelist-content');
    if (!container) return { playlist: [], error: 'Queue panel not found' };

    const items = container.querySelectorAll('[class*="JIYRe"], [data-testid="queue_song_row"]');
    const playlist = Array.from(items).map((item, index) => {
      const title = item.querySelector('[data-testid="title"]')?.textContent?.trim() || null;
      const artistEl = item.querySelector('a[data-testid="artist"], a[href*="/artist/"]');
      const artist = artistEl?.textContent?.trim() || null;
      const artistLink = artistEl?.href || null;
      const durationMatch = item.textContent.match(/\d{1,2}:\d{2}/);
      const duration = durationMatch ? durationMatch[0] : null;
      return { index: index + 1, title, artist, artistLink, duration };
    });

    return { playlist };
  },

  playSongByTitle: (title) => {
    const btn = document.querySelector(`button[aria-label*="${title}"]`);
    if (!btn) throw new Error(`Song button not found for: ${title}`);
    btn.click();
    return { ok: true, title };
  },

  shuffle: () => {
    const btn = document.querySelector('button[data-testid="shuffle_button"]');
    if (!btn) throw new Error('Shuffle button not found');
    btn.click();
    const isActive = btn.getAttribute('data-active') !== null || btn.classList.contains('active');
    return { ok: true, shuffleEnabled: isActive };
  },

  getShuffleStatus: () => {
    const btn = document.querySelector('button[data-testid="shuffle_button"]');
    if (!btn) return { shuffle: null };
    return { shuffle: btn.getAttribute('data-active') !== null || btn.classList.contains('active') };
  },

  seek: (percent) => {
    const slider = document.querySelector('[data-testid="progress_bar"] input, input[aria-label*="timeline" i]');
    if (!slider) throw new Error('Progress slider not found');
    const nativeInputSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    nativeInputSetter.call(slider, percent);
    slider.dispatchEvent(new Event('input', { bubbles: true }));
    slider.dispatchEvent(new Event('change', { bubbles: true }));
    return { ok: true, percent };
  },
  
  like: () => {
    const btn = document.querySelector('button[data-testid="like_button"], button[aria-label*="like" i], button[aria-label*="Love" i]');
    if (!btn) throw new Error('Like button not found');
    btn.click();
    return { ok: true };
  },
};

// ─── Command Handler ───────────────────────────────────────────────────────────

async function handleCommand(msg) {
  const { id, action, params = {} } = msg;

  const reply = (data) => send({ type: 'response', id, action, ...data });

  try {
    let result;

    switch (action) {
      case 'play':        result = await execInDeezer(DeezerScripts.play); break;
      case 'pause':       result = await execInDeezer(DeezerScripts.pause); break;
      case 'next':        result = await execInDeezer(DeezerScripts.next); break;
      case 'previous':    result = await execInDeezer(DeezerScripts.previous); break;
      case 'shuffle':     result = await execInDeezer(DeezerScripts.shuffle); break;
      case 'like':        result = await execInDeezer(DeezerScripts.like); break;

      case 'get_repeat':
        result = await execInDeezer(DeezerScripts.getRepeatStatus);
        break;
      case 'set_repeat':
        result = await execInDeezer(DeezerScripts.setRepeat, [params.status]);
        break;

      case 'get_volume':
        result = await execInDeezer(DeezerScripts.getVolume);
        break;
      case 'set_volume':
        result = await execInDeezer(DeezerScripts.setVolume, [params.level]);
        break;

      case 'get_track':
        result = await execInDeezer(DeezerScripts.getCurrentTrack);
        break;

      case 'get_playlist':
        result = await execInDeezer(DeezerScripts.getPlaylist);
        break;

      case 'play_song':
        result = await execInDeezer(DeezerScripts.playSongByTitle, [params.title]);
        break;

      case 'seek':
        result = await execInDeezer(DeezerScripts.seek, [params.percent]);
        break;

      case 'get_shuffle':
        result = await execInDeezer(DeezerScripts.getShuffleStatus);
        break;

      case 'ping':
        result = { pong: true, ts: Date.now() };
        break;

      default:
        return reply({ error: `Unknown action: ${action}` });
    }

    reply({ result });
  } catch (err) {
    console.error(`[Bridge] Command "${action}" failed:`, err);
    reply({ error: err.message || String(err) });
  }
}

// ─── Init ─────────────────────────────────────────────────────────────────────

async function init() {
  await loadConfig();
  connect();
}

// Re-connect when config changes
chrome.storage.onChanged.addListener((changes) => {
  if ('wsPort' in changes || 'wsHost' in changes) {
    loadConfig().then(() => {
      if (ws) ws.close();
    });
  }
});

// Keep service worker alive via alarms
chrome.alarms.create('keepAlive', { periodInMinutes: 0.4 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === 'keepAlive') {
    if (connectionState !== 'connected') connect();
  }
});

// Handle popup messages
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg.type === 'quick_command') {
    handleCommand({ id: 'popup-' + Date.now(), action: msg.action, params: msg.params || {} })
      .then(() => sendResponse({ ok: true }))
      .catch(e => sendResponse({ error: e.message }));
    return true; // async
  }
  if (msg.type === 'reconnect') {
    if (ws) ws.close();
    setTimeout(connect, 500);
    sendResponse({ ok: true });
  }
});

init();
