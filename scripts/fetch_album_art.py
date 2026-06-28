#!/usr/bin/env python3
# /// script
# dependencies = [
#     "musicbrainzngs>=0.7.1",
#     "mutagen>=1.47.0",
#     "requests>=2.31.0",
# ]
# ///

"""
Fetch missing album art from MusicBrainz Cover Art Archive.
Automatically tags FLAC files with MusicBrainz IDs when found.
"""

import argparse
import sys
import time
from pathlib import Path
from typing import Optional

import musicbrainzngs
import requests
from mutagen import File as MutagenFile


# Configuration
RATE_LIMIT_DELAY = 1.0  # Seconds between API calls
ALBUM_ART_NAMES = ["cover.jpg", "folder.jpg", "poster.jpg", "albumart.jpg"]
AUDIODB_API_KEY = "123"  # Default public API key


def setup_musicbrainz():
    """Initialize MusicBrainz client with required user agent."""
    musicbrainzngs.set_useragent(
        "music-art-fetcher",
        "1.0",
        "contact@example.com"
    )


def has_album_art(album_path: Path) -> bool:
    """Check if album directory already has cover art."""
    return any((album_path / name).exists() for name in ALBUM_ART_NAMES)


def get_mbid_from_tags(album_path: Path) -> Optional[str]:
    """
    Extract MusicBrainz release ID from audio file tags.
    Tries all audio files in the album directory.
    """
    # Look for common audio file extensions
    audio_extensions = ["*.flac", "*.mp3", "*.m4a", "*.ogg", "*.opus"]

    for pattern in audio_extensions:
        for audio_file in album_path.glob(pattern):
            try:
                audio = MutagenFile(audio_file)
                if audio is None:
                    continue

                # Try different tag names depending on format
                # FLAC/Vorbis use lowercase keys
                mbid = None
                if hasattr(audio, 'tags') and audio.tags:
                    # Try vorbis comment style (FLAC, OGG)
                    mbid = audio.tags.get("musicbrainz_albumid", [None])[0]

                    # Try as dict key for other formats
                    if mbid is None and isinstance(audio.tags, dict):
                        mbid = audio.tags.get("musicbrainz_albumid")

                if mbid:
                    return str(mbid) if not isinstance(mbid, str) else mbid

            except Exception as e:
                print(f"  Warning: Error reading tags from {audio_file.name}: {e}")
                continue

    return None


def write_mbid_to_tags(album_path: Path, mbid: str, dry_run: bool = False) -> int:
    """
    Write MusicBrainz album ID to all audio files in the album.
    Returns number of files tagged.
    """
    from mutagen.id3 import ID3, TXXX
    from mutagen.mp4 import MP4

    tagged_count = 0
    audio_extensions = ["*.flac", "*.mp3", "*.m4a", "*.ogg", "*.opus"]

    for pattern in audio_extensions:
        for audio_file in album_path.glob(pattern):
            try:
                if dry_run:
                    print(f"  [DRY RUN] Would tag: {audio_file.name}")
                    tagged_count += 1
                    continue

                audio = MutagenFile(audio_file)
                if audio is None:
                    continue

                # Handle different tag formats
                if audio_file.suffix.lower() == '.mp3':
                    # MP3 uses ID3 tags with TXXX frames for MusicBrainz IDs
                    if not hasattr(audio, 'tags') or audio.tags is None:
                        audio.add_tags()
                    audio.tags.add(TXXX(encoding=3, desc='MusicBrainz Album Id', text=mbid))
                    audio.save()
                    tagged_count += 1
                elif audio_file.suffix.lower() == '.m4a':
                    # M4A uses different tag format
                    if not hasattr(audio, 'tags'):
                        continue
                    audio.tags["----:com.apple.iTunes:MusicBrainz Album Id"] = mbid.encode('utf-8')
                    audio.save()
                    tagged_count += 1
                else:
                    # FLAC, OGG, OPUS use Vorbis comments
                    if not hasattr(audio, 'tags') or audio.tags is None:
                        continue
                    audio.tags["musicbrainz_albumid"] = mbid
                    audio.save()
                    tagged_count += 1

            except Exception as e:
                print(f"  Warning: Could not tag {audio_file.name}: {e}")

    return tagged_count


def search_musicbrainz(artist: str, album: str) -> Optional[str]:
    """
    Search MusicBrainz for a release and return its MBID.
    Respects rate limiting.
    """
    try:
        time.sleep(RATE_LIMIT_DELAY)
        result = musicbrainzngs.search_releases(
            artist=artist,
            release=album,
            limit=1
        )

        if result.get('release-list'):
            mbid = result['release-list'][0]['id']
            score = result['release-list'][0].get('ext:score', '0')
            print(f"  Found MBID: {mbid} (score: {score})")
            return mbid

    except Exception as e:
        print(f"  Warning: MusicBrainz search error: {e}")

    return None


def fetch_audiodb_image_url(mbid: str, api_key: str) -> Optional[str]:
    """
    Fetch album image URL from TheAudioDB using MusicBrainz ID.
    Returns image URL if found, None otherwise.
    """
    try:
        url = f"https://www.theaudiodb.com/api/v1/json/{api_key}/album-mb.php?i={mbid}"
        time.sleep(RATE_LIMIT_DELAY)

        response = requests.get(url, timeout=10)

        if response.status_code == 200:
            data = response.json()
            if data and data.get("album") and len(data["album"]) > 0:
                album_thumb = data["album"][0].get("strAlbumThumb")
                if album_thumb:
                    return album_thumb
    except Exception as e:
        print(f"  Warning: AudioDB MBID query error: {e}")

    return None


def search_audiodb_by_name(artist: str, album: str, api_key: str) -> Optional[str]:
    """
    Search TheAudioDB by artist and album name as a fallback.
    Returns image URL if found, None otherwise.
    """
    try:
        import urllib.parse
        artist_encoded = urllib.parse.quote(artist)
        album_encoded = urllib.parse.quote(album)
        url = f"https://www.theaudiodb.com/api/v1/json/{api_key}/searchalbum.php?s={artist_encoded}&a={album_encoded}"
        time.sleep(RATE_LIMIT_DELAY)

        response = requests.get(url, timeout=10)

        if response.status_code == 200:
            data = response.json()
            if data and data.get("album") and len(data["album"]) > 0:
                # Take the first result
                album_thumb = data["album"][0].get("strAlbumThumb")
                if album_thumb:
                    print(f"  Found via AudioDB search: {data['album'][0].get('strAlbum')} by {data['album'][0].get('strArtist')}")
                    return album_thumb
    except Exception as e:
        print(f"  Warning: AudioDB search error: {e}")

    return None


def download_cover_art(mbid: str, output_path: Path, dry_run: bool = False, audiodb_api_key: str = AUDIODB_API_KEY, artist_name: str = "", album_name: str = "", method: str = "all") -> bool:
    """
    Download cover art from Cover Art Archive or TheAudioDB.
    Method can be: 'all', 'coverartarchive', 'audiodb-mbid', 'audiodb-search'
    Returns True if successful.
    """
    # Try Cover Art Archive
    if method in ("all", "coverartarchive"):
        try:
            url = f"https://coverartarchive.org/release/{mbid}/front"
            time.sleep(RATE_LIMIT_DELAY)

            response = requests.get(url, timeout=10)

            if response.status_code == 200:
                if dry_run:
                    print(f"  [DRY RUN] Would download from Cover Art Archive to: {output_path}")
                else:
                    output_path.write_bytes(response.content)
                    print(f"  ✓ Downloaded from Cover Art Archive: {output_path.name}")
                return True
            elif response.status_code != 404:
                print(f"  Warning: Cover Art Archive returned status {response.status_code}")

        except Exception as e:
            print(f"  Warning: Cover Art Archive error: {e}")

        if method == "coverartarchive":
            print(f"  Warning: No cover art available in Cover Art Archive")
            return False

    # Try AudioDB by MBID
    image_url = None
    if method in ("all", "audiodb-mbid"):
        print(f"  Trying TheAudioDB by MBID...")
        image_url = fetch_audiodb_image_url(mbid, audiodb_api_key)

        if method == "audiodb-mbid":
            if not image_url:
                print(f"  Warning: No cover art available via AudioDB MBID lookup")
                return False

    # Try AudioDB search by name
    if method in ("all", "audiodb-search"):
        if not image_url and artist_name and album_name:
            print(f"  Trying TheAudioDB search by name...")
            image_url = search_audiodb_by_name(artist_name, album_name, audiodb_api_key)

        if method == "audiodb-search" and not image_url:
            print(f"  Warning: No cover art available via AudioDB search")
            return False

    if image_url:
        try:
            time.sleep(RATE_LIMIT_DELAY)
            response = requests.get(image_url, timeout=10)

            if response.status_code == 200:
                if dry_run:
                    print(f"  [DRY RUN] Would download from AudioDB to: {output_path}")
                else:
                    output_path.write_bytes(response.content)
                    print(f"  ✓ Downloaded from TheAudioDB: {output_path.name}")
                return True
            else:
                print(f"  Warning: AudioDB image download failed with status {response.status_code}")
        except Exception as e:
            print(f"  Warning: AudioDB download error: {e}")
    else:
        print(f"  Warning: No cover art available in any source")

    return False


def process_album(album_path: Path, artist_name: str, dry_run: bool = False, audiodb_api_key: str = AUDIODB_API_KEY, method: str = "all") -> None:
    """Process a single album directory."""
    album_name = album_path.name

    print(f"\n{artist_name} / {album_name}")

    # Check if already has cover art
    if has_album_art(album_path):
        print("  ✓ Already has cover art, skipping")
        return

    # Try to get MBID from existing tags
    mbid = get_mbid_from_tags(album_path)

    if mbid:
        print(f"  Found MBID in tags: {mbid}")
    else:
        # Search MusicBrainz
        print(f"  Searching MusicBrainz...")
        mbid = search_musicbrainz(artist_name, album_name)

        if not mbid:
            print(f"  ✗ Could not find MusicBrainz ID")
            return

        # Tag the files with the found MBID
        tagged_count = write_mbid_to_tags(album_path, mbid, dry_run)
        if tagged_count > 0:
            action = "Would tag" if dry_run else "Tagged"
            print(f"  {action} {tagged_count} FLAC files with MBID")

    # Download cover art
    output_path = album_path / "cover.jpg"
    download_cover_art(mbid, output_path, dry_run, audiodb_api_key, artist_name, album_name, method)


def is_album_folder(path: Path) -> bool:
    """
    Determine if a path is an album folder (contains audio files).
    Returns True if the path contains audio files directly.
    """
    audio_extensions = ["*.flac", "*.mp3", "*.m4a", "*.ogg", "*.opus"]
    for pattern in audio_extensions:
        if list(path.glob(pattern)):
            return True
    return False


def process_directory(base_dir: Path, dry_run: bool = False, audiodb_api_key: str = AUDIODB_API_KEY, method: str = "all") -> None:
    """Process all albums in a music directory or a single album folder."""
    if not base_dir.exists():
        print(f"Error: Directory {base_dir} does not exist", file=sys.stderr)
        return

    print(f"{'=' * 60}")
    print(f"Processing: {base_dir}")
    print(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"Method: {method}")
    print(f"{'=' * 60}")

    # Check if this is a single album folder
    if is_album_folder(base_dir):
        # Single album mode - infer artist name from parent directory
        artist_name = base_dir.parent.name
        print(f"\nProcessing single album")
        process_album(base_dir, artist_name, dry_run, audiodb_api_key, method)
    else:
        # Library mode - process all artist/album folders
        artist_folders = sorted([d for d in base_dir.iterdir() if d.is_dir()])

        for artist_path in artist_folders:
            artist_name = artist_path.name

            # Process each album within the artist folder
            album_folders = sorted([d for d in artist_path.iterdir() if d.is_dir()])

            for album_path in album_folders:
                process_album(album_path, artist_name, dry_run, audiodb_api_key, method)

    print(f"\n{'=' * 60}")
    print(f"Finished processing {base_dir.name}")
    print(f"{'=' * 60}")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Fetch album art from MusicBrainz Cover Art Archive and TheAudioDB"
    )
    parser.add_argument(
        "--directory",
        required=True,
        help="Path to the music directory or single album folder to process"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes"
    )
    parser.add_argument(
        "--audiodb-api-key",
        default=AUDIODB_API_KEY,
        help=f"TheAudioDB API key (default: {AUDIODB_API_KEY})"
    )
    parser.add_argument(
        "--method",
        choices=["all", "coverartarchive", "audiodb-mbid", "audiodb-search"],
        default="all",
        help="Which method to use for fetching cover art (default: all)"
    )

    args = parser.parse_args()

    # Setup
    setup_musicbrainz()

    # Process directory
    base_dir = Path(args.directory).expanduser()
    process_directory(base_dir, dry_run=args.dry_run, audiodb_api_key=args.audiodb_api_key, method=args.method)

    return 0


if __name__ == "__main__":
    sys.exit(main())
