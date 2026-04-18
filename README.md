# 🎵 Deezer Controller Bridge

Control Deezer from Python — **without Chrome's deprecated remote debugging port (9222)**.

This project to control deezer on chrome tab in newer Chrome versions. Instead of Chrome exposing a port *to you*, and since debugging port (default: 9222) has been deprecated, **you run a tiny relay server and the Chrome extension connects to it** — which Chrome's security model happily allows.

---

## Architecture

```
┌─────────────────┐        WebSocket        ┌──────────────────┐       chrome.scripting      ┌─────────────┐
│  Python Client  │ ──────────────────────► │  Relay Server    │ ◄─────────────────────────  │  Chrome Ext │ ──► Deezer Tab
│  (deezer.py /   │ ◄────────────────────── │  (server.py)     │ ─────────────────────────►  │             │
│   client.py)    │        responses         │  port 8765       │       responses              └─────────────┘
└─────────────────┘                          └──────────────────┘
```

**Why this works:** Chrome extensions can initiate *outbound* WebSocket connections. Your relay server sits in the middle, routing commands from Python to the extension and responses back.

---

## Quick Start

### 1. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 2. Start the relay server

```bash
python server/server.py
```

You should see:
```
╔══════════════════════════════════════════╗
║     Deezer Controller Bridge v2.0.0      ║
╠══════════════════════════════════════════╣
║  Relay server: ws://127.0.0.1:8765       ║
║  Auth token:   disabled                  ║
╚══════════════════════════════════════════╝
Waiting for Chrome extension connection...
```

### 3. Install the Chrome Extension

1. Open Chrome → `chrome://extensions`
2. Enable **Developer mode** (top right toggle)
3. Click **Load unpacked**
4. Select the `extension/` folder from this project
5. The extension icon appears — click it to verify it shows **CONNECTED** ✅

### 4. Open Deezer

Make sure `deezer.com` is open in a Chrome tab.

### 5. Control Deezer!

```bash
# Play / Pause
python client/deezer.py --play
python client/deezer.py --pause

# Navigation
python client/deezer.py --next
python client/deezer.py --previous

# Show current track
python client/deezer.py --track

# Show playlist & pick interactively
python client/deezer.py --current-playlist

# Play track #3 from queue
python client/deezer.py --play-song 3

# Set repeat
python client/deezer.py --repeat all
python client/deezer.py --repeat one
python client/deezer.py --repeat off

# Volume & seeking
python client/deezer.py --volume 80
python client/deezer.py --seek 50

# Shuffle & like
python client/deezer.py --shuffle
python client/deezer.py --like
```

---

## CLI Reference

```
usage: deezer [-x] [-X TITLE_OR_N] [-s] [-n] [-p] [-r MODE] [-l]
              [--track] [--shuffle] [--like] [--volume 0-100]
              [--seek 0-100] [--ping] [--port PORT] [--host HOST]
              [--token TOKEN]

Flags (same as original):
  -x, --play              Play / Resume
  -X, --play-song N       Play song by queue number or title
  -s, --pause             Pause
  -n, --next              Next track
  -p, --previous          Previous track
  -r, --repeat MODE       Repeat: all | one | off | 1 | 2 | 0
  -l, --current-playlist  Interactive playlist selector

New flags:
  --track                 Show current track info
  --shuffle               Toggle shuffle
  --like                  Like current track
  --volume N              Set volume (0-100)
  --seek N                Seek to position (0-100%)
  --ping                  Check relay server latency

Connection:
  --host HOST             Relay server host (default: localhost)
  --port PORT             Relay server port (default: 8765)
  --token TOKEN           Auth token (if server started with --token)
```

---

## Python API

Use `DeezerSync` for simple scripts (blocking), or `DeezerClient` for async code:

### Sync (simplest)

```python
from client.client import DeezerSync

with DeezerSync() as dz:
    dz.play()
    dz.next()

    track = dz.get_current_track()
    print(f"Now playing: {track['title']} by {track['artist']}")

    playlist = dz.get_playlist()
    for song in playlist:
        print(f"{song['index']}. {song['title']} - {song['artist']}")

    dz.set_repeat('all')
    dz.set_volume(75)
```

### Async

```python
import asyncio
from client.client import DeezerClient

async def main():
    async with DeezerClient() as dz:
        await dz.play()
        track = await dz.get_current_track()
        print(track)

asyncio.run(main())
```

### Available methods

| Method | Description |
|--------|-------------|
| `play()` | Resume playback |
| `pause()` | Pause playback |
| `next()` | Next track |
| `previous()` | Previous track |
| `shuffle()` | Toggle shuffle |
| `like()` | Like current track |
| `get_current_track()` | Dict: title, artist, cover, duration, position, progress |
| `get_playlist()` | List of queue items |
| `play_song(title)` | Play by title |
| `play_song_by_index(n)` | Play by queue position (1-based) |
| `get_repeat_status()` | Returns 'all' \| 'one' \| 'off' |
| `set_repeat(status)` | Set repeat: 'all' \| 'one' \| 'off' |
| `get_volume()` | Current volume |
| `set_volume(level)` | Set volume |
| `seek(percent)` | Seek to position (0.0–100.0) |
| `get_shuffle_status()` | True/False |
| `ping()` | Round-trip latency in ms |

---

## Server Options

```bash
python server/server.py --help

  --host HOST         Bind host (default: 127.0.0.1)
  --port PORT         Bind port (default: 8765)
  --token TOKEN       Optional auth bearer token
  --log-level LEVEL   DEBUG | INFO | WARNING | ERROR
  --timeout SECS      Command timeout (default: 10s)
```

### Run as a background service (Linux systemd)

Create `/etc/systemd/system/deezer-bridge.service`:

```ini
[Unit]
Description=Deezer Controller Bridge
After=network.target

[Service]
ExecStart=/usr/bin/python3 /path/to/deezer-controller/server/server.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now deezer-bridge
```

---

## Extension Config

Click the extension icon to:
- See **connection status** (green = relay server found)
- Change relay server **host/port**
- Use **quick controls** directly from the popup
- View **activity log**

---

## Migrating from v1 (pychrome)

Old code:
```python
from deezer import Deezer
Deezer.play()
Deezer.next()
Deezer.get_current_playlist()
```

New code:
```python
from client.client import DeezerSync
with DeezerSync() as dz:
    dz.play()
    dz.next()
    dz.get_playlist()
```

The method names are the same (or very close). The only change is: **start the relay server first**, and **install the extension**.

---

## Troubleshooting

**Extension shows DISCONNECTED**
→ Start `server/server.py` first. Extension reconnects automatically.

**"No Deezer tab found"**
→ Open `deezer.com` in Chrome. The extension needs an active Deezer tab.

**Commands time out**
→ Try reloading the Deezer tab. Some buttons may not be in the DOM until playback starts.

**Port 8765 already in use**
→ Use `--port 8766` on the server and match with `--port 8766` on the client.

---

## Project Structure

```
deezer-controller/
├── extension/              ← Chrome Extension (load this folder)
│   ├── manifest.json
│   ├── background.js       ← Service worker: WebSocket ↔ Deezer DOM
│   ├── popup.html          ← Extension popup UI
│   └── popup.js
├── server/
│   └── server.py           ← WebSocket relay server (run this first)
├── client/
│   ├── client.py           ← Python async/sync client library
│   └── deezer.py           ← CLI entry point
├── requirements.txt
└── README.md
```

---

## License

MIT — free to use, modify, and distribute.

## 👤 Author
        
[Hadi Cahyadi](mailto:cumulus13@gmail.com)
    

[![Buy Me a Coffee](https://www.buymeacoffee.com/assets/img/custom_images/orange_img.png)](https://www.buymeacoffee.com/cumulus13)

[![Donate via Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/cumulus13)
 
[Support me on Patreon](https://www.patreon.com/cumulus13)