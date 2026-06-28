#!/usr/bin/env python3
"""
Check which albums and artists are missing cover images.
"""

import argparse
import sys
from pathlib import Path


ALBUM_IMAGE_NAMES = [
    'cover.jpg', 'cover.png',
    'folder.jpg', 'folder.png',
    'poster.jpg', 'poster.png',
    'jacket.jpg', 'jacket.png',
    'albumart.jpg', 'albumart.png',
    'front.jpg', 'front.png',
]

ARTIST_IMAGE_NAMES = [
    'folder.jpg', 'folder.png',
    'poster.jpg', 'poster.png',
    'artist.jpg', 'artist.png',
]


def has_image(directory, image_names):
    """Check if directory contains any of the specified image files."""
    for img_name in image_names:
        if (directory / img_name).exists():
            return img_name
    return None


def check_directory(base_dir):
    """
    Check for missing album and artist images.

    Expected structure:
        base_dir/
            Artist1/
                folder.jpg (artist image)
                Album1/
                    cover.jpg (album image)
                Album2/
                    cover.jpg (album image)
    """
    base_path = Path(base_dir).expanduser()

    if not base_path.exists():
        print(f"Error: Directory {base_path} does not exist", file=sys.stderr)
        return 1

    artist_folders = sorted([d for d in base_path.iterdir() if d.is_dir()])

    missing_artist_images = []
    missing_album_images = []
    albums_with_images = []

    for artist_dir in artist_folders:
        artist_name = artist_dir.name

        # Check for artist image
        artist_image = has_image(artist_dir, ARTIST_IMAGE_NAMES)
        if not artist_image:
            missing_artist_images.append(artist_name)

        # Check albums
        album_folders = sorted([d for d in artist_dir.iterdir() if d.is_dir()])

        for album_dir in album_folders:
            album_name = album_dir.name
            album_image = has_image(album_dir, ALBUM_IMAGE_NAMES)

            if not album_image:
                missing_album_images.append((artist_name, album_name))
            else:
                albums_with_images.append((artist_name, album_name, album_image))

    # Report results
    print(f"=== {base_path.name.upper()} ===")
    print(f"Total artists: {len(artist_folders)}")
    print(f"Artists missing images: {len(missing_artist_images)}")

    total_albums = sum(len([d for d in artist_dir.iterdir() if d.is_dir()]) for artist_dir in artist_folders)
    print(f"Total albums: {total_albums}")
    print(f"Albums with images: {len(albums_with_images)}")
    print(f"Albums missing images: {len(missing_album_images)}")
    print()

    if albums_with_images:
        print("Albums WITH images:")
        for artist, album, img_file in albums_with_images:
            print(f"  ✓ {artist} / {album} ({img_file})")
        print()

    if missing_artist_images:
        print("Artists without images:")
        for artist in missing_artist_images:
            print(f"  - {artist}")
        print()

    if missing_album_images:
        print("Albums without images:")
        for artist, album in missing_album_images:
            print(f"  - {artist} / {album}")
        print()

    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Check for missing album and artist images"
    )
    parser.add_argument(
        "--directory",
        required=True,
        help="Path to the music directory to check"
    )

    args = parser.parse_args()

    return check_directory(args.directory)


if __name__ == "__main__":
    sys.exit(main())
