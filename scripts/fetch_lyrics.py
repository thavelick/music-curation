#!/usr/bin/env -S uv run --script
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

Negative results are remembered in a cache file (.lyrics_cache.json at the
library root) so repeat runs don't re-query tracks LRCLIB has nothing for. The
cache distinguishes a "full_miss" (no lyrics at all) from "plain_only" (plain
exists but no synced), so a later --plain run still fetches the plain-only
tracks while skipping the full misses. --overwrite ignores the cache and
re-queries everything; --no-cache disables reading and writing it entirely.

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
import json
import os
import sys
import time
from pathlib import Path

import requests
from mutagen import File as MutagenFile

MUSIC_DIR = Path(os.environ.get("MUSIC_DIR", Path.home() / "Music")).expanduser()
DEFAULT_ROOT = MUSIC_DIR / "curated"

AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav"}
CACHE_NAME = ".lyrics_cache.json"  # negative-result cache, written at the library root
LRCLIB_GET = "https://lrclib.net/api/get"
LRCLIB_SEARCH = "https://lrclib.net/api/search"
# LRCLIB asks clients to identify themselves with a User-Agent linking to the app.
USER_AGENT = "music-curation-fetch-lyrics/1.0 (https://github.com/thavelick/music-curation)"
RATE_LIMIT_DELAY = 0.5  # min seconds between *every* HTTP call, to be polite to LRCLIB
REQUEST_TIMEOUT = 40  # seconds per HTTP call; some titles return large/slow responses
MAX_RETRIES = 5  # attempts per request when LRCLIB returns 429 / 5xx
BACKOFF_BASE = 2.0  # seconds; exponential backoff base for retries
SYNCED_DURATION_TOLERANCE = 3  # seconds; prefer a synced match within this of our track


class RateLimitedSession:
    """A requests.Session wrapper that spaces out and retries LRCLIB calls.

    Two things matter for being a good LRCLIB citizen:
      1. A minimum gap between *every* HTTP call (a single track can fire both a
         /get and a /search, so a per-track sleep alone is not enough).
      2. Honouring 429 (Too Many Requests) / 5xx by backing off and retrying,
         respecting a Retry-After header when the server sends one.
    """

    def __init__(self, delay=RATE_LIMIT_DELAY):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.delay = delay
        self._last_call = 0.0

    def get(self, url, **kwargs):
        r = None
        for attempt in range(MAX_RETRIES):
            wait = self.delay - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            r = self.session.get(url, **kwargs)
            self._last_call = time.monotonic()
            if r.status_code == 429 or r.status_code >= 500:
                retry_after = r.headers.get("Retry-After")
                sleep_for = float(retry_after) if retry_after and retry_after.isdigit() else BACKOFF_BASE ** attempt
                print(f"  [wait   ] HTTP {r.status_code}, backing off {sleep_for:.0f}s", flush=True)
                time.sleep(sleep_for)
                continue
            return r
        return r  # exhausted retries; return the last response for the caller to handle


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
    r = session.get(LRCLIB_GET, params=params, timeout=REQUEST_TIMEOUT)
    exact = None
    if r.status_code == 200:
        exact = r.json()
        if exact.get("syncedLyrics"):
            return exact
    elif r.status_code != 404:
        r.raise_for_status()

    # Either /get missed, or it returned plain-only -- search for a synced match.
    r = session.get(LRCLIB_SEARCH, params={"artist_name": artist, "track_name": title}, timeout=REQUEST_TIMEOUT)
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


def load_cache(cache_path):
    """Load the negative-result cache: {absolute audio path: "full_miss"|"plain_only"}."""
    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_cache(cache_path, cache):
    cache_path.write_text(json.dumps(cache, indent=0, sort_keys=True), encoding="utf-8")


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
    ap.add_argument("--overwrite", action="store_true", help="replace existing .lrc/.txt sidecars and ignore the miss cache")
    ap.add_argument("--no-cache", action="store_true", help="don't read or write the negative-result cache")
    args = ap.parse_args()

    # flush each line so progress is visible when output is piped/redirected
    # (e.g. run in the background) rather than withheld until the script exits.
    def progress(msg):
        print(msg, flush=True)

    files = find_audio_files(args.paths or [args.root])
    if not files:
        sys.exit("No audio files found.")

    session = RateLimitedSession()

    cache_path = Path(args.root) / CACHE_NAME
    cache = {} if args.no_cache else load_cache(cache_path)
    cache_dirty = False

    counts = {"synced": 0, "plain": 0, "skipped": 0, "cached": 0, "missing": 0, "error": 0}
    progress(f"Fetching lyrics for {len(files)} track(s)...\n")
    try:
        for path in files:
            label = path.name
            key = str(path)
            lrc = path.with_suffix(".lrc")
            txt = path.with_suffix(".txt")
            # A synced .lrc means we're done. A plain .txt does *not* count -- we
            # still try for synced lyrics unless --plain says plain is acceptable.
            already = lrc.exists() or (txt.exists() and args.plain)
            if not args.overwrite and already:
                counts["skipped"] += 1
                progress(f"  [skip   ] {label} (sidecar exists)")
                continue

            # Skip tracks we've already looked up and know LRCLIB has nothing new
            # for: a full miss always, and a plain-only miss unless --plain wants
            # the plain lyrics we haven't written yet.
            cached = cache.get(key)
            if not args.overwrite and (cached == "full_miss" or (cached == "plain_only" and not args.plain)):
                counts["cached"] += 1
                progress(f"  [cached ] {label} (known {cached})")
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

            synced = (match or {}).get("syncedLyrics")
            plain = (match or {}).get("plainLyrics")
            if synced:
                written = write_sidecar(path, synced, ".lrc")
                if txt.exists():
                    txt.unlink()  # drop the redundant plain sidecar now that we have synced
                cache.pop(key, None)  # resolved -- no longer a miss
                cache_dirty = True
                counts["synced"] += 1
                progress(f"  [synced ] {label} -> {written.name}")
            elif plain and args.plain:
                written = write_sidecar(path, plain, ".txt")
                # Wrote plain, but there's still no synced version: record it so a
                # future non-plain run skips it instead of re-querying.
                cache[key] = "plain_only"
                cache_dirty = True
                counts["plain"] += 1
                progress(f"  [plain  ] {label} -> {written.name}")
            else:
                cache[key] = "plain_only" if plain else "full_miss"
                cache_dirty = True
                counts["missing"] += 1
                note = "only plain available, use --plain" if plain else "no lyrics found"
                progress(f"  [miss   ] {label} ({note})")
    finally:
        # Persist whatever we learned even if interrupted mid-run.
        if cache_dirty and not args.no_cache:
            save_cache(cache_path, cache)

    print("\n=== Summary ===")
    for k in ("synced", "plain", "skipped", "cached", "missing", "error"):
        print(f"  {k:8}: {counts[k]}")

    if counts["error"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
