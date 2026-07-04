#!/usr/bin/env python3
# /// script
# dependencies = [
#     "mutagen>=1.47.0",
# ]
# ///

"""Print an ASCII histogram of albums per release year.

Walks a library root laid out as Artist/Album (with optional Disc N/ subfolders),
reads the year from the first DATE tag found in each album, and prints one line
per year — including empty years, so the shape of the collection is visible.
Albums with no DATE tag are listed at the end.

Usage:
  album_histogram.py                          # ~/Music/curated (or $MUSIC_DIR/curated)
  album_histogram.py ~/Music/classical
"""

import argparse
import os
import re
import sys
from collections import Counter
from pathlib import Path

from mutagen import File as MutagenFile

AUDIO_EXTENSIONS = {".flac", ".mp3", ".m4a", ".ogg", ".opus"}


def album_year(album_dir):
    """Year from the first readable DATE tag in the album, or None."""
    for dirpath, dirnames, filenames in os.walk(album_dir):
        dirnames.sort()
        for name in sorted(filenames):
            if Path(name).suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            try:
                audio = MutagenFile(Path(dirpath) / name, easy=True)
            except Exception:
                continue
            if audio and audio.get("date"):
                match = re.search(r"\d{4}", str(audio["date"][0]))
                if match:
                    return int(match.group())
    return None


def main():
    music_dir = Path(os.environ.get("MUSIC_DIR", Path.home() / "Music")).expanduser()
    parser = argparse.ArgumentParser(
        description="ASCII histogram of albums per release year"
    )
    parser.add_argument(
        "root",
        nargs="?",
        type=Path,
        default=music_dir / "curated",
        help="Library root containing Artist/Album folders (default: %(default)s)",
    )
    args = parser.parse_args()

    if not args.root.is_dir():
        print(f"Error: {args.root} is not a directory", file=sys.stderr)
        return 1

    years = Counter()
    missing = []
    for artist_dir in sorted(p for p in args.root.iterdir() if p.is_dir()):
        for album_dir in sorted(p for p in artist_dir.iterdir() if p.is_dir()):
            year = album_year(album_dir)
            if year:
                years[year] += 1
            else:
                missing.append(f"{artist_dir.name}/{album_dir.name}")

    if not years:
        print("No dated albums found", file=sys.stderr)
        return 1

    for year in range(min(years), max(years) + 1):
        count = years.get(year, 0)
        bar = f"█" * count
        print(f"{year} │{bar} {count}" if count else f"{year} │")

    total = sum(years.values())
    print(f"\n{total} albums" + (f", {len(missing)} without a DATE tag:" if missing else ""))
    for album in missing:
        print(f"  {album}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
