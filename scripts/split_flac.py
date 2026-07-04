#!/usr/bin/env -S uv run --script
"""Split a FLAC file based on a CUE sheet"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

def parse_cue(cue_path):
    """Parse CUE file and extract track information"""
    tracks = []
    current_track = None

    with open(cue_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()

            # Match TRACK line
            track_match = re.match(r'TRACK (\d+) AUDIO', line)
            if track_match:
                if current_track:
                    tracks.append(current_track)
                current_track = {
                    'number': int(track_match.group(1)),
                    'title': '',
                    'start': None
                }

            # Match TITLE line (for tracks only, not album)
            elif current_track and re.match(r'TITLE', line):
                title_match = re.match(r'TITLE "(.+)"', line)
                if title_match:
                    current_track['title'] = title_match.group(1)

            # Match INDEX 01 line (main start point)
            elif current_track and re.match(r'INDEX 01', line):
                index_match = re.match(r'INDEX 01 (\d+):(\d+):(\d+)', line)
                if index_match:
                    mm, ss, ff = map(int, index_match.groups())
                    # Convert to seconds (75 frames per second)
                    start_seconds = mm * 60 + ss + ff / 75.0
                    current_track['start'] = start_seconds

    # Add last track
    if current_track:
        tracks.append(current_track)

    return tracks

def get_flac_duration(flac_path):
    """Get duration of FLAC file in seconds"""
    cmd = [
        'ffprobe', '-v', 'error',
        '-show_entries', 'format=duration',
        '-of', 'default=noprint_wrappers=1:nokey=1',
        str(flac_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return float(result.stdout.strip())

def split_track(flac_path, output_path, start_time, duration=None):
    """Extract a track from the FLAC file"""
    cmd = [
        'ffmpeg', '-v', 'error',
        '-ss', str(start_time),
    ]

    if duration:
        cmd.extend(['-t', str(duration)])

    # Re-encode (not -c copy): stream-copying leaves each track's STREAMINFO
    # total_samples pointing at the whole-album length, which yields wrong
    # durations and breaks AccurateRip verification (ARCUE reads the header,
    # expects the full album, and hits EOF). Re-encoding writes correct headers.
    cmd.extend([
        '-i', str(flac_path),
        '-c:a', 'flac',
        '-compression_level', '8',
        '-y', str(output_path)
    ])

    subprocess.run(cmd, check=True)

def main():
    parser = argparse.ArgumentParser(
        description="Split a FLAC file based on a CUE sheet"
    )
    parser.add_argument(
        "directory",
        help="Directory containing the FLAC and CUE files"
    )
    parser.add_argument(
        "--flac",
        help="FLAC filename (if not specified, will look for *.flac)"
    )
    parser.add_argument(
        "--cue",
        help="CUE filename (if not specified, will look for *.cue)"
    )

    args = parser.parse_args()

    source_dir = Path(args.directory).expanduser()

    if not source_dir.exists():
        print(f"Error: Directory {source_dir} does not exist", file=sys.stderr)
        return 1

    # Find FLAC file
    if args.flac:
        flac_path = source_dir / args.flac
    else:
        flac_files = list(source_dir.glob("*.flac"))
        if not flac_files:
            print(f"Error: No FLAC file found in {source_dir}", file=sys.stderr)
            return 1
        if len(flac_files) > 1:
            print(f"Error: Multiple FLAC files found. Please specify with --flac", file=sys.stderr)
            return 1
        flac_path = flac_files[0]

    # Find CUE file
    if args.cue:
        cue_path = source_dir / args.cue
    else:
        cue_files = list(source_dir.glob("*.cue"))
        if not cue_files:
            print(f"Error: No CUE file found in {source_dir}", file=sys.stderr)
            return 1
        if len(cue_files) > 1:
            print(f"Error: Multiple CUE files found. Please specify with --cue", file=sys.stderr)
            return 1
        cue_path = cue_files[0]

    print(f"Parsing CUE file: {cue_path}")
    tracks = parse_cue(cue_path)

    print(f"Found {len(tracks)} tracks")

    total_duration = get_flac_duration(flac_path)
    print(f"Total duration: {total_duration:.2f} seconds")

    for i, track in enumerate(tracks):
        track_num = f"{track['number']:02d}"
        title = track['title']
        # Sanitize filename - replace problematic characters
        safe_title = title.replace('/', '-').replace('\\', '-')
        start = track['start']

        # Calculate duration
        if i < len(tracks) - 1:
            duration = tracks[i + 1]['start'] - start
        else:
            duration = None  # Last track goes to end

        output_file = source_dir / f"{track_num} - {safe_title}.flac"

        print(f"Extracting track {track_num}: {title} (start: {start:.2f}s)")
        split_track(flac_path, output_file, start, duration)

    print(f"\nSplit complete! Created {len(tracks)} tracks.")
    return 0

if __name__ == '__main__':
    sys.exit(main())
