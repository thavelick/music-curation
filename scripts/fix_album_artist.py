#!/usr/bin/env python3
# /// script
# dependencies = [
#     "mutagen>=1.47.0",
# ]
# ///

"""Set the ALBUMARTIST tag on every track to match its artist folder name.

The library is laid out as <root>/<Artist>/<Album>/[Disc N/]tracks, and the rule
is that each track's ALBUMARTIST must equal the artist folder name. This walks a
directory and enforces that.

The directory you pass can be any level of the tree:
  - a library root   (e.g. ~/Music/curated)  -> every artist/album under it
  - a single artist  (e.g. .../Pink Floyd)   -> every album for that artist
  - a single album   (e.g. .../Pink Floyd/Animals)

Usage:
  fix_album_artist.py ~/Music/curated
  fix_album_artist.py ~/Music/curated/"Pink Floyd" --dry-run
"""

import argparse
import sys
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.id3 import TPE2

AUDIO_GLOBS = ["*.flac", "*.mp3", "*.m4a", "*.ogg", "*.opus"]


def album_audio_files(album_dir):
    """All audio files in an album, including tracks under Disc N/ subfolders."""
    files = []
    for pattern in AUDIO_GLOBS:
        files.extend(album_dir.glob(pattern))
        files.extend(album_dir.glob(f"Disc */{pattern}"))
    return sorted(files)


def has_audio(directory):
    return bool(album_audio_files(directory))


def iter_albums(root):
    """Yield (artist_name, album_dir) pairs for a root, artist, or album dir."""
    if has_audio(root):  # a single album folder
        yield root.parent.name, root
        return
    children = sorted(d for d in root.iterdir() if d.is_dir())
    if any(has_audio(c) for c in children):  # an artist folder
        for album in children:
            if has_audio(album):
                yield root.name, album
        return
    for artist in children:  # a library root
        for album in sorted(d for d in artist.iterdir() if d.is_dir()):
            if has_audio(album):
                yield artist.name, album


def set_album_artist(audio_file, album_artist):
    """Set ALBUMARTIST on one file, using the right tag for its format."""
    audio = MutagenFile(audio_file)
    if audio is None:
        return False

    suffix = audio_file.suffix.lower()
    if suffix == ".mp3":
        if audio.tags is None:
            audio.add_tags()
        audio.tags.setall("TPE2", [TPE2(encoding=3, text=album_artist)])
    elif suffix == ".m4a":
        audio["aART"] = [album_artist]
    else:  # FLAC, OGG, OPUS (Vorbis comments)
        if audio.tags is None:
            audio.add_tags()
        audio["albumartist"] = album_artist
    audio.save()
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Set ALBUMARTIST to match the artist folder name"
    )
    parser.add_argument("directory", help="library root, artist folder, or album folder")
    parser.add_argument("--dry-run", action="store_true", help="show changes without writing")
    args = parser.parse_args()

    root = Path(args.directory).expanduser()
    if not root.is_dir():
        print(f"Error: {root} is not a directory", file=sys.stderr)
        return 1

    total = 0
    for album_artist, album_dir in iter_albums(root):
        files = album_audio_files(album_dir)
        if not files:
            continue
        print(f"\n{album_artist} / {album_dir.name}")
        for audio_file in files:
            if args.dry_run:
                print(f"  [DRY RUN] would set ALBUMARTIST='{album_artist}' on {audio_file.name}")
                total += 1
            elif set_album_artist(audio_file, album_artist):
                total += 1
            else:
                print(f"  Warning: could not read {audio_file.name}", file=sys.stderr)

    action = "Would update" if args.dry_run else "Updated"
    print(f"\n{action} {total} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
