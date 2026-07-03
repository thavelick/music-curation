#!/usr/bin/env python3
# /// script
# dependencies = [
#     "musicbrainzngs>=0.7.1",
#     "mutagen>=1.47.0",
#     "requests>=2.31.0",
# ]
# ///

"""Fetch artist images from TheAudioDB."""

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
ARTIST_IMAGE_NAMES = ["folder.jpg", "folder.png", "artist.jpg", "artist.png"]
AUDIODB_API_KEY = "123"  # Default public API key


def setup_musicbrainz():
    """Initialize MusicBrainz client with required user agent."""
    musicbrainzngs.set_useragent(
        "artist-image-fetcher",
        "1.0",
        "contact@example.com"
    )


def has_artist_image(artist_path: Path) -> bool:
    """Check if artist directory already has an artist image."""
    return any((artist_path / name).exists() for name in ARTIST_IMAGE_NAMES)


def get_artist_mbid_from_tags(artist_path: Path) -> Optional[str]:
    """
    Extract MusicBrainz artist ID from audio file tags.
    Searches through all albums for this artist.
    """
    # Look for audio files in all album subdirectories
    for pattern in ["**/*.flac", "**/*.mp3", "**/*.m4a", "**/*.ogg", "**/*.opus"]:
        for audio_file in artist_path.glob(pattern):
            try:
                audio = MutagenFile(audio_file)
                if audio is None or audio.tags is None:
                    continue

                # Try different tag formats
                mbid = None
                if hasattr(audio.tags, 'get'):
                    # Vorbis comments (FLAC, OGG, OPUS)
                    mbid = audio.tags.get("musicbrainz_artistid", [None])[0]
                elif hasattr(audio.tags, '__getitem__'):
                    # ID3 tags (MP3)
                    try:
                        mbid = str(audio.tags.get("TXXX:MusicBrainz Artist Id", None))
                    except:
                        pass

                if mbid and mbid != "None":
                    return str(mbid)

            except Exception as e:
                print(f"  Warning: Error reading tags from {audio_file.name}: {e}")
                continue

    return None


def search_musicbrainz_artist(artist_name: str) -> Optional[str]:
    """
    Search MusicBrainz for an artist and return their MBID.
    Returns the MBID if found, None otherwise.
    """
    try:
        time.sleep(RATE_LIMIT_DELAY)
        result = musicbrainzngs.search_artists(artist=artist_name, limit=1)

        if result.get('artist-list'):
            artist = result['artist-list'][0]
            score = artist.get('ext:score', '0')
            mbid = artist['id']
            print(f"  Found MBID: {mbid} (score: {score})")
            return mbid

    except Exception as e:
        print(f"  Warning: MusicBrainz search error: {e}")

    return None


def write_artist_mbid_to_tags(artist_path: Path, mbid: str, dry_run: bool = False) -> int:
    """
    Write MusicBrainz artist ID to all audio files for this artist.
    Returns the number of files tagged.
    """
    from mutagen.id3 import ID3, TXXX
    from mutagen.mp4 import MP4

    tagged_count = 0
    audio_extensions = ["**/*.flac", "**/*.mp3", "**/*.m4a", "**/*.ogg", "**/*.opus"]

    for pattern in audio_extensions:
        for audio_file in artist_path.glob(pattern):
            try:
                if dry_run:
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
                    audio.tags.add(TXXX(encoding=3, desc='MusicBrainz Artist Id', text=mbid))
                    audio.save()
                    tagged_count += 1
                elif audio_file.suffix.lower() == '.m4a':
                    # M4A uses different tag format
                    if not hasattr(audio, 'tags'):
                        continue
                    audio.tags["----:com.apple.iTunes:MusicBrainz Artist Id"] = mbid.encode('utf-8')
                    audio.save()
                    tagged_count += 1
                else:
                    # FLAC, OGG, OPUS use Vorbis comments
                    if not hasattr(audio, 'tags') or audio.tags is None:
                        continue
                    audio.tags["musicbrainz_artistid"] = mbid
                    audio.save()
                    tagged_count += 1

            except Exception as e:
                print(f"  Warning: Could not tag {audio_file.name}: {e}")
                continue

    return tagged_count


def fetch_audiodb_artist_image_url(mbid: str, api_key: str) -> Optional[str]:
    """
    Fetch artist image URL from TheAudioDB.
    Returns image URL if found, None otherwise.
    """
    try:
        url = f"https://www.theaudiodb.com/api/v1/json/{api_key}/artist-mb.php?i={mbid}"
        time.sleep(RATE_LIMIT_DELAY)

        response = requests.get(url, timeout=10)

        if response.status_code == 200:
            data = response.json()
            if data and data.get("artists") and len(data["artists"]) > 0:
                artist_thumb = data["artists"][0].get("strArtistThumb")
                if artist_thumb:
                    return artist_thumb
    except Exception as e:
        print(f"  Warning: AudioDB query error: {e}")

    return None


def download_artist_image(mbid: str, output_path: Path, dry_run: bool = False, audiodb_api_key: str = AUDIODB_API_KEY) -> bool:
    """
    Download artist image from TheAudioDB.
    Returns True if successful.
    """
    print(f"  Querying TheAudioDB for artist image...")
    image_url = fetch_audiodb_artist_image_url(mbid, audiodb_api_key)

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
        print(f"  Warning: No artist image available in AudioDB")

    return False


def process_artist(artist_path: Path, dry_run: bool = False, audiodb_api_key: str = AUDIODB_API_KEY) -> None:
    """Process a single artist directory."""
    artist_name = artist_path.name

    print(f"\n{artist_name}")

    # Check if already has artist image
    if has_artist_image(artist_path):
        print("  ✓ Already has artist image, skipping")
        return

    # Try to get artist MBID from existing tags
    mbid = get_artist_mbid_from_tags(artist_path)

    if mbid:
        print(f"  Found artist MBID in tags: {mbid}")
    else:
        # Search MusicBrainz
        print(f"  Searching MusicBrainz for artist...")
        mbid = search_musicbrainz_artist(artist_name)

        if not mbid:
            print(f"  ✗ Could not find MusicBrainz artist ID")
            return

        # Tag the files with the found MBID
        tagged_count = write_artist_mbid_to_tags(artist_path, mbid, dry_run)
        if tagged_count > 0:
            action = "Would tag" if dry_run else "Tagged"
            print(f"  {action} {tagged_count} FLAC files with artist MBID")

    # Download artist image
    output_path = artist_path / "folder.jpg"
    download_artist_image(mbid, output_path, dry_run, audiodb_api_key)


def process_artist_dir(artist_dir: Path, dry_run: bool = False, audiodb_api_key: str = AUDIODB_API_KEY) -> None:
    """Fetch the artist image for a single artist folder (writes folder.jpg into it)."""
    if not artist_dir.exists():
        print(f"Error: Directory {artist_dir} does not exist", file=sys.stderr)
        return

    print(f"{'=' * 60}")
    print(f"Processing: {artist_dir}")
    print(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"{'=' * 60}")

    process_artist(artist_dir, dry_run, audiodb_api_key)

    print(f"\n{'=' * 60}")
    print(f"Finished processing {artist_dir.name}")
    print(f"{'=' * 60}")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Fetch artist images from TheAudioDB"
    )
    parser.add_argument(
        "--artist-dir",
        required=True,
        help="Path to a single artist folder; folder.jpg is written into it"
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

    args = parser.parse_args()

    # Setup
    setup_musicbrainz()

    # Process the artist folder
    artist_dir = Path(args.artist_dir).expanduser()
    process_artist_dir(artist_dir, dry_run=args.dry_run, audiodb_api_key=args.audiodb_api_key)

    return 0


if __name__ == "__main__":
    sys.exit(main())
