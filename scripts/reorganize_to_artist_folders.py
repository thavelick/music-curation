#!/usr/bin/env -S uv run --script
"""
Reorganize music library from flat [Artist] - [Album] structure
into Artist/Album/ hierarchy for Jellyfin compatibility.
"""

import argparse
import os
import sys
from pathlib import Path


def parse_folder_name(folder_name):
    """
    Parse artist and album from folder name.
    Expected format: "[Artist] - [Album]"

    Returns:
        tuple: (artist_name, album_name)
    """
    if " - " not in folder_name:
        # No separator found - skip this folder
        return None, None

    parts = folder_name.split(" - ", 1)
    artist = parts[0].strip()
    album = parts[1].strip()

    return artist, album


def reorganize_directory(base_dir, dry_run=True):
    """
    Reorganize a music directory into Artist/Album/ structure.

    Args:
        base_dir: Path to the music directory
        dry_run: If True, only print what would be done
    """
    base_path = Path(base_dir).expanduser()

    if not base_path.exists():
        print(f"Error: Directory {base_path} does not exist", file=sys.stderr)
        return 1

    if not base_path.is_dir():
        print(f"Error: {base_path} is not a directory", file=sys.stderr)
        return 1

    # Get all immediate subdirectories (album folders)
    album_folders = [d for d in base_path.iterdir() if d.is_dir()]

    if not album_folders:
        print(f"No folders found in {base_path}")
        return 0

    print(f"{'[DRY RUN] ' if dry_run else ''}Processing {len(album_folders)} folders in {base_path}")
    print()

    moved_count = 0
    skipped_count = 0

    for album_path in sorted(album_folders):
        folder_name = album_path.name
        artist, album = parse_folder_name(folder_name)

        if artist is None or album is None:
            print(f"SKIP: No ' - ' separator found: {folder_name}")
            skipped_count += 1
            continue

        # Create artist directory path
        artist_dir = base_path / artist
        new_album_path = artist_dir / album

        # Check if this would be a no-op (already in correct structure)
        if album_path == new_album_path:
            print(f"SKIP: Already in correct location: {folder_name}")
            skipped_count += 1
            continue

        # Check for conflicts
        if new_album_path.exists():
            print(f"WARNING: Target already exists, skipping: {new_album_path}")
            skipped_count += 1
            continue

        if dry_run:
            print(f"WOULD MOVE:")
            print(f"  From: {album_path}")
            print(f"    To: {new_album_path}")
            print()
        else:
            # Create artist directory if it doesn't exist
            artist_dir.mkdir(exist_ok=True)

            # Move the album folder
            album_path.rename(new_album_path)
            print(f"MOVED: {folder_name}")
            print(f"    -> {artist}/{album}")
            print()

        moved_count += 1

    print(f"{'[DRY RUN] ' if dry_run else ''}Summary:")
    print(f"  {'Would move' if dry_run else 'Moved'}: {moved_count} folders")
    print(f"  Skipped: {skipped_count} folders")

    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Reorganize music library into Artist/Album/ structure"
    )
    parser.add_argument(
        "--directory",
        required=True,
        help="Path to the music directory to reorganize"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes"
    )

    args = parser.parse_args()

    return reorganize_directory(args.directory, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
