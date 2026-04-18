#!/usr/bin/env python3

# File: client/client.py
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Date: 2026-04-18
# Description: Deezer Controller Bridge - Python Client Library
# License: MIT

"""
Deezer Controller Bridge - Python Client Library
=================================================
Fixed version. Changes vs original:

  BUG 1 - ConcurrencyError (crash on startup):
    Original connect() called ws.recv() to read the hello-ack WHILE
    _listen() was already running as a background task also calling ws.recv().
    websockets 14+ raises ConcurrencyError when two coroutines call recv()
    concurrently on the same connection.
    Fix: the hello-ack is now routed through the normal _listen() → Future
    mechanism instead of a raw ws.recv() call.

  BUG 2 - "Task was destroyed but it is pending" / "Event loop is closed":
    DeezerSync ran connect() (which spawned background tasks) inside
    run_until_complete(), then returned. The loop was still alive but the
    background tasks (_listen, keepalive) were left pending. Every subsequent
    run_until_complete() call on the same loop then hit the closed-loop error.
    Fix: DeezerSync keeps the loop running in a dedicated background thread.
    All coroutines are submitted via run_coroutine_threadsafe(). The loop is
    only stopped/closed in close().

  BUG 3 - No reconnect after internet drop:
    If the relay server dropped the connection the client raised and never
    recovered.
    Fix: _listen() detects closure and schedules a reconnect with exponential
    back-off (up to 30 s). Pending futures are rejected immediately so callers
    don't hang.

Requires:
    pip install websockets
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

try:
    import websockets
except ImportError:
    raise ImportError("websockets not installed.  Run: pip install websockets")

log = logging.getLogger("deezer.client")


# ── Exceptions ────────────────────────────────────────────────────────────────

class DeezerBridgeError(Exception):
    """Base error for Deezer Bridge client."""

class ExtensionNotConnected(DeezerBridgeError):
    """Chrome extension is not connected to the relay server."""

class CommandError(DeezerBridgeError):
    """Command execution failed in the extension."""

class ServerNotReachable(DeezerBridgeError):
    """Cannot connect to the relay server."""


# ── Async client ──────────────────────────────────────────────────────────────

class DeezerClient:
    """
    Async WebSocket client for the Deezer Bridge relay server.

    Example:
        async with DeezerClient(host='localhost', port=8765) as dz:
            await dz.play()
            track = await dz.get_current_track()
    """

    # Hello-ack uses this synthetic id so it flows through the normal
    # Future pipeline without needing a separate ws.recv() call.
    _HELLO_ID = "__hello__"

    def __init__(
        self,
        host: str = "localhost",
        port: int = 8765,
        token: Optional[str] = None,
        timeout: float = 10.0,
        reconnect_delay: float = 2.0,
        max_reconnect_delay: float = 30.0,
    ):
        self.url = f"ws://{host}:{port}"
        self.token = token
        self.timeout = timeout
        self.reconnect_delay = reconnect_delay
        self.max_reconnect_delay = max_reconnect_delay

        self._ws: Optional[Any] = None
        self._pending: Dict[str, asyncio.Future] = {}
        self._listener_task: Optional[asyncio.Task] = None
        self._ready = False
        self._closing = False

    # ── Connection lifecycle ──────────────────────────────────────────────────

    async def connect(self) -> None:
        self._closing = False
        await self._do_connect()

    async def _do_connect(self) -> None:
        """Open WS, start the single listener, send hello, await ack."""
        headers: dict = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        try:
            self._ws = await websockets.connect(
                self.url,
                additional_headers=headers,
                ping_interval=20,
                ping_timeout=10,
                open_timeout=10,
            )
        except (OSError, ConnectionRefusedError, TimeoutError) as exc:
            raise ServerNotReachable(
                f"Cannot connect to relay server at {self.url}. "
                f"Is server.py running?  Error: {exc}"
            ) from exc

        # Cancel any stale listener before creating a new one.
        await self._cancel_listener()
        self._listener_task = asyncio.create_task(
            self._listen(), name="deezer-listener"
        )

        # Register the hello-ack future BEFORE sending — so _listen() can
        # resolve it as soon as the ack message arrives.
        loop = asyncio.get_event_loop()
        hello_fut: asyncio.Future = loop.create_future()
        self._pending[self._HELLO_ID] = hello_fut

        await self._raw_send(
            {"type": "hello", "client": "python", "version": "2.0.0"}
        )

        try:
            ack = await asyncio.wait_for(hello_fut, timeout=self.timeout)
        except asyncio.TimeoutError:
            raise ServerNotReachable(
                f"Relay server at {self.url} did not acknowledge hello "
                f"within {self.timeout}s."
            )

        if not ack.get("extension_present"):
            log.warning(
                "Chrome extension not connected to relay server. "
                "Commands will fail until the extension connects."
            )

        self._ready = True
        log.info("Connected to relay server at %s", self.url)

    async def disconnect(self) -> None:
        self._closing = True
        self._ready = False
        await self._cancel_listener()
        self._reject_pending(ServerNotReachable("Client disconnected."))
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def _cancel_listener(self) -> None:
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
            try:
                await self._listener_task
            except (asyncio.CancelledError, Exception):
                pass
        self._listener_task = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *_):
        await self.disconnect()

    # ── Single drain loop (the ONLY place ws.recv() is called) ───────────────

    async def _listen(self) -> None:
        """
        Drains the WebSocket. Routes every message to the matching Future.
        On any connection loss, rejects pending futures and schedules reconnect.
        """
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    log.debug("Non-JSON from server: %r", raw)
                    continue

                msg_type = msg.get("type", "")

                if msg_type == "ping":
                    try:
                        await self._raw_send({"type": "pong", "ts": msg.get("ts")})
                    except Exception:
                        pass

                elif msg_type in ("hello_ack", "connected", "ack"):
                    # Relay server acknowledged our hello
                    fut = self._pending.pop(self._HELLO_ID, None)
                    if fut and not fut.done():
                        fut.set_result(msg)

                elif msg_type == "response":
                    cmd_id = msg.get("id")
                    fut = self._pending.pop(cmd_id, None)
                    if fut and not fut.done():
                        if "error" in msg:
                            fut.set_exception(CommandError(msg["error"]))
                        else:
                            fut.set_result(msg.get("result", {}))

        except asyncio.CancelledError:
            # Intentional shutdown — do not reconnect.
            return

        except Exception as exc:
            log.warning("WebSocket connection lost: %s", exc)

        # Connection dropped unexpectedly.
        self._ready = False
        self._reject_pending(
            ServerNotReachable("Connection to relay server lost.")
        )
        if not self._closing:
            asyncio.create_task(
                self._reconnect_loop(), name="deezer-reconnect"
            )

    # ── Reconnect with exponential back-off ───────────────────────────────────

    async def _reconnect_loop(self) -> None:
        delay = self.reconnect_delay
        attempt = 0
        while not self._closing:
            attempt += 1
            log.info("Reconnect attempt %d in %.1fs …", attempt, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, self.max_reconnect_delay)
            try:
                await self._do_connect()
                log.info("Reconnected after %d attempt(s).", attempt)
                return
            except Exception as exc:
                log.warning("Reconnect attempt %d failed: %s", attempt, exc)

    # ── Messaging ─────────────────────────────────────────────────────────────

    async def _raw_send(self, data: dict) -> None:
        """Send without checking _ready (used during handshake)."""
        if not self._ws:
            raise ServerNotReachable("WebSocket is not open.")
        try:
            await self._ws.send(json.dumps(data))
        except Exception as exc:
            raise ServerNotReachable(f"Send failed: {exc}") from exc

    async def _send(self, data: dict) -> None:
        if not self._ready:
            raise ServerNotReachable(
                "Not connected to relay server — waiting for reconnection."
            )
        await self._raw_send(data)

    def _reject_pending(self, exc: Exception) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def _command(self, action: str, **params) -> dict:
        cmd_id = str(uuid.uuid4())[:8]
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[cmd_id] = fut

        try:
            await self._send(
                {"type": "command", "id": cmd_id, "action": action, "params": params}
            )
        except Exception:
            self._pending.pop(cmd_id, None)
            if not fut.done():
                fut.cancel()
            raise

        try:
            return await asyncio.wait_for(fut, timeout=self.timeout)
        except asyncio.TimeoutError:
            self._pending.pop(cmd_id, None)
            raise CommandError(f'Command "{action}" timed out after {self.timeout}s')

    # ── Public API ────────────────────────────────────────────────────────────

    async def play(self) -> dict:                   return await self._command("play")
    async def pause(self) -> dict:                  return await self._command("pause")
    async def next(self) -> dict:                   return await self._command("next")
    async def previous(self) -> dict:               return await self._command("previous")
    async def shuffle(self) -> dict:                return await self._command("shuffle")
    async def like(self) -> dict:                   return await self._command("like")
    async def seek(self, percent: float) -> dict:   return await self._command("seek", percent=percent)
    async def set_volume(self, level: float) -> dict: return await self._command("set_volume", level=level)
    async def play_song(self, title: str) -> dict:  return await self._command("play_song", title=title)

    async def get_repeat_status(self) -> str:
        result = await self._command("get_repeat")
        return result.get("status", "unknown")

    async def set_repeat(self, status: str) -> dict:
        if status not in ("all", "one", "off"):
            raise ValueError(f"Invalid repeat status: {status!r}. Use 'all', 'one', or 'off'")
        return await self._command("set_repeat", status=status)

    async def get_volume(self) -> Optional[float]:
        result = await self._command("get_volume")
        return result.get("volume")

    async def get_current_track(self) -> dict:
        return await self._command("get_track")

    async def get_playlist(self) -> List[dict]:
        result = await self._command("get_playlist")
        return result.get("playlist", [])

    async def play_song_by_index(self, index: int) -> dict:
        playlist = await self.get_playlist()
        if not playlist:
            raise CommandError("Playlist is empty or could not be retrieved")
        if index < 1 or index > len(playlist):
            raise ValueError(f"Index {index} out of range (1-{len(playlist)})")
        return await self.play_song(playlist[index - 1]["title"])

    async def get_shuffle_status(self) -> Optional[bool]:
        result = await self._command("get_shuffle")
        return result.get("shuffle")

    async def ping(self) -> float:
        start = time.monotonic()
        await self._command("ping")
        return round((time.monotonic() - start) * 1000, 2)


# ── Sync wrapper ──────────────────────────────────────────────────────────────

class DeezerSync:
    """
    Synchronous wrapper around DeezerClient.

    Runs the asyncio event loop in a private daemon thread so background
    tasks (_listen, reconnect) stay alive between calls — unlike the original
    which used run_until_complete() and destroyed all tasks on return.

    Example:
        dz = DeezerSync()
        dz.play()
        print(dz.get_current_track())
        dz.close()

        with DeezerSync() as dz:
            dz.play()
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 8765,
        token: Optional[str] = None,
        timeout: float = 10.0,
    ):
        self._client = DeezerClient(
            host=host, port=port, token=token, timeout=timeout
        )

        # The event loop lives in a daemon thread so it never blocks the
        # calling thread and is never accidentally closed between calls.
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
            name="deezer-async-loop",
        )
        self._thread.start()

        # Block until connected (or raise ServerNotReachable).
        future = asyncio.run_coroutine_threadsafe(
            self._client.connect(), self._loop
        )
        try:
            future.result(timeout=15)
        except Exception:
            self._loop.call_soon_threadsafe(self._loop.stop)
            raise

    def _run(self, coro):
        """Submit coro to background loop, block until result."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=self._client.timeout + 2)

    def close(self) -> None:
        try:
            asyncio.run_coroutine_threadsafe(
                self._client.disconnect(), self._loop
            ).result(timeout=5)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=3)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # ── Sync wrappers ─────────────────────────────────────────────────────────

    def play(self):                  return self._run(self._client.play())
    def pause(self):                 return self._run(self._client.pause())
    def next(self):                  return self._run(self._client.next())
    def previous(self):              return self._run(self._client.previous())
    def shuffle(self):               return self._run(self._client.shuffle())
    def like(self):                  return self._run(self._client.like())
    def get_repeat_status(self):     return self._run(self._client.get_repeat_status())
    def set_repeat(self, s):         return self._run(self._client.set_repeat(s))
    def get_volume(self):            return self._run(self._client.get_volume())
    def set_volume(self, lvl):       return self._run(self._client.set_volume(lvl))
    def get_current_track(self):     return self._run(self._client.get_current_track())
    def get_playlist(self):          return self._run(self._client.get_playlist())
    def play_song(self, title):      return self._run(self._client.play_song(title))
    def play_song_by_index(self, i): return self._run(self._client.play_song_by_index(i))
    def seek(self, pct):             return self._run(self._client.seek(pct))
    def get_shuffle_status(self):    return self._run(self._client.get_shuffle_status())
    def ping(self):                  return self._run(self._client.ping())
