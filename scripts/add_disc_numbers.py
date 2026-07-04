#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#     "mutagen>=1.47.0",
# ]
# ///

"""Set the DISCNUMBER tag on multi-disc albums from their Disc N/ subfolders.

For an album laid out as:
    Album Title/
        Disc 1/  01 ....flac, 02 ....flac
        Disc 2/  01 ....flac, ...
this sets each track's DISCNUMBER to the number in its "Disc N" folder. Pass the
album folder (the one containing the Disc N/ subfolders), or a library/artist
root to process every multi-disc album found beneath it.

Usage:
  add_disc_numbers.py ~/Music/curated/"Mark Knopfler"/Privateering
  add_disc_numbers.py ~/Music/curated --dry-run
"""

import argparse
import re
import sys
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen.id3 import TPOS

AUDIO_GLOBS = ["*.flac", "*.mp3", "*.m4a", "*.ogg", "*.opus"]
DISC_RE = re.compile(r"^Disc\s+(\d+)", re.IGNORECASE)


def disc_audio_files(disc_dir):
    return sorted(p for g in AUDIO_GLOBS for p in disc_dir.glob(g))


def find_disc_dirs(root):
    """Yield (disc_number, disc_dir) for every 'Disc N' folder under root."""
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            m = DISC_RE.match(path.name)
            if m and disc_audio_files(path):
                yield int(m.group(1)), path


def set_disc_number(audio_file, disc_number):
    """Set DISCNUMBER on one file, using the right tag for its format."""
    audio = MutagenFile(audio_file)
    if audio is None:
        return False

    suffix = audio_file.suffix.lower()
    if suffix == ".mp3":
        if audio.tags is None:
            audio.add_tags()
        audio.tags.setall("TPOS", [TPOS(encoding=3, text=str(disc_number))])
    elif suffix == ".m4a":
        audio["disk"] = [(disc_number, 0)]
    else:  # FLAC, OGG, OPUS (Vorbis comments)
        if audio.tags is None:
            audio.add_tags()
        audio["discnumber"] = str(disc_number)
    audio.save()
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Set DISCNUMBER from Disc N/ subfolders"
    )
    parser.add_argument("directory", help="album folder, or a root containing multi-disc albums")
    parser.add_argument("--dry-run", action="store_true", help="show changes without writing")
    args = parser.parse_args()

    root = Path(args.directory).expanduser()
    if not root.is_dir():
        print(f"Error: {root} is not a directory", file=sys.stderr)
        return 1

    disc_dirs = list(find_disc_dirs(root))
    if not disc_dirs:
        print(f"No 'Disc N' subfolders with audio found under {root}")
        return 0

    total = 0
    for disc_number, disc_dir in disc_dirs:
        print(f"\nDisc {disc_number}: {disc_dir}")
        for audio_file in disc_audio_files(disc_dir):
            if args.dry_run:
                print(f"  [DRY RUN] would set DISCNUMBER={disc_number} on {audio_file.name}")
                total += 1
            elif set_disc_number(audio_file, disc_number):
                total += 1
            else:
                print(f"  Warning: could not read {audio_file.name}", file=sys.stderr)

    action = "Would update" if args.dry_run else "Updated"
    print(f"\n{action} {total} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
