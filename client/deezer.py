#!/usr/bin/env python3

# File: client/deezer.py
# Author: Hadi Cahyadi <cumulus13@gmail.com>
# Date: 2026-04-18
# Description: Deezer Controller - CLI
# License: MIT

"""
Deezer Controller - CLI
=======================
Drop-in replacement for the old pychrome-based CLI.
All original flags preserved + new ones added.

Usage:
    python deezer.py --play
    python deezer.py --next
    python deezer.py --pause
    python deezer.py --current-playlist
    python deezer.py --play-song 3
    python deezer.py --repeat all
    python deezer.py --volume 80
    python deezer.py --track
"""

import sys
import os
import argparse
import re
import time

# Try colored output (optional, falls back gracefully)
try:
    from make_colors import make_colors
except ImportError:
    def make_colors(text, *args, **kwargs):
        return str(text)

# Add parent dir for local import
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from client import DeezerSync, DeezerBridgeError, ServerNotReachable, CommandError

# ─── Display helpers ──────────────────────────────────────────────────────────

def print_track(track: dict):
    if not track or not track.get('title'):
        print(make_colors("No track info available.", 'lw', 'r'))
        return
    print()
    print(f"  {make_colors('♪', 'lm')} {make_colors(track.get('title', '—'), 'ly', 'b')}")
    print(f"    {make_colors('Artist:', 'lc')} {track.get('artist', '—')}")
    if track.get('position') and track.get('duration'):
        print(f"    {make_colors('Time:  ', 'lc')} {track.get('position')} / {track.get('duration')}")
    print()

def print_playlist(playlist: list):
    if not playlist:
        print(make_colors("Playlist is empty.", 'lw', 'r'))
        return
    print()
    for song in playlist:
        n = str(song.get('index', '?')).zfill(2)
        title = song.get('title', '—')
        artist = song.get('artist', '—')
        dur = song.get('duration', '—')
        print(
            f"  {make_colors(n, 'lc')}. "
            f"{make_colors(title, 'ly')} - "
            f"{make_colors(artist, 'lg')} "
            f"[{make_colors(dur, 'lm')}]"
        )
    print()

def ok(msg):
    print(f"  {make_colors('✓', 'lg')} {msg}")

def err(msg):
    print(f"  {make_colors('✕', 'lr')} {msg}")

# ─── CLI ──────────────────────────────────────────────────────────────────────

def build_parser():
    parser = argparse.ArgumentParser(
        prog='deezer',
        description='Deezer Controller Bridge - CLI v2.0',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python deezer.py -x                 Play
  python deezer.py -s                 Pause
  python deezer.py -n                 Next track
  python deezer.py -p                 Previous track
  python deezer.py -X "Song Title"    Play specific song
  python deezer.py -X 3               Play song #3 from queue
  python deezer.py -r all             Set repeat all
  python deezer.py -r one             Set repeat one
  python deezer.py -r off             Turn off repeat
  python deezer.py -l                 Show current playlist (interactive)
  python deezer.py --track            Show current track info
  python deezer.py --volume 80        Set volume to 80
  python deezer.py --seek 50          Seek to 50%
  python deezer.py --shuffle          Toggle shuffle
  python deezer.py --like             Like current track
  python deezer.py --ping             Ping relay server
        """,
    )

    parser.add_argument('-x', '--play',             action='store_true',  help='Play / Resume')
    parser.add_argument('-X', '--play-song',         metavar='TITLE_OR_N', help='Play song by title or queue number')
    parser.add_argument('-s', '--pause',             action='store_true',  help='Pause')
    parser.add_argument('-n', '--next',              action='store_true',  help='Next track')
    parser.add_argument('-p', '--previous',          action='store_true',  help='Previous track')
    parser.add_argument('-r', '--repeat',            metavar='MODE',       help='Repeat: all | one | off | 0 | 1 | 2')
    parser.add_argument('-l', '--current-playlist',  action='store_true',  help='Show current playlist (interactive)')
    parser.add_argument('--track',                   action='store_true',  help='Show current track info')
    parser.add_argument('--shuffle',                 action='store_true',  help='Toggle shuffle')
    parser.add_argument('--like',                    action='store_true',  help='Like current track')
    parser.add_argument('--volume',                  metavar='0-100', type=float, help='Set volume (0-100)')
    parser.add_argument('--seek',                    metavar='0-100', type=float, help='Seek to position percent')
    parser.add_argument('--ping',                    action='store_true',  help='Ping relay server')
    parser.add_argument('--port',                    default=8765, type=int, help='Relay server port (default: 8765)')
    parser.add_argument('--host',                    default='localhost',   help='Relay server host (default: localhost)')
    parser.add_argument('--token',                   default=None,         help='Auth token (if server uses --token)')

    return parser

def main():
    parser = build_parser()

    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    args = parser.parse_args()

    # ── Connect ───────────────────────────────────────────────────────────────
    try:
        dz = DeezerSync(host=args.host, port=args.port, token=args.token)
    except ServerNotReachable as e:
        err(str(e))
        print()
        print(make_colors("  Make sure the relay server is running:", 'lw'))
        print(make_colors("    python server/server.py", 'ly'))
        sys.exit(1)

    # ── Dispatch ──────────────────────────────────────────────────────────────
    try:
        if args.play:
            dz.play()
            ok("Playing")

        elif args.pause:
            dz.pause()
            ok("Paused")

        elif args.next:
            dz.next()
            ok("Skipped to next track")
            time.sleep(0.5)
            print_track(dz.get_current_track())

        elif args.previous:
            dz.previous()
            ok("Went to previous track")
            time.sleep(0.5)
            print_track(dz.get_current_track())

        elif args.track:
            track = dz.get_current_track()
            print_track(track)

        elif args.shuffle:
            result = dz.shuffle()
            state = "enabled" if result.get('shuffleEnabled') else "toggled"
            ok(f"Shuffle {state}")

        elif args.like:
            dz.like()
            ok("Liked current track ♥")

        elif args.ping:
            ms = dz.ping()
            ok(f"Pong! Round-trip: {ms}ms")

        elif args.volume is not None:
            dz.set_volume(args.volume)
            ok(f"Volume set to {args.volume}")

        elif args.seek is not None:
            dz.seek(args.seek)
            ok(f"Seeked to {args.seek}%")

        elif args.play_song:
            arg = args.play_song
            if arg.isdigit():
                dz.play_song_by_index(int(arg))
                ok(f"Playing track #{arg}")
            else:
                dz.play_song(arg)
                ok(f"Playing: {arg}")

        elif args.repeat:
            raw = args.repeat.strip().lower()
            mode_map = {'0': 'off', '1': 'all', '2': 'one', 'all': 'all', 'one': 'one', 'off': 'off'}
            mode = mode_map.get(raw)
            if not mode:
                err(f"Invalid repeat mode: {raw!r}. Use: all | one | off")
                sys.exit(1)
            dz.set_repeat(mode)
            ok(f"Repeat set to: {mode}")

        elif args.current_playlist:
            playlist = dz.get_playlist()
            print_playlist(playlist)

            if playlist:
                q = input(
                    make_colors("  Select number to play", 'lw', 'bl') +
                    ", " + make_colors("s = resume", 'b', 'ly') +
                    ", " + make_colors("Enter = cancel", 'lw') + ": "
                ).strip()

                if q.isdigit() and 1 <= int(q) <= len(playlist):
                    dz.play_song_by_index(int(q))
                    ok(f"Playing: {playlist[int(q)-1]['title']}")
                elif q.lower() == 's':
                    dz.play()
                    ok("Playing")

    except CommandError as e:
        err(f"Command failed: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        pass
    finally:
        dz.close()

if __name__ == '__main__':
    main()
