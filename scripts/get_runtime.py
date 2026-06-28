#!/usr/bin/env python3
# /// script
# dependencies = [
#     "mutagen>=1.47.0",
# ]
# ///
"""
Calculate total runtime of all audio files in a directory.
"""

import argparse
from mutagen import File
from pathlib import Path


def get_duration(file_path):
    """Get duration of an audio file in seconds."""
    try:
        audio = File(file_path)
        if audio and audio.info:
            return audio.info.length
    except Exception as e:
        print(f"Error reading {file_path}: {e}")
    return 0


def format_duration(seconds):
    """Format duration as HH:MM:SS."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)

    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    else:
        return f"{minutes}:{secs:02d}"


def main():
    parser = argparse.ArgumentParser(
        description="Calculate total runtime of audio files in a directory"
    )
    parser.add_argument(
        "directory",
        help="Directory containing audio files"
    )
    parser.add_argument(
        "--recursive",
        "-r",
        action="store_true",
        help="Search recursively in subdirectories"
    )

    args = parser.parse_args()

    directory = Path(args.directory).expanduser()

    if not directory.exists():
        print(f"Error: Directory '{directory}' does not exist")
        return 1

    # Find all audio files
    if args.recursive:
        audio_files = []
        for ext in ['*.mp3', '*.flac', '*.m4a', '*.ogg']:
            audio_files.extend(directory.rglob(ext))
    else:
        audio_files = []
        for ext in ['*.mp3', '*.flac', '*.m4a', '*.ogg']:
            audio_files.extend(directory.glob(ext))

    audio_files = sorted(audio_files)

    if not audio_files:
        print(f"No audio files found in {directory}")
        return 1

    print(f"Found {len(audio_files)} audio files in {directory}")
    print()

    total_seconds = 0

    for file_path in audio_files:
        duration = get_duration(file_path)
        total_seconds += duration
        print(f"{file_path.name:60s} {format_duration(duration)}")

    print()
    print(f"Total runtime: {format_duration(total_seconds)} ({total_seconds:.1f} seconds)")
    print(f"Total runtime in minutes: {total_seconds / 60:.1f} minutes")


if __name__ == "__main__":
    main()
