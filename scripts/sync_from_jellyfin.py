#!/usr/bin/env -S uv run --script
"""Fully sync the music library down from the Jellyfin server.

Companion to sync_to_jellyfin.py, in the other direction: rsyncs the entire
remote curated/ (or classical/) tree onto the local library, making local
match the server. Remote is treated as the source of truth — local files that
differ (including local tag edits not yet pushed) are overwritten. Push local
work up with sync_to_jellyfin.py BEFORE pulling if you want to keep it.

Requires JELLYFIN_HOST and JELLYFIN_FILES_PATH (or --host/--path), same as
sync_to_jellyfin.py. Remote files must be world-readable (no sudo is used).
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(
        description="Fully sync the music library down from the Jellyfin server"
    )
    parser.add_argument(
        "--classical",
        action="store_true",
        help="Sync the classical/ directory instead of curated/",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be transferred without changing anything",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Enable rsync --delete flag to remove local files that don't exist on remote",
    )
    parser.add_argument("--host", help="Override JELLYFIN_HOST environment variable")
    parser.add_argument("--path", help="Override JELLYFIN_FILES_PATH environment variable")
    args = parser.parse_args()

    host = args.host or os.environ.get("JELLYFIN_HOST")
    remote_base_path = args.path or os.environ.get("JELLYFIN_FILES_PATH")
    if not host:
        print("Error: JELLYFIN_HOST environment variable must be set or --host must be provided", file=sys.stderr)
        return 1
    if not remote_base_path:
        print("Error: JELLYFIN_FILES_PATH environment variable must be set or --path must be provided", file=sys.stderr)
        return 1

    music_dir = Path(os.environ.get("MUSIC_DIR", Path.home() / "Music")).expanduser()
    dir_name = "classical" if args.classical else "curated"
    local_dir = music_dir / dir_name
    remote_dir = f"{remote_base_path}/{dir_name}"
    local_dir.mkdir(parents=True, exist_ok=True)

    mode = "DRY RUN" if args.dry_run else "LIVE"
    print("=" * 60)
    print("Syncing Music from Jellyfin Server")
    print(f"Mode: {mode}")
    print("=" * 60)
    print()
    print(f"Source: {host}:{remote_dir}/")
    print(f"Destination: {local_dir}/")
    print(f"Delete enabled: {'Yes' if args.delete else 'No'}")
    print()

    # --no-o/--no-g: remote files are jellyfin-owned; keep local ownership as
    # the invoking user. No sudo needed: the remote tree is world-readable.
    rsync_cmd = ["rsync", "-avz", "--no-o", "--no-g", "--progress"]
    if args.dry_run:
        rsync_cmd.append("--dry-run")
    if args.delete:
        rsync_cmd.append("--delete")
    rsync_cmd.extend([f"{host}:{remote_dir}/", f"{local_dir}/"])

    print("Executing rsync...")
    result = subprocess.run(rsync_cmd)
    print()
    if result.returncode == 0:
        print("=" * 60)
        print("Dry run completed" if args.dry_run else "✓ Sync completed successfully")
        print("=" * 60)
        return 0
    print(f"✗ Error: rsync failed with exit code {result.returncode}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
