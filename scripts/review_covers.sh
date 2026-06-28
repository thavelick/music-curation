#!/bin/bash
# Review album covers with imv

music_dir="$1"

if [ -z "$music_dir" ]; then
    echo "Usage: $0 <music_directory>"
    exit 1
fi

# Find all cover images (only cover.jpg/png in album folders, skip artist folders)
find "$music_dir" -mindepth 3 -type f \( -name "cover.jpg" -o -name "cover.png" \) | sort | while read -r cover_path; do
    # Get the album directory
    album_dir=$(dirname "$cover_path")

    # Get artist and album name from the path
    artist=$(basename "$(dirname "$album_dir")")
    album=$(basename "$album_dir")

    # Print the album info
    echo ""
    echo "========================================"
    echo "Artist: $artist"
    echo "Album: $album"
    echo "========================================"

    # Show the image with imv
    imv "$cover_path"
done
