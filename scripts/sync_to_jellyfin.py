#!/usr/bin/env python3
"""
Sync music library to Jellyfin server via rsync with proper permissions handling.

This script syncs the entire curated/ or classical/ directory to a remote Jellyfin
server, then ensures proper ownership and permissions for the jellyfin user.
"""

import argparse
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


def trigger_scan(host, dry_run):
    """Trigger a Jellyfin library scan via the API.

    Returns True on success (or dry run), False on failure. Missing API key is
    treated as a non-fatal skip so the sync itself still counts as successful.
    """
    api_key = os.environ.get("JELLYFIN_API_KEY")
    if not api_key:
        print("⚠ Skipping scan: JELLYFIN_API_KEY not set", file=sys.stderr)
        return True

    # API is HTTP on :8096 by default, distinct from the SSH/rsync host.
    api_url = os.environ.get("JELLYFIN_API_URL") or f"http://{host}:8096"
    refresh_url = f"{api_url.rstrip('/')}/Library/Refresh"

    print("Triggering Jellyfin library scan...")
    if dry_run:
        print(f"[DRY RUN] Would POST: {refresh_url}")
        return True

    request = urllib.request.Request(
        refresh_url,
        method="POST",
        headers={"Authorization": f"MediaBrowser Token={api_key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status in (200, 204):
                print("✓ Library scan triggered")
                return True
            print(f"✗ Error: scan returned HTTP {response.status}", file=sys.stderr)
            return False
    except urllib.error.URLError as e:
        print(f"✗ Error: could not reach Jellyfin API at {refresh_url}: {e}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Sync music to Jellyfin server via rsync"
    )
    parser.add_argument(
        "--classical",
        action="store_true",
        help="Sync classical/ directory instead of curated/"
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Enable rsync --delete flag to remove files on remote that don't exist locally"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be synced without making changes"
    )
    parser.add_argument(
        "--host",
        help="Override JELLYFIN_HOST environment variable"
    )
    parser.add_argument(
        "--path",
        help="Override JELLYFIN_FILES_PATH environment variable"
    )
    parser.add_argument(
        "--scan",
        action="store_true",
        help="Trigger a Jellyfin library scan via the API after syncing "
             "(needs JELLYFIN_API_KEY; URL defaults to http://JELLYFIN_HOST:8096, "
             "override with JELLYFIN_API_URL)"
    )

    args = parser.parse_args()

    # Get host and path from env vars or args
    host = args.host or os.environ.get("JELLYFIN_HOST")
    remote_base_path = args.path or os.environ.get("JELLYFIN_FILES_PATH")

    # Validate required configuration
    if not host:
        print("Error: JELLYFIN_HOST environment variable must be set or --host must be provided", file=sys.stderr)
        return 1
    if not remote_base_path:
        print("Error: JELLYFIN_FILES_PATH environment variable must be set or --path must be provided", file=sys.stderr)
        return 1

    # Determine source directory (MUSIC_DIR overrides the default ~/Music)
    music_dir = Path(os.environ.get("MUSIC_DIR", Path.home() / "Music")).expanduser()
    dir_name = "classical" if args.classical else "curated"
    source_dir = music_dir / dir_name

    # Validate source directory exists
    if not source_dir.exists():
        print(f"Error: Source directory {source_dir} does not exist", file=sys.stderr)
        return 1

    # Build remote destination path
    remote_dir = f"{remote_base_path}/{dir_name}"

    # Print header
    mode = "DRY RUN" if args.dry_run else "LIVE"
    print("=" * 60)
    print("Syncing Music to Jellyfin Server")
    print(f"Mode: {mode}")
    print("=" * 60)
    print()
    print(f"Source: {source_dir}/")
    print(f"Destination: {host}:{remote_dir}/")
    print(f"Delete enabled: {'Yes' if args.delete else 'No'}")
    print()

    # Build rsync command.
    #
    # --rsync-path="sudo rsync": run the *remote* rsync as root. The library tree is
    # owned by jellyfin (set by the chown step below), but we connect as a normal
    # user, who can't update jellyfin-owned files' contents/permissions/times. Without
    # this, every pre-existing file produces "Operation not permitted" warnings and a
    # nonzero rsync exit. Root on the remote can write anything. (Passwordless sudo is
    # configured on the Jellyfin host.)
    #
    # --no-o/--no-g: don't preserve source owner/group; ownership is set explicitly by
    # the chown step below, so there's nothing to gain from carrying the local uid over.
    rsync_cmd = [
        "rsync",
        "-avz",
        "--rsync-path=sudo rsync",
        "--no-o",
        "--no-g",
        "--progress",
    ]

    if args.delete:
        rsync_cmd.append("--delete")

    # Add source and destination (trailing slash on source is important)
    source = str(source_dir) + "/"
    dest = f"{host}:{remote_dir}/"
    rsync_cmd.extend([source, dest])

    # Execute rsync
    print("Executing rsync...")
    if args.dry_run:
        print(f"[DRY RUN] Would execute: {' '.join(rsync_cmd)}")
        print()
    else:
        # rsync exit codes 23/24 are "partial transfer"/"vanished source files" —
        # non-fatal warnings (e.g. unpreservable attributes). Treat those as success
        # so the permission-fix step below still runs; only bail on real failures.
        non_fatal = {0, 23, 24}
        try:
            result = subprocess.run(rsync_cmd)
            print()
            if result.returncode not in non_fatal:
                print(f"✗ Error: rsync failed with exit code {result.returncode}", file=sys.stderr)
                return 1
            if result.returncode != 0:
                print(f"⚠ rsync exited {result.returncode} (non-fatal); continuing to permission fix", file=sys.stderr)
        except FileNotFoundError:
            print("✗ Error: rsync command not found", file=sys.stderr)
            return 1

    # Fix permissions on remote
    print("Fixing permissions on remote...")
    ssh_cmd = [
        "ssh", host,
        f"sudo chown -R jellyfin:jellyfin '{remote_dir}' && "
        f"sudo chmod -R ug+rw '{remote_dir}' && "
        f"sudo find '{remote_dir}' -type d -exec chmod ug+x {{}} +"
    ]

    if args.dry_run:
        print(f"[DRY RUN] Would execute: {' '.join(ssh_cmd)}")
        print()
    else:
        try:
            result = subprocess.run(ssh_cmd, check=True)
            print("✓ Permissions updated")
            print()
        except subprocess.CalledProcessError as e:
            print(f"✗ Error: Permission fix failed with exit code {e.returncode}", file=sys.stderr)
            print("Note: Files were synced but permissions may be incorrect", file=sys.stderr)
            return 1
        except FileNotFoundError:
            print("✗ Error: ssh command not found", file=sys.stderr)
            return 1

    # Optionally trigger a library scan
    if args.scan:
        print()
        if not trigger_scan(host, args.dry_run):
            return 1

    # Print summary
    print("=" * 60)
    if args.dry_run:
        print("Dry run completed")
    else:
        print("✓ Sync completed successfully")
    print("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
