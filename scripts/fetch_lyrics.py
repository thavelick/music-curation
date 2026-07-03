#!/usr/bin/env python3
# /// script
# dependencies = [
#     "mutagen>=1.47.0",
#     "requests>=2.31.0",
# ]
# ///

"""Fetch synced (.lrc) lyrics from LRCLIB and write them next to each track.

For every audio file under the given path, this reads the track's ARTIST /
TITLE / ALBUM tags and duration, looks the song up in the free LRCLIB database
(https://lrclib.net -- no API key), and writes a sidecar lyric file whose
basename matches the track:

    01 Guns In The Sky.flac
    01 Guns In The Sky.lrc      <- written here

Jellyfin attaches a lyric sidecar to a track when their basenames match, so
naming the .lrc after the audio file is what makes it show up after a scan.

Synced (timestamped) lyrics are written as .lrc. When only plain lyrics exist,
nothing is written unless --plain is given, in which case they go to .txt.
Existing sidecars are left alone unless --overwrite is given.

LRCLIB matches on artist/title/album *and duration*, so accurate tags and
durations give the best hit rate. A track whose duration is off by more than a
couple seconds from the reference may miss.

Usage:
  scripts/fetch_lyrics.py ~/Music/curated/INXS/Kick      # one album
  scripts/fetch_lyrics.py ~/Music/curated/INXS           # one artist
  scripts/fetch_lyrics.py                                 # whole curated library
  scripts/fetch_lyrics.py --plain                         # also accept unsynced (.txt)
  scripts/fetch_lyrics.py --overwrite                     # replace existing sidecars

Environment overrides:
  MUSIC_DIR   music library root  (default: ~/Music); default scan root is
              $MUSIC_DIR/curated
"""

import argparse
import os
import sys
import time
from pathlib import Path

import requests
from mutagen import File as MutagenFile

MUSIC_DIR = Path(os.environ.get("MUSIC_DIR", Path.home() / "Music")).expanduser()
DEFAULT_ROOT = MUSIC_DIR / "curated"

AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav"}
LRCLIB_GET = "https://lrclib.net/api/get"
LRCLIB_SEARCH = "https://lrclib.net/api/search"
# LRCLIB asks clients to identify themselves with a User-Agent linking to the app.
USER_AGENT = "music-curation-fetch-lyrics/1.0 (https://github.com/thavelick/music-curation)"
RATE_LIMIT_DELAY = 0.5  # seconds between API calls, to be polite to LRCLIB
SYNCED_DURATION_TOLERANCE = 3  # seconds; prefer a synced match within this of our track


def first_tag(audio, *names):
    """Return the first present tag value across candidate tag names."""
    for name in names:
        val = audio.get(name)
        if val:
            return str(val[0]) if isinstance(val, list) else str(val)
    return None


def track_metadata(path):
    """Read (artist, title, album, duration_seconds) from an audio file."""
    audio = MutagenFile(path, easy=True)
    if audio is None:
        return None
    artist = first_tag(audio, "artist", "albumartist")
    title = first_tag(audio, "title")
    album = first_tag(audio, "album")
    duration = int(round(audio.info.length)) if audio.info else None
    if not (artist and title):
        return None
    return artist, title, album, duration


def lrclib_lookup(session, artist, title, album, duration):
    """Look up lyrics on LRCLIB. Returns the best match dict, or None.

    The exact /get endpoint allows a small duration tolerance, so it can return
    a *plain-only* record even when a synced version of the same song exists at
    a slightly different duration. So: take the /get hit, but if it lacks synced
    lyrics, also run the broader /search and prefer a synced candidate whose
    duration is close to ours. That stops a nearer-but-plain entry from leaving
    the track unsynced when a synced version is only a second or two off.
    """
    params = {"artist_name": artist, "track_name": title}
    if album:
        params["album_name"] = album
    if duration is not None:
        params["duration"] = duration
    r = session.get(LRCLIB_GET, params=params, timeout=20)
    exact = None
    if r.status_code == 200:
        exact = r.json()
        if exact.get("syncedLyrics"):
            return exact
    elif r.status_code != 404:
        r.raise_for_status()

    # Either /get missed, or it returned plain-only -- search for a synced match.
    r = session.get(LRCLIB_SEARCH, params={"artist_name": artist, "track_name": title}, timeout=20)
    r.raise_for_status()
    results = r.json()

    if duration is not None:
        def dur_diff(c):
            return abs((c.get("duration") or 0) - duration)

        synced_near = [c for c in results if c.get("syncedLyrics") and dur_diff(c) <= SYNCED_DURATION_TOLERANCE]
        if synced_near:
            return min(synced_near, key=dur_diff)

    # No synced match found; keep the exact plain hit if we had one, else the
    # closest search result (by duration when known), else nothing.
    if exact is not None:
        return exact
    if not results:
        return None
    if duration is None:
        return results[0]
    return min(results, key=lambda c: abs((c.get("duration") or 0) - duration))


def write_sidecar(path, text, suffix):
    sidecar = path.with_suffix(suffix)
    sidecar.write_text(text, encoding="utf-8")
    return sidecar


def find_audio_files(paths):
    files = []
    for base in paths:
        base = Path(base)
        if not base.exists():
            print(f"!! path does not exist: {base}", file=sys.stderr)
            continue
        if base.is_file():
            if base.suffix.lower() in AUDIO_EXTS:
                files.append(base)
            continue
        for dirpath, _, filenames in os.walk(base):
            for name in filenames:
                if Path(name).suffix.lower() in AUDIO_EXTS:
                    files.append(Path(dirpath) / name)
    return sorted(files)


def main():
    ap = argparse.ArgumentParser(description="Fetch synced lyrics from LRCLIB.")
    ap.add_argument("paths", nargs="*", help="album/artist dirs or files (default: --root)")
    ap.add_argument("--root", default=str(DEFAULT_ROOT), help=f"library root (default: {DEFAULT_ROOT})")
    ap.add_argument("--plain", action="store_true", help="also write unsynced lyrics as .txt when no synced lyrics exist")
    ap.add_argument("--overwrite", action="store_true", help="replace existing .lrc/.txt sidecars")
    args = ap.parse_args()

    # flush each line so progress is visible when output is piped/redirected
    # (e.g. run in the background) rather than withheld until the script exits.
    def progress(msg):
        print(msg, flush=True)

    files = find_audio_files(args.paths or [args.root])
    if not files:
        sys.exit("No audio files found.")

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    counts = {"synced": 0, "plain": 0, "skipped": 0, "missing": 0, "error": 0}
    progress(f"Fetching lyrics for {len(files)} track(s)...\n")
    for path in files:
        label = path.name
        lrc = path.with_suffix(".lrc")
        txt = path.with_suffix(".txt")
        if not args.overwrite and (lrc.exists() or txt.exists()):
            counts["skipped"] += 1
            progress(f"  [skip   ] {label} (sidecar exists)")
            continue

        meta = track_metadata(path)
        if meta is None:
            counts["error"] += 1
            progress(f"  [error  ] {label} (missing artist/title tags)")
            continue
        artist, title, album, duration = meta

        try:
            match = lrclib_lookup(session, artist, title, album, duration)
        except requests.RequestException as e:
            counts["error"] += 1
            progress(f"  [error  ] {label} ({e})")
            continue
        finally:
            time.sleep(RATE_LIMIT_DELAY)

        synced = (match or {}).get("syncedLyrics")
        plain = (match or {}).get("plainLyrics")
        if synced:
            written = write_sidecar(path, synced, ".lrc")
            counts["synced"] += 1
            progress(f"  [synced ] {label} -> {written.name}")
        elif plain and args.plain:
            written = write_sidecar(path, plain, ".txt")
            counts["plain"] += 1
            progress(f"  [plain  ] {label} -> {written.name}")
        else:
            counts["missing"] += 1
            note = "only plain available, use --plain" if plain else "no lyrics found"
            progress(f"  [miss   ] {label} ({note})")

    print("\n=== Summary ===")
    for k in ("synced", "plain", "skipped", "missing", "error"):
        print(f"  {k:8}: {counts[k]}")


if __name__ == "__main__":
    main()
