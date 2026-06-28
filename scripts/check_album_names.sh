#!/bin/bash

# Check if artist and album names in tags match folder names.
# MUSIC_DIR overrides the music library root (default: ~/Music).

MUSIC_DIR="${MUSIC_DIR:-$HOME/Music}"

check_folder() {
    local folder="$1"
    local folder_name
    folder_name=$(basename "$folder")

    # Find first FLAC file in the folder
    local first_flac
    first_flac=$(find "$folder" -maxdepth 1 -name "*.flac" -type f | head -1)

    if [[ -z "$first_flac" ]]; then
        echo "WARNING: No FLAC files found in: $folder_name"
        return
    fi

    # Get artist and album name from metadata
    local artist_tag
    local album_tag
    artist_tag=$(metaflac --show-tag=ARTIST "$first_flac" 2>/dev/null | sed 's/^[Aa][Rr][Tt][Ii][Ss][Tt]=//')
    album_tag=$(metaflac --show-tag=ALBUM "$first_flac" 2>/dev/null | sed 's/^[Aa][Ll][Bb][Uu][Mm]=//')

    # Try ALBUMARTIST if ARTIST is empty
    if [[ -z "$artist_tag" ]]; then
        artist_tag=$(metaflac --show-tag=ALBUMARTIST "$first_flac" 2>/dev/null | sed 's/^[Aa][Ll][Bb][Uu][Mm][Aa][Rr][Tt][Ii][Ss][Tt]=//')
    fi

    if [[ -z "$artist_tag" ]]; then
        echo "WARNING: No ARTIST tag found in: $folder_name"
        return
    fi

    if [[ -z "$album_tag" ]]; then
        echo "WARNING: No ALBUM tag found in: $folder_name"
        return
    fi

    # Construct expected folder name from tags
    local expected_name="${artist_tag} - ${album_tag}"

    # Compare folder name with expected name
    if [[ "$folder_name" != "$expected_name" ]]; then
        echo "MISMATCH:"
        echo "  Folder:   $folder_name"
        echo "  Expected: $expected_name"
        echo ""
    fi
}

echo "Checking Classical albums..."
echo "=============================="
echo ""

while IFS= read -r -d '' folder; do
    check_folder "$folder"
done < <(find "$MUSIC_DIR/classical" -mindepth 1 -maxdepth 1 -type d -print0 | sort -z)

echo ""
echo "Checking Curated albums..."
echo "=============================="
echo ""

while IFS= read -r -d '' folder; do
    check_folder "$folder"
done < <(find "$MUSIC_DIR/curated" -mindepth 1 -maxdepth 1 -type d -print0 | sort -z)
