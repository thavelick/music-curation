#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#     "mutagen>=1.47.0",
# ]
# ///

"""Report tracks whose ALBUMARTIST tag doesn't match their artist folder name.

Read-only companion to fix_album_artist.py. Walks a library root, artist folder,
or single album and prints any album where a track's ALBUMARTIST differs from the
artist folder name (the library's grouping key).

Usage:
  check_tags.py ~/Music/curated
  check_tags.py ~/Music/curated/"Pink Floyd"
"""

import argparse
import sys
from pathlib import Path

from mutagen import File as MutagenFile

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


def get_album_artist(audio_file):
    """Read ALBUMARTIST across formats; returns the string or None."""
    audio = MutagenFile(audio_file)
    if audio is None or audio.tags is None:
        return None
    for key in ("albumartist", "ALBUMARTIST", "TPE2", "aART"):
        try:
            value = audio.tags.get(key)
        except (AttributeError, TypeError):
            value = None
        if value:
            return str(value[0]) if isinstance(value, list) else str(value)
    return None


def main():
    parser = argparse.ArgumentParser(
        description="Report ALBUMARTIST tags that don't match the artist folder name"
    )
    parser.add_argument("directory", help="library root, artist folder, or album folder")
    args = parser.parse_args()

    root = Path(args.directory).expanduser()
    if not root.is_dir():
        print(f"Error: {root} is not a directory", file=sys.stderr)
        return 1

    mismatches = 0
    for expected, album_dir in iter_albums(root):
        for audio_file in album_audio_files(album_dir):
            actual = get_album_artist(audio_file)
            if actual != expected:
                mismatches += 1
                rel = audio_file.relative_to(root) if root in audio_file.parents else audio_file.name
                print(f"MISMATCH: {rel}")
                print(f"  ALBUMARTIST: {actual!r}")
                print(f"  Expected:    {expected!r}")

    if mismatches:
        print(f"\n{mismatches} mismatch(es) found")
    else:
        print("All ALBUMARTIST tags match their artist folders")
    return 0


if __name__ == "__main__":
    sys.exit(main())
