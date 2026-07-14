#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#     "musicbrainzngs>=0.7.1",
#     "mutagen>=1.47.0",
#     "requests>=2.31.0",
# ]
# ///

"""Write Kodi/Jellyfin NFO sidecars (artist.nfo / album.nfo) from TheAudioDB.

These carry the fields that have no home in audio tags -- artist biography,
formed year, album review, and mood/style -- so Jellyfin can show bios and
browse/mix by mood once its NFO metadata reader is enabled. Data is scraped
from TheAudioDB (https://www.theaudiodb.com), keyed off the MusicBrainz IDs
already embedded in your tracks (see fetch_artist_image.py, which uses the same
API). Genre/year still live in your tags -- NFO only adds what tags can't hold.

For each artist folder it writes `artist.nfo` (next to folder.jpg); for each
album folder it writes `album.nfo` (next to cover.jpg). Existing files are left
alone unless --overwrite is given.

Usage:
  scripts/fetch_nfo.py ~/Music/curated/"The Chemical Brothers"/"Push the Button"
  scripts/fetch_nfo.py ~/Music/curated/"The Chemical Brothers"   # artist + its albums
  scripts/fetch_nfo.py                                           # whole curated library

Environment overrides:
  MUSIC_DIR   music library root (default: ~/Music); default scan root is
              $MUSIC_DIR/curated
"""

import argparse
import os
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

import musicbrainzngs
import requests
from mutagen import File as MutagenFile

# TheAudioDB's free key allows 30 requests/minute (429 above that). We make two
# calls per artist (artist + album), so space them well over 2s apart to stay
# comfortably under 30/min across a whole-library run.
RATE_LIMIT_DELAY = 2.5  # Seconds between API calls
AUDIODB_API_KEY = "123"  # Default public API key
AUDIODB_BASE = "https://www.theaudiodb.com/api/v1/json"
AUDIO_GLOBS = ["*.flac", "*.mp3", "*.m4a", "*.ogg", "*.opus"]


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
    """Read a tag by lowercase Vorbis name across formats; returns str or None.

    `name` is the Vorbis-comment key (e.g. "musicbrainz_albumartistid"); this
    maps it to the right ID3 TXXX frame / MP4 atom for MP3 and M4A files.
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
            # MusicBrainz IDs live in TXXX frames keyed by human-readable desc.
            desc = {
                "musicbrainz_albumartistid": "MusicBrainz Album Artist Id",
                "musicbrainz_artistid": "MusicBrainz Artist Id",
                "musicbrainz_releasegroupid": "MusicBrainz Release Group Id",
                "musicbrainz_albumid": "MusicBrainz Album Id",
            }.get(name)
            if desc is None:
                return None
            frame = audio.tags.get(f"TXXX:{desc}")
            return str(frame.text[0]) if frame else None
        if suffix == ".m4a":
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


def release_group_from_release(release_mbid: str) -> Optional[str]:
    """Look up a release's release-group MBID via MusicBrainz.

    TheAudioDB keys albums off the release *group*, but most tracks only carry
    the release ID (MUSICBRAINZ_ALBUMID). When the release-group tag is missing,
    resolve it best-effort from the release ID; return None on any failure.
    """
    try:
        time.sleep(RATE_LIMIT_DELAY)
        result = musicbrainzngs.get_release_by_id(release_mbid, includes=["release-groups"])
        return result["release"]["release-group"]["id"]
    except Exception as e:
        print(f"  Warning: MusicBrainz release-group lookup failed: {e}")
        return None


def audiodb_get(endpoint: str, mbid: str, api_key: str) -> Optional[dict]:
    """Query a TheAudioDB *-mb.php endpoint by MusicBrainz ID; return raw JSON.

    On a 429 (rate limit), waits a minute — the window the free key resets on —
    and retries once before giving up.
    """
    url = f"{AUDIODB_BASE}/{api_key}/{endpoint}?i={mbid}"
    for attempt in range(2):
        try:
            time.sleep(RATE_LIMIT_DELAY)
            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429 and attempt == 0:
                print("  Rate limited (429); waiting 60s before retrying...")
                time.sleep(60)
                continue
            print(f"  Warning: TheAudioDB returned HTTP {resp.status_code}")
            return None
        except Exception as e:
            print(f"  Warning: TheAudioDB query error: {e}")
            return None
    return None


def paragraphs(text: Optional[str]) -> Optional[str]:
    """Normalize prose to blank-line-separated paragraphs.

    TheAudioDB inconsistently separates paragraphs with a single newline or a
    blank line. Jellyfin's overview renderer collapses a lone newline into a
    space (like HTML), so single-newline bios show as one blob. Promote every
    newline to a blank-line gap so both styles render identically. Already
    blank-line-separated text is unchanged (the empty lines drop out and are
    re-added).
    """
    if text is None:
        return None
    parts = [p.strip() for p in text.splitlines() if p.strip()]
    return "\n\n".join(parts)


def add(parent: ET.Element, tag: str, value) -> None:
    """Append <tag>value</tag> only when value is non-empty."""
    if value is None:
        return
    text = str(value).strip()
    if text:
        ET.SubElement(parent, tag).text = text


def write_nfo(root: ET.Element, path: Path, dry_run: bool) -> None:
    """Pretty-print an NFO tree to `path` with an XML declaration."""
    ET.indent(root)
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        + ET.tostring(root, encoding="unicode")
        + "\n"
    )
    if dry_run:
        print(f"  [DRY RUN] Would write {path.name}:")
        for line in xml.splitlines():
            print(f"    {line}")
        return
    path.write_text(xml, encoding="utf-8")
    print(f"  ✓ Wrote {path.name}")


def biography_field(data: dict, lang: str) -> Optional[str]:
    """Prefer the requested language's biography, then English, then any."""
    suffix = "" if lang == "en" else lang.upper()
    for key in (f"strBiography{suffix}", "strBiography", "strBiographyEN"):
        if data.get(key):
            return data[key]
    return None


def process_artist(artist_dir: Path, api_key: str, lang: str, overwrite: bool, dry_run: bool) -> None:
    """Write artist.nfo for an artist folder using its MusicBrainz artist ID."""
    nfo_path = artist_dir / "artist.nfo"
    if nfo_path.exists() and not overwrite:
        print(f"  ✓ artist.nfo exists, skipping (use --overwrite to replace)")
        return

    files = album_audio_files(artist_dir) or list(
        f for p in AUDIO_GLOBS for f in artist_dir.glob(f"*/{p}")
    )
    mbid = find_tag(files, "musicbrainz_albumartistid") or find_tag(files, "musicbrainz_artistid")
    if not mbid:
        print(f"  ✗ No MusicBrainz artist ID in tags; skipping artist.nfo")
        return

    print(f"  Querying TheAudioDB for artist {mbid}...")
    data = audiodb_get("artist-mb.php", mbid, api_key)
    artists = (data or {}).get("artists")
    if not artists:
        print(f"  ✗ Artist not found in TheAudioDB")
        return
    a = artists[0]

    root = ET.Element("artist")
    add(root, "name", a.get("strArtist") or artist_dir.name)
    add(root, "musicBrainzArtistID", mbid)
    add(root, "biography", paragraphs(biography_field(a, lang)))
    add(root, "formed", a.get("intFormedYear"))
    add(root, "genre", a.get("strGenre"))
    add(root, "style", a.get("strStyle"))
    add(root, "mood", a.get("strMood"))
    write_nfo(root, nfo_path, dry_run)


def process_album(album_dir: Path, artist_name: str, api_key: str, overwrite: bool, dry_run: bool) -> None:
    """Write album.nfo for an album folder using its release-group MBID."""
    nfo_path = album_dir / "album.nfo"
    if nfo_path.exists() and not overwrite:
        print(f"  ✓ album.nfo exists, skipping (use --overwrite to replace)")
        return

    files = album_audio_files(album_dir)
    # TheAudioDB's album-mb.php keys off the release *group*, not the release.
    # Prefer the tag; else derive it from the release ID (MUSICBRAINZ_ALBUMID).
    mbid = find_tag(files, "musicbrainz_releasegroupid")
    if not mbid:
        release_id = find_tag(files, "musicbrainz_albumid")
        if release_id:
            mbid = release_group_from_release(release_id)
    if not mbid:
        print(f"  ✗ No MusicBrainz release group for this album; skipping album.nfo")
        return

    print(f"  Querying TheAudioDB for album {mbid}...")
    data = audiodb_get("album-mb.php", mbid, api_key)
    albums = (data or {}).get("album")
    if not albums:
        print(f"  ✗ Album not found in TheAudioDB")
        return
    al = albums[0]

    root = ET.Element("album")
    add(root, "title", al.get("strAlbum") or album_dir.name)
    add(root, "artist", al.get("strArtist") or artist_name)
    add(root, "musicBrainzReleaseGroupID", mbid)
    add(root, "musicBrainzAlbumID", find_tag(files, "musicbrainz_albumid"))
    add(root, "year", al.get("intYearReleased"))
    add(root, "genre", al.get("strGenre"))
    add(root, "style", al.get("strStyle"))
    add(root, "mood", al.get("strMood"))
    add(root, "review", paragraphs(al.get("strDescription")))
    add(root, "rating", al.get("intScore"))
    write_nfo(root, nfo_path, dry_run)


def process_tree(
    path: Path, api_key: str, lang: str, overwrite: bool, dry_run: bool, include_artist: bool
) -> None:
    """Dispatch on whether `path` is an album, an artist folder, or a root.

    With `include_artist`, a single album folder also gets its parent
    artist.nfo written -- but no sibling albums are touched.
    """
    if has_audio(path):  # a single album folder
        if include_artist:
            print(f"\n{path.parent.name}")
            process_artist(path.parent, api_key, lang, overwrite, dry_run)
        print(f"\n{path.parent.name} / {path.name}")
        process_album(path, path.parent.name, api_key, overwrite, dry_run)
        return

    children = sorted(d for d in path.iterdir() if d.is_dir())
    if any(has_audio(c) for c in children):  # an artist folder
        print(f"\n{path.name}")
        process_artist(path, api_key, lang, overwrite, dry_run)
        for album in children:
            if has_audio(album):
                print(f"\n{path.name} / {album.name}")
                process_album(album, path.name, api_key, overwrite, dry_run)
        return

    for artist in children:  # a library root
        if any(has_audio(c) for c in (d for d in artist.iterdir() if d.is_dir())):
            process_tree(artist, api_key, lang, overwrite, dry_run, include_artist)


def main() -> int:
    parser = argparse.ArgumentParser(description="Write artist.nfo/album.nfo from TheAudioDB")
    parser.add_argument(
        "path",
        nargs="?",
        help="Album, artist, or library root (default: $MUSIC_DIR/curated)",
    )
    parser.add_argument("--overwrite", action="store_true", help="Replace existing .nfo files")
    parser.add_argument("--dry-run", action="store_true", help="Print NFOs without writing")
    parser.add_argument(
        "--include-artist",
        action="store_true",
        help="When given a single album folder, also write its parent artist.nfo "
        "(sibling albums are left untouched)",
    )
    parser.add_argument("--lang", default="en", help="Biography language code (default: en)")
    parser.add_argument(
        "--audiodb-api-key",
        default=AUDIODB_API_KEY,
        help=f"TheAudioDB API key (default: {AUDIODB_API_KEY})",
    )
    args = parser.parse_args()

    musicbrainzngs.set_useragent("nfo-fetcher", "1.0", "contact@example.com")

    if args.path:
        root = Path(args.path).expanduser()
    else:
        music_dir = Path(os.environ.get("MUSIC_DIR", Path.home() / "Music")).expanduser()
        root = music_dir / "curated"

    if not root.exists():
        print(f"Error: {root} does not exist", file=sys.stderr)
        return 1

    print("=" * 60)
    print(f"Fetching NFO metadata for: {root}")
    print(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE'}")
    print("=" * 60)
    process_tree(
        root, args.audiodb_api_key, args.lang, args.overwrite, args.dry_run, args.include_artist
    )
    print("\n" + "=" * 60)
    print("Done")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
