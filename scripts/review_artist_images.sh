#!/bin/bash
# Review artist images with imv

music_dir="$1"

if [ -z "$music_dir" ]; then
    echo "Usage: $0 <music_directory>"
    exit 1
fi

# Find all artist images (folder.jpg/png in artist folders, not album folders)
find "$music_dir" -mindepth 2 -maxdepth 2 -type f \( -name "folder.jpg" -o -name "folder.png" -o -name "artist.jpg" \) | sort | while read -r image_path; do
    # Get the artist directory
    artist_dir=$(dirname "$image_path")
    
    # Get artist name from the path
    artist=$(basename "$artist_dir")
    
    # Print the artist info
    echo ""
    echo "========================================"
    echo "Artist: $artist"
    echo "========================================"
    
    # Show the image with imv
    imv "$image_path"
done
