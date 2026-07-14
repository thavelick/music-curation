#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#     "mutagen>=1.47.0",
#     "requests>=2.31.0",
# ]
# ///

"""Derive album genres from MusicBrainz and (optionally) write them to tags.

TheAudioDB's single hand-entered genre is coarse and often wrong (it files The
Black Keys under "Indie"). MusicBrainz instead exposes a *controlled* genre
vocabulary that is community-voted, so each genre carries a vote count you can
use both to pick the best label and to gauge confidence. Because the whole
library is already keyed on MusicBrainz IDs, this needs no new matching step --
just the release-group MBID already in your tags.

For each album this:
  1. reads the release-group MBID from the tracks (deriving it from the release
     ID when the release-group tag is absent);
  2. fetches that release group's genres from MusicBrainz, taking the
     highest-voted one; and
  3. falls back to the *artist's* top genre when the release group has none.

By default it only prints a report -- the proposed genre (with vote counts)
next to each album's current GENRE tag -- so you can eyeball quality before
changing anything. Pass --apply to actually write the GENRE tag.

All MusicBrainz responses are cached in .genre_cache.json at the library root
(keyed by MBID) so repeat runs don't re-query. --refresh ignores the cache and
re-fetches; --no-cache disables it entirely.

MusicBrainz asks for at most one request per second; every HTTP call here is
spaced by RATE_LIMIT_DELAY and backs off on 503/Retry-After.

Usage:
  scripts/fetch_genres.py                                   # report on whole library
  scripts/fetch_genres.py ~/Music/curated/"The Black Keys"  # one artist
  scripts/fetch_genres.py ~/Music/curated/INXS/Kick         # one album
  scripts/fetch_genres.py --apply                           # write GENRE tags
  scripts/fetch_genres.py --apply --overwrite               # replace non-empty genres too

Environment overrides:
  MUSIC_DIR   music library root (default: ~/Music); default scan root is
              $MUSIC_DIR/curated
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

import requests
from mutagen import File as MutagenFile

MUSIC_DIR = Path(os.environ.get("MUSIC_DIR", Path.home() / "Music")).expanduser()
DEFAULT_ROOT = MUSIC_DIR / "curated"

AUDIO_GLOBS = ["*.flac", "*.mp3", "*.m4a", "*.ogg", "*.opus"]
CACHE_NAME = ".genre_cache.json"  # MusicBrainz genre cache, written at the library root
MB_BASE = "https://musicbrainz.org/ws/2"
# MusicBrainz requires a descriptive User-Agent with contact info.
USER_AGENT = "music-curation-fetch-genres/1.0 (https://github.com/thavelick/music-curation)"
RATE_LIMIT_DELAY = 1.1  # seconds between *every* HTTP call; MB asks for <=1/second
REQUEST_TIMEOUT = 30  # seconds per HTTP call
MAX_RETRIES = 5  # attempts per request when MB returns 503 / 5xx
BACKOFF_BASE = 2.0  # seconds; exponential backoff base for retries


class RateLimitedSession:
    """A requests.Session wrapper that keeps MusicBrainz calls under 1/second.

    MusicBrainz enforces a rolling one-request-per-second limit and returns 503
    when exceeded. We keep a minimum gap between *every* call and, on a 503/5xx,
    honour a Retry-After header (or exponential backoff) before retrying.
    """

    def __init__(self, delay=RATE_LIMIT_DELAY):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.delay = delay
        self._last_call = 0.0

    def get_json(self, url, params) -> Optional[dict]:
        for attempt in range(MAX_RETRIES):
            wait = self.delay - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            try:
                r = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as e:
                print(f"  [wait   ] request error ({e}); retrying", flush=True)
                time.sleep(BACKOFF_BASE ** attempt)
                continue
            finally:
                self._last_call = time.monotonic()
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
            if r.status_code == 503 or r.status_code >= 500:
                retry_after = r.headers.get("Retry-After")
                sleep_for = float(retry_after) if retry_after and retry_after.isdigit() else BACKOFF_BASE ** attempt
                print(f"  [wait   ] HTTP {r.status_code}, backing off {sleep_for:.0f}s", flush=True)
                time.sleep(sleep_for)
                continue
            print(f"  [warn   ] MusicBrainz returned HTTP {r.status_code}")
            return None
        return None  # exhausted retries


# --- tag reading (cross-format, mirrors fetch_nfo.py) -----------------------


def album_audio_files(album_dir: Path):
    """All audio files in an album, including tracks under Disc N/ subfolders."""
    files = []
    for pattern in AUDIO_GLOBS:
        files.extend(album_dir.glob(pattern))
        files.extend(album_dir.glob(f"Disc */{pattern}"))
    return sorted(files)


def has_audio(directory: Path) -> bool:
    return bool(album_audio_files(directory))


def get_tag(audio_file: Path, name: str) -> Optional[str]:
    """Read a Vorbis-named tag across formats; returns str or None.

    `name` is the lowercase Vorbis key (e.g. "musicbrainz_releasegroupid");
    this maps it to the right ID3 TXXX frame / MP4 atom for MP3 and M4A.
    """
    try:
        audio = MutagenFile(audio_file)
    except Exception:
        return None
    if audio is None or audio.tags is None:
        return None

    suffix = audio_file.suffix.lower()
    try:
        if suffix == ".mp3":
            desc = {
                "musicbrainz_albumartistid": "MusicBrainz Album Artist Id",
                "musicbrainz_artistid": "MusicBrainz Artist Id",
                "musicbrainz_releasegroupid": "MusicBrainz Release Group Id",
                "musicbrainz_albumid": "MusicBrainz Album Id",
                "genre": None,  # handled via easy interface below
            }.get(name, name)
            if name == "genre":
                frame = audio.tags.get("TCON")
                return str(frame.text[0]) if frame and frame.text else None
            frame = audio.tags.get(f"TXXX:{desc}")
            return str(frame.text[0]) if frame else None
        if suffix == ".m4a":
            if name == "genre":
                val = audio.tags.get("\xa9gen")
                return str(val[0]) if val else None
            key = f"----:com.apple.iTunes:{name}"
            val = audio.tags.get(key)
            if not val:
                return None
            raw = val[0]
            return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        # FLAC, OGG, OPUS: Vorbis comments, case-insensitive keys.
        val = audio.tags.get(name)
        return str(val[0]) if val else None
    except Exception:
        return None


def find_tag(files, name: str) -> Optional[str]:
    """Return the first non-empty value of `name` across a list of files."""
    for f in files:
        val = get_tag(f, name)
        if val:
            return val
    return None


# --- MusicBrainz genre lookups (cached) -------------------------------------


def top_genres(session, cache, entity: str, mbid: str, refresh: bool):
    """Return this entity's genres as a [ (name, votes), ... ] list, votes-desc.

    entity is "release-group" or "artist". Results (including empty ones) are
    cached under "<entity>:<mbid>" so we never re-query the same MBID.
    """
    key = f"{entity}:{mbid}"
    if not refresh and key in cache:
        return [tuple(g) for g in cache[key]]
    data = session.get_json(f"{MB_BASE}/{entity}/{mbid}", {"inc": "genres", "fmt": "json"})
    genres = sorted(
        ((g["name"], g.get("count", 0)) for g in (data or {}).get("genres", [])),
        key=lambda g: -g[1],
    )
    cache[key] = [list(g) for g in genres]
    return genres


def release_group_for_release(session, cache, release_id: str, refresh: bool) -> Optional[str]:
    """Resolve a release ID to its release-group MBID (cached)."""
    key = f"rel2rg:{release_id}"
    if not refresh and key in cache:
        return cache[key]
    data = session.get_json(f"{MB_BASE}/release/{release_id}", {"inc": "release-groups", "fmt": "json"})
    rg = (data or {}).get("release-group", {}).get("id")
    cache[key] = rg
    return rg


# --- genre canonicalisation -------------------------------------------------


def canonical(name: str) -> str:
    """Title-case a MusicBrainz genre to match the library's tag style.

    MusicBrainz genres are lowercase ("blues rock", "hip-hop"); title-casing
    yields "Blues Rock", "Hip-Hop", "Trip Hop". A few well-known initialisms
    are fixed up so they don't come out as "Uk" / "R&b".
    """
    titled = name.title()
    fixups = {"Uk": "UK", "Us": "US", "R&B": "R&B", "Edm": "EDM", "Dj": "DJ"}
    return " ".join(fixups.get(w) or w for w in titled.split(" "))


# --- library walking --------------------------------------------------------


def collect_albums(path: Path):
    """Yield album directories under `path` (an album, artist, or library root)."""
    if has_audio(path):
        yield path
        return
    children = sorted(d for d in path.iterdir() if d.is_dir())
    if any(has_audio(c) for c in children):  # artist folder
        for c in children:
            if has_audio(c):
                yield c
        return
    for artist in children:  # library root
        yield from collect_albums(artist)


def resolve_genre(session, cache, files, refresh):
    """Return (canonical_genre, votes, source, rg_genres) for an album's files.

    Prefer the release group's top-voted genre; fall back to the artist's.
    source is "release-group", "artist", or None when nothing is found.
    """
    rg_mbid = find_tag(files, "musicbrainz_releasegroupid")
    if not rg_mbid:
        release_id = find_tag(files, "musicbrainz_albumid")
        if release_id:
            rg_mbid = release_group_for_release(session, cache, release_id, refresh)

    rg_genres = top_genres(session, cache, "release-group", rg_mbid, refresh) if rg_mbid else []
    if rg_genres:
        name, votes = rg_genres[0]
        return canonical(name), votes, "release-group", rg_genres

    artist_mbid = find_tag(files, "musicbrainz_albumartistid") or find_tag(files, "musicbrainz_artistid")
    artist_genres = top_genres(session, cache, "artist", artist_mbid, refresh) if artist_mbid else []
    if artist_genres:
        name, votes = artist_genres[0]
        return canonical(name), votes, "artist", artist_genres

    return None, 0, None, rg_genres


def write_genre(files, genre: str) -> int:
    """Set GENRE on every audio file via mutagen's easy interface. Returns count."""
    written = 0
    for f in files:
        try:
            audio = MutagenFile(f, easy=True)
            if audio is None:
                continue
            audio["genre"] = genre
            audio.save()
            written += 1
        except Exception as e:
            print(f"    ! failed to tag {f.name}: {e}", flush=True)
    return written


def load_cache(cache_path: Path) -> dict:
    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_cache(cache_path: Path, cache: dict) -> None:
    cache_path.write_text(json.dumps(cache, indent=0, sort_keys=True), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="Derive album genres from MusicBrainz.")
    ap.add_argument("paths", nargs="*", help="album/artist dirs (default: --root)")
    ap.add_argument("--root", default=str(DEFAULT_ROOT), help=f"library root (default: {DEFAULT_ROOT})")
    ap.add_argument("--apply", action="store_true", help="write the GENRE tag (default: report only)")
    ap.add_argument("--overwrite", action="store_true", help="with --apply, replace non-empty existing genres")
    ap.add_argument("--refresh", action="store_true", help="ignore cached MusicBrainz results and re-fetch")
    ap.add_argument("--no-cache", action="store_true", help="don't read or write the genre cache")
    args = ap.parse_args()

    def progress(msg):
        print(msg, flush=True)

    roots = [Path(p) for p in (args.paths or [args.root])]
    albums = []
    for root in roots:
        if not root.exists():
            print(f"!! path does not exist: {root}", file=sys.stderr)
            continue
        albums.extend(collect_albums(root))
    albums = sorted(set(albums))
    if not albums:
        sys.exit("No album folders found.")

    session = RateLimitedSession()
    cache_path = Path(args.root) / CACHE_NAME
    cache = {} if args.no_cache else load_cache(cache_path)

    counts = {"release-group": 0, "artist": 0, "none": 0, "changed": 0, "kept": 0, "tagged": 0}
    progress(f"Resolving genres for {len(albums)} album(s)...\n")
    try:
        for album in albums:
            files = album_audio_files(album)
            old = find_tag(files, "genre")
            genre, votes, source, rg_genres = resolve_genre(session, cache, files, args.refresh)
            counts[source or "none"] += 1

            label = f"{album.parent.name} / {album.name}"
            if genre is None:
                progress(f"  [none   ] {label}  (old: {old or '(unset)'})  -- no MusicBrainz genre")
                continue

            detail = ", ".join(f"{n}={v}" for n, v in rg_genres[:3]) if source == "release-group" else "artist-level"
            change = "same" if old == genre else f"{old or '(unset)'} -> {genre}"
            progress(f"  [{source:13.13}] {label}\n              {change}  ({votes} votes; {detail})")

            if old == genre:
                counts["kept"] += 1
            else:
                counts["changed"] += 1

            if args.apply and old != genre and (args.overwrite or not old):
                n = write_genre(files, genre)
                counts["tagged"] += 1
                progress(f"              wrote GENRE={genre} to {n} file(s)")
            elif args.apply and old and not args.overwrite and old != genre:
                progress(f"              kept existing '{old}' (use --overwrite to replace)")
    finally:
        if not args.no_cache:
            save_cache(cache_path, cache)

    print("\n=== Summary ===")
    print(f"  from release-group : {counts['release-group']}")
    print(f"  from artist        : {counts['artist']}")
    print(f"  no genre found     : {counts['none']}")
    print(f"  would change       : {counts['changed']}")
    print(f"  already correct    : {counts['kept']}")
    if args.apply:
        print(f"  albums tagged      : {counts['tagged']}")
    else:
        print("  (report only; pass --apply to write GENRE tags)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
