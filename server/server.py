#!/usr/bin/env python3

# File: server/server.py
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Date: 2026-04-18
# Description: Deezer Controller Bridge - WebSocket Relay Server
# License: MIT

"""
Deezer Controller Bridge - WebSocket Relay Server
==================================================
Relays commands between Python clients and the Chrome extension.
Replaces the deprecated Chrome remote debugging port (9222).

Usage:
    python server.py [--host HOST] [--port PORT] [--token TOKEN] [--log-level LEVEL]

Architecture:
    Python Client ──► Relay Server ◄──► Chrome Extension ──► Deezer Tab
"""

import asyncio
import json
import logging
import argparse
import signal
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional, Dict, Set, Any
import websockets
from websockets.server import WebSocketServerProtocol

# ─── Logging Setup ────────────────────────────────────────────────────────────

def setup_logging(level: str = 'INFO') -> logging.Logger:
    fmt = '%(asctime)s  %(levelname)-8s  %(name)s  %(message)s'
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=fmt,
        datefmt='%H:%M:%S',
    )
    return logging.getLogger('deezer.bridge')

log = logging.getLogger('deezer.bridge')

# ─── Config ───────────────────────────────────────────────────────────────────

@dataclass
class ServerConfig:
    host: str = '127.0.0.1'
    port: int = 8765
    token: Optional[str] = None          # Optional bearer token for auth
    command_timeout: float = 10.0        # Seconds to wait for extension response
    max_clients: int = 20
    ping_interval: int = 20
    ping_timeout: int = 10

# ─── Client Registry ──────────────────────────────────────────────────────────

@dataclass
class Client:
    ws: WebSocketServerProtocol
    client_id: str
    client_type: str = 'unknown'   # 'extension' | 'python' | 'unknown'
    version: str = ''
    connected_at: float = field(default_factory=time.time)
    commands_sent: int = 0
    last_seen: float = field(default_factory=time.time)

    def touch(self):
        self.last_seen = time.time()

class Registry:
    def __init__(self):
        self._clients: Dict[str, Client] = {}
        self._extension: Optional[Client] = None
        self._pending: Dict[str, asyncio.Future] = {}   # command_id -> Future

    def register(self, client: Client):
        self._clients[client.client_id] = client
        if client.client_type == 'extension':
            self._extension = client
            log.info(f'[Registry] Extension connected: {client.client_id}')

    def unregister(self, client_id: str):
        c = self._clients.pop(client_id, None)
        if c and c.client_type == 'extension' and self._extension and self._extension.client_id == client_id:
            self._extension = None
            log.warning('[Registry] Extension disconnected!')
            # Fail all pending futures
            for fid, fut in list(self._pending.items()):
                if not fut.done():
                    fut.set_exception(RuntimeError('Extension disconnected'))
            self._pending.clear()

    @property
    def extension(self) -> Optional[Client]:
        return self._extension

    @property
    def has_extension(self) -> bool:
        return self._extension is not None

    def add_pending(self, command_id: str, fut: asyncio.Future):
        self._pending[command_id] = fut

    def resolve_pending(self, command_id: str, data: Any):
        fut = self._pending.pop(command_id, None)
        if fut and not fut.done():
            fut.set_result(data)

    def fail_pending(self, command_id: str, error: str):
        fut = self._pending.pop(command_id, None)
        if fut and not fut.done():
            fut.set_exception(RuntimeError(error))

    def stats(self) -> dict:
        return {
            'total_clients': len(self._clients),
            'has_extension': self.has_extension,
            'pending_commands': len(self._pending),
            'clients': [
                {
                    'id': c.client_id,
                    'type': c.client_type,
                    'version': c.version,
                    'connected_for': round(time.time() - c.connected_at, 1),
                }
                for c in self._clients.values()
            ],
        }


registry = Registry()

# ─── Message Helpers ──────────────────────────────────────────────────────────

async def send_json(ws: WebSocketServerProtocol, data: dict):
    try:
        await ws.send(json.dumps(data))
    except Exception as e:
        log.debug(f'send_json failed: {e}')

# ─── Authentication ───────────────────────────────────────────────────────────

def check_auth(headers, config: ServerConfig) -> bool:
    if not config.token:
        return True
    auth = headers.get('Authorization', '')
    return auth == f'Bearer {config.token}'

# ─── Connection Handler ───────────────────────────────────────────────────────

async def handler(ws: WebSocketServerProtocol, config: ServerConfig):
    # Auth check via query param or header
    if config.token:
        token_param = ws.request.headers.get('Authorization', '').replace('Bearer ', '')
        if token_param != config.token:
            await ws.close(4001, 'Unauthorized')
            return

    if len(registry._clients) >= config.max_clients:
        await ws.close(4029, 'Too many clients')
        return

    client_id = str(uuid.uuid4())[:8]
    client = Client(ws=ws, client_id=client_id)
    registry.register(client)

    remote = ws.remote_address
    log.info(f'[+] Client connected: {client_id} from {remote}')

    try:
        async for raw in ws:
            client.touch()

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await send_json(ws, {'type': 'error', 'error': 'Invalid JSON'})
                continue

            msg_type = msg.get('type', '')

            # ── Hello handshake ───────────────────────────────────────────────
            if msg_type == 'hello':
                client.client_type = msg.get('client', 'unknown')
                client.version = msg.get('version', '')
                registry.register(client)  # re-register with updated type
                log.info(f'[{client_id}] Hello as {client.client_type} v{client.version}')
                await send_json(ws, {
                    'type': 'hello_ack',
                    'server_version': '2.0.0',
                    'client_id': client_id,
                    'extension_present': registry.has_extension,
                })
                continue

            # ── Heartbeat ─────────────────────────────────────────────────────
            if msg_type in ('ping', 'pong'):
                if msg_type == 'ping':
                    await send_json(ws, {'type': 'pong', 'ts': msg.get('ts')})
                continue

            # ── Command from Python client ────────────────────────────────────
            if msg_type == 'command':
                if not registry.has_extension:
                    await send_json(ws, {
                        'type': 'response',
                        'id': msg.get('id'),
                        'error': 'Chrome extension not connected. Install and enable the Deezer Bridge extension.',
                    })
                    continue

                command_id = msg.get('id') or str(uuid.uuid4())[:8]
                msg['id'] = command_id

                log.debug(f'[{client_id}] CMD {msg.get("action")} (id={command_id})')
                client.commands_sent += 1

                # Create future and forward to extension
                loop = asyncio.get_event_loop()
                fut = loop.create_future()
                registry.add_pending(command_id, fut)

                await send_json(registry.extension.ws, msg)

                try:
                    result = await asyncio.wait_for(fut, timeout=config.command_timeout)
                    await send_json(ws, result)
                except asyncio.TimeoutError:
                    registry.fail_pending(command_id, 'Command timed out')
                    await send_json(ws, {
                        'type': 'response',
                        'id': command_id,
                        'error': f'Timeout: extension did not respond within {config.command_timeout}s',
                    })
                except Exception as e:
                    await send_json(ws, {
                        'type': 'response',
                        'id': command_id,
                        'error': str(e),
                    })
                continue

            # ── Response from extension → resolve pending future ──────────────
            if msg_type == 'response':
                cmd_id = msg.get('id')
                log.debug(f'[{client_id}] RESP id={cmd_id}')
                registry.resolve_pending(cmd_id, msg)
                continue

            # ── Stats request ─────────────────────────────────────────────────
            if msg_type == 'stats':
                await send_json(ws, {'type': 'stats', 'data': registry.stats()})
                continue

            log.debug(f'[{client_id}] Unknown message type: {msg_type}')

    except websockets.exceptions.ConnectionClosed as e:
        log.info(f'[-] Client {client_id} disconnected: code={e.code}')
    except Exception as e:
        log.error(f'[!] Client {client_id} error: {e}', exc_info=True)
    finally:
        registry.unregister(client_id)
        log.info(f'[Registry] {len(registry._clients)} client(s) remaining')

# ─── Server Entry Point ───────────────────────────────────────────────────────

async def run_server(config: ServerConfig):
    stop = asyncio.Event()

    def _signal_handler():
        log.info('Shutdown signal received')
        stop.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except (NotImplementedError, OSError):
            pass  # Windows

    async def _handler(ws):
        await handler(ws, config)

    log.info(f'╔══════════════════════════════════════════╗')
    log.info(f'║     Deezer Controller Bridge v2.0.0      ║')
    log.info(f'╠══════════════════════════════════════════╣')
    log.info(f'║  Relay server: ws://{config.host}:{config.port}{"":>7}║')
    log.info(f'║  Auth token:   {"enabled" if config.token else "disabled":>10}{"":>16}║')
    log.info(f'║  Max clients:  {config.max_clients:>10}{"":>16}║')
    log.info(f'╚══════════════════════════════════════════╝')
    log.info('Waiting for Chrome extension connection...')

    async with websockets.serve(
        _handler,
        config.host,
        config.port,
        ping_interval=config.ping_interval,
        ping_timeout=config.ping_timeout,
    ):
        await stop.wait()

    log.info('Server stopped.')

# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Deezer Controller Bridge - WebSocket Relay Server',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--host',      default='127.0.0.1', help='Bind host (default: 127.0.0.1)')
    parser.add_argument('--port',      default=8765, type=int, help='Bind port (default: 8765)')
    parser.add_argument('--token',     default=None, help='Optional auth token for security')
    parser.add_argument('--log-level', default='INFO', choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
                        help='Log level (default: INFO)')
    parser.add_argument('--timeout',   default=10.0, type=float, help='Command timeout in seconds')
    args = parser.parse_args()

    setup_logging(args.log_level)

    config = ServerConfig(
        host=args.host,
        port=args.port,
        token=args.token,
        command_timeout=args.timeout,
    )

    try:
        asyncio.run(run_server(config))
    except KeyboardInterrupt:
        pass

if __name__ == '__main__':
    main()
