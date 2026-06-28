#!/usr/bin/env python3
# /// script
# dependencies = [
#     "mutagen>=1.47.0",
# ]
# ///

"""Find tracks with multiple artists that aren't using the '; ' delimiter.

The library convention is to join multiple artists with a semicolon
("Santana; Dave Matthews"), not "feat."/"&"/",". This walks a directory and
flags any ARTIST tag that looks like it lists several artists but doesn't use
the semicolon form, so you can fix it by hand.

Usage:
  check_multi_artist.py ~/Music/curated
  check_multi_artist.py ~/Music/curated/"The Notorious B.I.G."
"""

import argparse
import re
import sys
from pathlib import Path

from mutagen import File as MutagenFile

AUDIO_GLOBS = ["*.flac", "*.mp3", "*.m4a", "*.ogg", "*.opus"]
# Signals that an ARTIST field probably names more than one artist.
MULTI_ARTIST_RE = re.compile(r"\bfeat\.?\b|&|,|/|\bwith\b|\bvs\.?\b", re.IGNORECASE)


def get_artist(audio_file):
    audio = MutagenFile(audio_file)
    if audio is None or audio.tags is None:
        return None
    for key in ("artist", "ARTIST", "TPE1", "\xa9ART"):
        try:
            value = audio.tags.get(key)
        except (AttributeError, TypeError):
            value = None
        if value:
            return str(value[0]) if isinstance(value, list) else str(value)
    return None


def main():
    parser = argparse.ArgumentParser(
        description="Flag multi-artist tracks not using the '; ' delimiter"
    )
    parser.add_argument("directory", help="library root, artist folder, or album folder")
    args = parser.parse_args()

    root = Path(args.directory).expanduser()
    if not root.is_dir():
        print(f"Error: {root} is not a directory", file=sys.stderr)
        return 1

    flagged = 0
    for audio_file in sorted(p for g in AUDIO_GLOBS for p in root.rglob(g)):
        artist = get_artist(audio_file)
        if not artist or "; " in artist:
            continue
        if MULTI_ARTIST_RE.search(artist):
            flagged += 1
            rel = audio_file.relative_to(root)
            print(f"{rel}")
            print(f"  ARTIST: {artist!r}  (no '; ' delimiter)")

    print(f"\n{flagged} track(s) to review" if flagged
          else "\nNo multi-artist tracks missing the '; ' delimiter")
    return 0


if __name__ == "__main__":
    sys.exit(main())
