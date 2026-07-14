#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#     "musicbrainzngs>=0.7.1",
#     "mutagen>=1.47.0",
# ]
# ///
"""Find and (optionally) write MusicBrainz IDs for albums that lack them.

CD rips get exact MusicBrainz IDs from the disc TOC; Bandcamp/download albums
don't, so this backfills them by *searching* MusicBrainz by artist + album and
verifying the hit by artist name and track count (and year, when present). A
search match is inherently fuzzier than a disc-ID lookup, so this only proposes
a match when it clears a confidence bar, and it's dry-run by default -- pass
--write to tag.

It writes the four album-level IDs fetch_nfo.py reads
(musicbrainz_albumartistid / _artistid / _releasegroupid / _albumid) in the
right per-format keys (Vorbis for FLAC/OGG/OPUS, ----:com.apple.iTunes: atoms
for M4A, TXXX frames for MP3), which is enough to unlock artist.nfo + album.nfo.

Usage:
  match_musicbrainz.py [path]            # dry run: propose matches
  match_musicbrainz.py [path] --write    # write tags for confident matches
  match_musicbrainz.py --all [path]      # also consider already-MB-tagged albums

`path` defaults to $MUSIC_DIR/curated; it may be that root, an artist folder, or
a single album folder. Multi-disc albums (Album/Disc N/) are matched as a whole.
"""
import argparse
import os
import re
import sys
import time
from pathlib import Path

import musicbrainzngs as mb
from mutagen import File as MutagenFile
from mutagen.id3 import TXXX
from mutagen.mp4 import MP4FreeForm

AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".ogg", ".opus"}
RATE_LIMIT_DELAY = 1.1  # MusicBrainz asks for <=1 request/second
DISC_RE = re.compile(r"^(disc|cd)\s*\d+", re.IGNORECASE)

# Logical tag -> per-format key for the text fields we read to build a query.
TEXT_KEYS = {
    "album": {"vorbis": "album", "mp3": "TALB", "m4a": "\xa9alb"},
    "albumartist": {"vorbis": "albumartist", "mp3": "TPE2", "m4a": "aART"},
    "artist": {"vorbis": "artist", "mp3": "TPE1", "m4a": "\xa9ART"},
    "date": {"vorbis": "date", "mp3": "TDRC", "m4a": "\xa9day"},
}

# The four MusicBrainz IDs fetch_nfo.py consumes (Vorbis/lowercase names).
MP3_DESC = {
    "musicbrainz_albumartistid": "MusicBrainz Album Artist Id",
    "musicbrainz_artistid": "MusicBrainz Artist Id",
    "musicbrainz_releasegroupid": "MusicBrainz Release Group Id",
    "musicbrainz_albumid": "MusicBrainz Album Id",
}


def subdirs(d: Path):
    return sorted(p for p in d.iterdir() if p.is_dir())


def direct_audio(d: Path):
    """Audio files directly inside d (not recursive)."""
    return sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXTS)


def disc_dirs(d: Path):
    """`Disc N` / `CD N` subfolders of d that contain audio."""
    return [s for s in subdirs(d) if DISC_RE.match(s.name) and direct_audio(s)]


def is_album(d: Path) -> bool:
    return bool(direct_audio(d)) or bool(disc_dirs(d))


def album_audio(d: Path):
    """All audio belonging to an album, spanning Disc N/ subfolders."""
    if direct_audio(d):
        return direct_audio(d)
    files = []
    for disc in disc_dirs(d):
        files += direct_audio(disc)
    return files


def iter_albums(path: Path):
    """Yield album folders under an album / artist / library-root path."""
    if is_album(path):
        yield path
        return
    for sub in subdirs(path):
        if is_album(sub):          # path is an artist folder
            yield sub
        else:                      # path is the library root; sub is an artist
            for album in subdirs(sub):
                if is_album(album):
                    yield album


def read_text(audio_file: Path, logical: str):
    """Read a logical text tag (album/artist/date) across formats; str or None."""
    try:
        audio = MutagenFile(audio_file)
    except Exception:
        return None
    if audio is None or audio.tags is None:
        return None
    suffix = audio_file.suffix.lower()
    fmt = "mp3" if suffix == ".mp3" else "m4a" if suffix == ".m4a" else "vorbis"
    key = TEXT_KEYS[logical][fmt]
    try:
        val = audio.tags.get(key)
        if not val:
            return None
        first = val[0] if isinstance(val, list) else val
        return str(first).strip() or None
    except Exception:
        return None


def read_mb_id(audio_file: Path, name: str):
    """Read a MusicBrainz ID tag across formats; str or None."""
    try:
        audio = MutagenFile(audio_file)
    except Exception:
        return None
    if audio is None or audio.tags is None:
        return None
    suffix = audio_file.suffix.lower()
    try:
        if suffix == ".mp3":
            frame = audio.tags.get(f"TXXX:{MP3_DESC[name]}")
            return str(frame.text[0]) if frame else None
        if suffix == ".m4a":
            val = audio.tags.get(f"----:com.apple.iTunes:{name}")
            if not val:
                return None
            raw = val[0]
            return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        val = audio.tags.get(name)
        return str(val[0]) if val else None
    except Exception:
        return None


def first_of(files, reader, *args):
    for f in files:
        v = reader(f, *args)
        if v:
            return v
    return None


def normalize(text: str) -> str:
    """Lowercase, drop bracketed/parenthetical suffixes and punctuation."""
    text = re.sub(r"[\[(].*?[\])]", "", text)            # [Deluxe], (Remastered)
    text = re.sub(r"[^a-z0-9]+", " ", text.lower())      # punctuation -> space
    return text.strip()


def loose_match(a: str, b: str) -> bool:
    a, b = normalize(a), normalize(b)
    return bool(a) and bool(b) and (a == b or a in b or b in a)


def year_of(text):
    m = re.search(r"\d{4}", text or "")
    return m.group(0) if m else None


def candidate_tracks(rel: dict):
    for key in ("track-count", "medium-track-count"):
        if rel.get(key) not in (None, ""):
            try:
                return int(rel[key])
            except (TypeError, ValueError):
                pass
    return None


def candidate_artist_name(rel: dict) -> str:
    ac = rel.get("artist-credit") or [{}]
    entry = ac[0] if isinstance(ac[0], dict) else {}
    return entry.get("artist", {}).get("name", "")


def evaluate(local_artist, local_title, local_tracks, local_year, rel, multi):
    """Return (confident, reason) for a candidate release."""
    score = int(rel.get("ext:score", 0))
    artist_ok = loose_match(local_artist, candidate_artist_name(rel))
    title_ok = loose_match(local_title, rel.get("title", ""))
    ct = candidate_tracks(rel)
    tracks_exact = ct is not None and ct == local_tracks
    tracks_close = ct is not None and abs(ct - local_tracks) <= 1
    cand_year = year_of(rel.get("date"))
    year_ok = local_year and cand_year and local_year == cand_year

    if artist_ok and title_ok:
        # Track counts from search stubs are unreliable for multi-disc, so lean
        # on artist+title+score there; be strict on single-disc.
        if multi and score >= 95:
            return True, f"multi-disc: artist+title match, score {score} (track count not checked)"
        if tracks_exact and score >= 85:
            return True, f"artist+title+tracks({ct}) exact, score {score}"
        if tracks_close and score >= 95:
            return True, f"artist+title match, tracks {ct}~{local_tracks}, score {score}" \
                + (" +year" if year_ok else "")
    bits = [
        "artist ok" if artist_ok else "artist mismatch",
        "title ok" if title_ok else "title mismatch",
        f"tracks {ct} vs {local_tracks}",
        f"score {score}",
    ]
    return False, ", ".join(bits)


def write_tags(files, ids: dict):
    for f in files:
        audio = MutagenFile(f)
        if audio is None:
            continue
        if audio.tags is None:
            audio.add_tags()
        suffix = f.suffix.lower()
        if suffix == ".mp3":
            for k, v in ids.items():
                audio.tags.setall(f"TXXX:{MP3_DESC[k]}",
                                  [TXXX(encoding=3, desc=MP3_DESC[k], text=[v])])
        elif suffix == ".m4a":
            for k, v in ids.items():
                audio.tags[f"----:com.apple.iTunes:{k}"] = MP4FreeForm(v.encode("utf-8"))
        else:
            for k, v in ids.items():
                audio.tags[k] = v
        audio.save()
    return len(files)


def process_album(album_dir: Path, write: bool, consider_all: bool, exclude):
    files = album_audio(album_dir)
    if not files:
        return
    multi = not direct_audio(album_dir)  # audio only under Disc N/
    key = f"{album_dir.parent.name}/{album_dir.name}"
    label = f"{album_dir.parent.name} / {album_dir.name}" + ("  [multi-disc]" if multi else "")

    if any(e.lower() in key.lower() for e in exclude):
        print(f"\n{label}\n  → excluded")
        return

    already = bool(first_of(files, read_mb_id, "musicbrainz_releasegroupid")
                   or first_of(files, read_mb_id, "musicbrainz_albumid"))
    if already and not consider_all:
        return

    album = first_of(files, read_text, "album") or album_dir.name
    artist = (first_of(files, read_text, "albumartist")
              or first_of(files, read_text, "artist") or album_dir.parent.name)
    local_year = year_of(first_of(files, read_text, "date"))

    print(f"\n{label}")
    print(f"  query: artist={artist!r} album={album!r} tracks={len(files)} year={local_year or '?'}")
    try:
        time.sleep(RATE_LIMIT_DELAY)
        res = mb.search_releases(artist=artist, release=album, limit=6)
    except Exception as e:
        print(f"  ! search error: {e}")
        return
    rels = res.get("release-list", [])
    if not rels:
        print("  → no candidates")
        return

    confident = []
    for rel in rels:
        ok, reason = evaluate(artist, album, len(files), local_year, rel, multi)
        ct = candidate_tracks(rel)
        print(f"    [{'✓' if ok else ' '}] '{rel.get('title')}' by "
              f"{candidate_artist_name(rel) or '?'} [{rel.get('date', '?')}, tracks={ct}] — {reason}")
        if ok:
            confident.append(rel)

    if not confident:
        print("  → no confident match")
        return

    # Prefer an exact track-count match, then a year match, then MB score -- the
    # first confident hit isn't always the tightest (e.g. a box set edition).
    def rank(rel):
        return (candidate_tracks(rel) == len(files),
                year_of(rel.get("date")) == local_year,
                int(rel.get("ext:score", 0)))
    chosen = max(confident, key=rank)

    rgid = (chosen.get("release-group") or {}).get("id")
    relid = chosen.get("id")
    ac = chosen.get("artist-credit") or [{}]
    aid = ac[0].get("artist", {}).get("id") if isinstance(ac[0], dict) else None
    ids = {}
    if aid:
        ids["musicbrainz_albumartistid"] = aid
        ids["musicbrainz_artistid"] = aid
    if rgid:
        ids["musicbrainz_releasegroupid"] = rgid
    if relid:
        ids["musicbrainz_albumid"] = relid
    print(f"  → MATCH: release={relid} group={rgid} artist={aid}")

    if write:
        n = write_tags(files, ids)
        print(f"  ✓ wrote {len(ids)} MB tag(s) to {n} file(s)")
    else:
        print("  (dry run; pass --write to tag)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill MusicBrainz IDs by search")
    parser.add_argument("path", nargs="?",
                        help="Album, artist, or library root (default: $MUSIC_DIR/curated)")
    parser.add_argument("--write", action="store_true", help="Write tags for confident matches")
    parser.add_argument("--all", action="store_true", dest="consider_all",
                        help="Also consider albums that already have MB tags")
    parser.add_argument("--exclude", action="append", default=[], metavar="SUBSTR",
                        help="Skip albums whose 'Artist/Album' path contains SUBSTR "
                             "(repeatable)")
    args = parser.parse_args()

    mb.set_useragent("music-curation-match", "1.0", "tristan@havelick.com")

    if args.path:
        root = Path(args.path).expanduser()
    else:
        music_dir = Path(os.environ.get("MUSIC_DIR", Path.home() / "Music")).expanduser()
        root = music_dir / "curated"
    if not root.exists():
        print(f"Error: {root} does not exist", file=sys.stderr)
        return 1

    print("=" * 60)
    print(f"MusicBrainz match: {root}")
    print(f"Mode: {'WRITE' if args.write else 'DRY RUN'}")
    print("=" * 60)
    for album in iter_albums(root):
        process_album(album, args.write, args.consider_all, args.exclude)
    print("\n" + "=" * 60)
    print("Done")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
