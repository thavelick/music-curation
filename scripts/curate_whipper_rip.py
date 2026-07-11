#!/usr/bin/env -S uv run --script
"""Curate a whipper rip end to end: pull it from the rip host, rename/tag/verify

it, and (if it's clean) sync it to Jellyfin.

whipper lands rips unnamed/undertagged in `~/whipper/out/album/<Artist> -
<Album>/` on the rip host (see README "Accurate ripping with whipper"). This
script automates the whole post-rip workflow described in the README's
"Processing Workflow" checklist:

  1. Pick a rip on the rip host (newest, or by substring match).
  2. Preflight: parse the whipper `.log` for per-track AccurateRip verdicts
     and check for a `RESCUED-TRACKS.txt` breadcrumb.
  3. Pull it down with rsync into $MUSIC_DIR/curated/<Artist>/<Album>/.
  4. Rename tracks to the library's `NN Title.flac` convention.
  5. Fetch NFO metadata (scripts/fetch_nfo.py) and read its genre.
  6. Fix tags with metaflac: DATE truncated to a year, zero-padded
     TRACKNUMBER, TRACKTOTAL, and GENRE (from the NFO).
  7. Add ReplayGain.
  8. Fetch an artist image (scripts/fetch_artist_image.py) -- best effort.
  9. Fetch lyrics (scripts/fetch_lyrics.py), retried once on error.
 10. Delete the now-stale `.m3u`/`.cue` sidecars (keep `.log`/`.toc`).
 11. Verify the rip (scripts/verify_rips.py) and run the tag/image checks
     (scripts/check_tags.py, scripts/check_missing_images.py).
 12. Auto-sync to Jellyfin (scripts/sync_to_jellyfin.py --scan) iff the
     preflight was clean, verify_rips confirms it, and a genre was set.
 13. Print a summary.

A rip that looks like one disc of a multi-disc set, or whose destination
album dir already exists, aborts immediately -- both are handled manually
(see README "Multi-Disc Albums").

Usage:
  scripts/curate_whipper_rip.py                # curate the newest rip
  scripts/curate_whipper_rip.py battle          # curate the rip matching "battle"

Environment:
  RIP_HOST    required; SSH alias of the rip host (e.g. "bazzite")
  MUSIC_DIR   music library root (default: ~/Music); albums land under
              $MUSIC_DIR/curated/
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MUSIC_DIR = Path(os.environ.get("MUSIC_DIR", Path.home() / "Music")).expanduser()
CURATED_DIR = MUSIC_DIR / "curated"
REMOTE_RIP_DIR = "whipper/out/album"  # relative to the rip host's home dir

MULTI_DISC_RE = re.compile(r"\((disc|cd)\s*\d+\)", re.IGNORECASE)
VERIFIED_STATUSES = {"OK", "OK*"}


class Abort(Exception):
    """Raised to stop the pipeline with a clear, already-printed message."""


def run(cmd, **kwargs):
    """Run a subprocess, printing the command first for visibility."""
    print(f"  $ {' '.join(str(c) for c in cmd)}")
    return subprocess.run(cmd, **kwargs)


def ssh_output(rip_host, remote_cmd):
    """Run a command on the rip host over ssh and return its stdout."""
    proc = subprocess.run(
        ["ssh", rip_host, remote_cmd],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise Abort(f"ssh command failed ({proc.returncode}): {remote_cmd}\n{proc.stderr}")
    return proc.stdout


# --- Rip selection -----------------------------------------------------


def list_rip_dirs(rip_host):
    """Return [(mtime, name), ...] of rip folders on the rip host, newest last."""
    # -printf with a tab keeps folder names (spaces, unicode) unambiguous.
    out = ssh_output(
        rip_host,
        f"find ~/{REMOTE_RIP_DIR} -mindepth 1 -maxdepth 1 -type d -printf '%T@\\t%f\\n'",
    )
    rows = []
    for line in out.splitlines():
        if not line.strip():
            continue
        mtime_str, name = line.split("\t", 1)
        rows.append((float(mtime_str), name))
    return sorted(rows)


def select_rip(rip_host, substring):
    rows = list_rip_dirs(rip_host)
    if not rows:
        raise Abort(f"No rip folders found in ~/{REMOTE_RIP_DIR} on {rip_host}")

    if substring is None:
        _, name = rows[-1]
        print(f"No argument given; selecting newest rip: {name!r}")
        return name

    matches = [name for _, name in rows if substring.lower() in name.lower()]
    if len(matches) == 1:
        print(f"Selected rip matching {substring!r}: {matches[0]!r}")
        return matches[0]
    if not matches:
        candidates = "\n".join(f"  - {name}" for _, name in rows)
        raise Abort(f"No rip folder matches {substring!r}. Candidates:\n{candidates}")
    candidates = "\n".join(f"  - {name}" for name in matches)
    raise Abort(f"Multiple rip folders match {substring!r}:\n{candidates}")


# --- Preflight: whipper log + rescue check ------------------------------


def parse_whipper_log(text):
    """Parse a whipper rip log; return [{"num", "results", "verified"}, ...].

    Each track section looks like:

        1:
            Filename: ...
            ...
            AccurateRip v1:
              Result: Track not present in AccurateRip database
            AccurateRip v2:
              Result: Found, exact match
            Status: Copy OK

    A track is "verified" if any of its AccurateRip "Result:" lines contains
    "exact match" (v1 or v2 -- whichever database has the disc).
    """
    lines = text.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == "Tracks:")
    except StopIteration:
        return []
    try:
        end = next(i for i, l in enumerate(lines) if l.strip().startswith("Conclusive status report"))
    except StopIteration:
        end = len(lines)
    section = lines[start + 1 : end]

    tracks = []
    current = None
    in_ar_block = False
    for line in section:
        m = re.match(r"^\s{2}(\d+):\s*$", line)
        if m:
            if current is not None:
                tracks.append(current)
            current = {"num": int(m.group(1)), "results": []}
            in_ar_block = False
            continue
        if current is None:
            continue
        if re.match(r"^\s*AccurateRip v[12]:\s*$", line):
            in_ar_block = True
            continue
        m = re.match(r"^\s*Result:\s*(.+)$", line)
        if m and in_ar_block:
            current["results"].append(m.group(1).strip())
            in_ar_block = False
    if current is not None:
        tracks.append(current)

    for t in tracks:
        t["verified"] = any("exact match" in r for r in t["results"])
    return tracks


def preflight(rip_host, rip_name):
    """Fetch the whipper log + rescue breadcrumb; return (clean, tracks, rescued)."""
    # ~/ must stay outside the quoting or the remote shell won't expand it
    remote_dir = f"~/{shlex.quote(f'{REMOTE_RIP_DIR}/{rip_name}')}"
    # find the actual .log filename in case it's not exactly "<rip_name>.log"
    listing = ssh_output(rip_host, f"ls -1 {remote_dir}")
    log_candidates = [l for l in listing.splitlines() if l.endswith(".log")]
    if not log_candidates:
        raise Abort(f"No .log file found in {remote_dir}")
    log_name = log_candidates[0]

    log_text = ssh_output(rip_host, f"cat {remote_dir}/{shlex.quote(log_name)}")
    tracks = parse_whipper_log(log_text)
    if not tracks:
        print("  ! Could not parse any tracks from the whipper log")

    rescued = "RESCUED-TRACKS.txt" in listing.splitlines()

    failing = [t for t in tracks if not t["verified"]]
    clean = bool(tracks) and not failing and not rescued

    if clean:
        print(f"  Preflight: clean -- {len(tracks)}/{len(tracks)} tracks AccurateRip-verified")
    else:
        print()
        print("=" * 60)
        print("!! RIP IS NOT CLEAN -- see details below !!")
        print("=" * 60)
        if failing:
            print(f"  {len(failing)} track(s) failed AccurateRip verification:")
            for t in failing:
                print(f"    - track {t['num']:02d}: {', '.join(t['results']) or 'no result found'}")
        if rescued:
            print("  RESCUED-TRACKS.txt present -- one or more tracks were rescued "
                  "with error-concealing extraction (not AccurateRip-verified)")
        print("=" * 60)
        print()

    return clean, tracks, rescued


# --- Pull ----------------------------------------------------------------


def split_artist_album(rip_name):
    if " - " not in rip_name:
        raise Abort(f"Rip folder name {rip_name!r} doesn't look like 'Artist - Album'")
    artist, album = rip_name.split(" - ", 1)
    return artist.strip(), album.strip()


def pull_rip(rip_host, rip_name, album_dir):
    album_dir.parent.mkdir(parents=True, exist_ok=True)
    album_dir.mkdir(parents=True, exist_ok=True)
    remote = f"{rip_host}:{REMOTE_RIP_DIR}/{rip_name}/"
    cmd = ["rsync", "-av", remote, f"{album_dir}/"]
    proc = run(cmd)
    if proc.returncode != 0:
        raise Abort(f"rsync failed with exit code {proc.returncode}")


# --- Rename ----------------------------------------------------------------


TRACK_PREFIX_RE = re.compile(r"^(\d+)\.\s+(.+)$")


def rename_tracks(album_dir):
    renamed = []
    for path in sorted(album_dir.glob("*.flac")):
        stem = path.stem  # "NN. Artist - Title"
        m = TRACK_PREFIX_RE.match(stem)
        if not m:
            print(f"  ! Skipping rename, unexpected filename: {path.name}")
            continue
        num, rest = m.groups()
        # "Artist - Title" -> split on the FIRST " - " (an artist name may
        # itself contain " - ", but the title is whatever's left, matching
        # the folder-name split convention).
        if " - " not in rest:
            print(f"  ! Skipping rename, no 'Artist - Title' separator: {path.name}")
            continue
        title = rest.split(" - ", 1)[1]
        new_name = f"{int(num):02d} {title}.flac"
        new_path = path.with_name(new_name)
        if new_path != path:
            path.rename(new_path)
        renamed.append(new_path)
        print(f"  {path.name} -> {new_name}")
    return sorted(renamed)


# --- NFO / genre -----------------------------------------------------------


def fetch_nfo(album_dir):
    script = REPO_ROOT / "scripts" / "fetch_nfo.py"
    proc = run([str(script), str(album_dir)])
    if proc.returncode != 0:
        print(f"  ! fetch_nfo.py exited {proc.returncode} (continuing without NFO)")


def read_nfo_genre(album_dir):
    nfo_path = album_dir / "album.nfo"
    if not nfo_path.exists():
        return None
    try:
        root = ET.parse(nfo_path).getroot()
    except ET.ParseError as e:
        print(f"  ! Failed to parse {nfo_path.name}: {e}")
        return None
    genre_el = root.find("genre")
    if genre_el is None:
        return None
    return (genre_el.text or "").strip() or None


# --- Tags --------------------------------------------------------------


def metaflac(args, flac):
    return run(["metaflac", *args, str(flac)])


def normalize_year(date_value):
    """Truncate whipper's DATE (e.g. '2007-11' or '2007-11-05') to 'YYYY'."""
    m = re.match(r"(\d{4})", date_value or "")
    return m.group(1) if m else None


def get_tag(flac, tag):
    proc = subprocess.run(
        ["metaflac", f"--show-tag={tag}", str(flac)],
        capture_output=True,
        text=True,
    )
    for line in proc.stdout.splitlines():
        if line.lower().startswith(f"{tag.lower()}="):
            return line.split("=", 1)[1]
    return None


def fetch_mb_artist_credits(release_mbid):
    """Return {tracknumber: "Artist; Artist; ..."} for multi-artist tracks.

    Whipper concatenates MusicBrainz artist credits into one ARTIST string
    using whatever join phrase the MB editor typed ("feat.", "ft.", "&", ...).
    The library convention is "; " between artists, so rebuild ARTIST from the
    structured credit instead of string-parsing the join phrase.
    """
    url = (
        f"https://musicbrainz.org/ws/2/release/{release_mbid}"
        "?inc=recordings+artist-credits&fmt=json"
    )
    req = urllib.request.Request(
        url, headers={"User-Agent": "music-curation/1.0 (tristan@havelick.com)"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    credits = {}
    for medium in data.get("media", []):
        for track in medium.get("tracks", []):
            names = [c["name"] for c in track.get("artist-credit", []) if isinstance(c, dict)]
            if len(names) > 1:
                credits[int(track["position"])] = "; ".join(names)
    return credits


def fix_tags(flacs, genre):
    track_total = len(flacs)

    multi_artist = {}
    release_mbid = get_tag(flacs[0], "MUSICBRAINZ_ALBUMID")
    if release_mbid:
        try:
            multi_artist = fetch_mb_artist_credits(release_mbid)
        except Exception as e:
            print(f"  ! MusicBrainz artist-credit lookup failed ({e}); "
                  "leaving ARTIST tags as-is (check_multi_artist.py will flag them)")

    for i, flac in enumerate(flacs, 1):
        date = get_tag(flac, "DATE")
        year = normalize_year(date) if date else None
        artist = get_tag(flac, "ARTIST")

        removes = ["--remove-tag=TRACKNUMBER", "--remove-tag=TRACKTOTAL"]
        adds = [f"--set-tag=TRACKNUMBER={i:02d}", f"--set-tag=TRACKTOTAL={track_total}"]
        if year:
            removes.append("--remove-tag=DATE")
            adds.append(f"--set-tag=DATE={year}")
        if genre:
            removes.append("--remove-tag=GENRE")
            adds.append(f"--set-tag=GENRE={genre}")
        fixed = multi_artist.get(i)
        if fixed and fixed != artist:
            removes.append("--remove-tag=ARTIST")
            adds.append(f"--set-tag=ARTIST={fixed}")
            print(f"  ARTIST: {artist!r} -> {fixed!r}")

        metaflac(removes, flac)
        metaflac(adds, flac)


def add_replaygain(flacs):
    if not flacs:
        return
    run(["metaflac", "--add-replay-gain", *[str(f) for f in flacs]])


# --- Artist image / lyrics ------------------------------------------------


def fetch_artist_image(artist_dir):
    script = REPO_ROOT / "scripts" / "fetch_artist_image.py"
    proc = run([str(script), "--artist-dir", str(artist_dir)])
    if proc.returncode != 0:
        print(f"  ! fetch_artist_image.py exited {proc.returncode} (non-fatal, continuing)")


def fetch_lyrics(album_dir):
    script = REPO_ROOT / "scripts" / "fetch_lyrics.py"
    proc = run([str(script), str(album_dir)])
    if proc.returncode != 0:
        print("  Retrying fetch_lyrics.py once after a non-zero exit...")
        proc = run([str(script), str(album_dir)])
        if proc.returncode != 0:
            print(f"  ! fetch_lyrics.py still exited {proc.returncode}; continuing (lyrics never block sync)")
    return proc


# --- Cleanup ---------------------------------------------------------------


def cleanup_sidecars(album_dir):
    removed = []
    for ext in (".m3u", ".cue"):
        for path in album_dir.glob(f"*{ext}"):
            path.unlink()
            removed.append(path.name)
    return removed


# --- Verify / checks --------------------------------------------------------


def run_verify_rips(artist_dir, album_name):
    script = REPO_ROOT / "scripts" / "verify_rips.py"
    proc = subprocess.run(
        [str(script), str(artist_dir)],
        capture_output=True,
        text=True,
    )
    print(proc.stdout)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)

    status = None
    for line in proc.stdout.splitlines():
        if album_name.lower() in line.lower():
            m = re.match(r"\s*\[(\S+)\s*\]", line)
            if m:
                status = m.group(1)
    return status


def run_checks(artist_dir, artist_name):
    check_tags = REPO_ROOT / "scripts" / "check_tags.py"
    proc = subprocess.run([str(check_tags), str(artist_dir)], capture_output=True, text=True)
    relevant = [l for l in proc.stdout.splitlines() if artist_name.lower() in l.lower()]
    print("--- check_tags.py ---")
    print("\n".join(relevant) if relevant else "  (no mismatches for this artist)")

    check_multi = REPO_ROOT / "scripts" / "check_multi_artist.py"
    proc = subprocess.run([str(check_multi), str(artist_dir)], capture_output=True, text=True)
    relevant = [l for l in proc.stdout.splitlines() if "delimiter" in l or ".flac" in l]
    print("--- check_multi_artist.py ---")
    print("\n".join(relevant) if relevant else "  (no multi-artist issues)")

    check_images = REPO_ROOT / "scripts" / "check_missing_images.py"
    proc = subprocess.run(
        [str(check_images), "--directory", str(CURATED_DIR)],
        capture_output=True,
        text=True,
    )
    relevant = [l for l in proc.stdout.splitlines() if artist_name.lower() in l.lower()]
    print("--- check_missing_images.py ---")
    print("\n".join(relevant) if relevant else "  (nothing flagged for this artist)")


# --- Sync ------------------------------------------------------------------


def sync_to_jellyfin():
    script = REPO_ROOT / "scripts" / "sync_to_jellyfin.py"
    proc = run([str(script), "--scan"])
    return proc.returncode == 0


# --- Main pipeline -----------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Curate a whipper rip: pull, rename, tag, verify, and sync it."
    )
    parser.add_argument(
        "substring",
        nargs="?",
        help="case-insensitive substring to match a rip folder name (default: newest rip)",
    )
    args = parser.parse_args()

    rip_host = os.environ.get("RIP_HOST")
    if not rip_host:
        sys.exit("Error: RIP_HOST environment variable must be set (ssh alias of the rip host)")

    try:
        rip_name = select_rip(rip_host, args.substring)

        if MULTI_DISC_RE.search(rip_name):
            raise Abort(
                f"{rip_name!r} looks like one disc of a multi-disc set (disc N)/(CD N). "
                "Multi-disc curation is manual -- see README 'Multi-Disc Albums'."
            )

        artist_name, album_name = split_artist_album(rip_name)
        artist_dir = CURATED_DIR / artist_name
        album_dir = artist_dir / album_name

        if album_dir.exists():
            raise Abort(
                f"{album_dir} already exists. Investigate and resume manually with the "
                "individual scripts (fetch_nfo.py, verify_rips.py, etc.) instead."
            )

        print(f"\n=== Curating: {artist_name} / {album_name} ===\n")

        print("--- Step 1: Preflight (whipper log) ---")
        clean, _tracks, rescued = preflight(rip_host, rip_name)

        print("\n--- Step 2: Pull rip ---")
        pull_rip(rip_host, rip_name, album_dir)

        print("\n--- Step 3: Rename tracks ---")
        flacs = rename_tracks(album_dir)
        if not flacs:
            raise Abort("No FLAC tracks found/renamed after pulling the rip.")

        print("\n--- Step 4: NFO metadata ---")
        fetch_nfo(album_dir)
        genre = read_nfo_genre(album_dir)
        missing_genre = genre is None
        if genre:
            print(f"  Genre from NFO: {genre}")
        else:
            print("  ! No genre found in album.nfo; GENRE tag will be left unset")

        print("\n--- Step 5: Tags ---")
        fix_tags(flacs, genre)

        print("\n--- Step 6: ReplayGain ---")
        add_replaygain(flacs)

        print("\n--- Step 7: Artist image ---")
        fetch_artist_image(artist_dir)

        print("\n--- Step 8: Lyrics ---")
        lyrics_proc = fetch_lyrics(album_dir)

        print("\n--- Step 9: Cleanup sidecars ---")
        removed = cleanup_sidecars(album_dir)
        print(f"  Removed: {', '.join(removed) if removed else '(none found)'}")

        print("\n--- Step 10: Verify rip (AccurateRip/CTDB) ---")
        verify_status = run_verify_rips(artist_dir, album_name)
        verified = verify_status in VERIFIED_STATUSES

        print("\n--- Step 11: Tag/image checks ---")
        run_checks(artist_dir, artist_name)

        print("\n--- Step 12: Auto-sync ---")
        gates = {
            "preflight clean": clean,
            "verify_rips verdict OK/OK*": verified,
            "genre set": not missing_genre,
        }
        failing_gates = [name for name, ok in gates.items() if not ok]
        synced = False
        sync_reason = ""
        if not failing_gates:
            synced = sync_to_jellyfin()
            sync_reason = "all gates passed" if synced else "sync_to_jellyfin.py failed"
        else:
            sync_reason = "gate(s) failed: " + ", ".join(failing_gates)
            print(f"  Skipping sync -- {sync_reason}")

        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)
        print(f"  Album:            {artist_name} / {album_name}")
        print(f"  Tracks:           {len(flacs)}")
        print(f"  AccurateRip:      preflight {'clean' if clean else 'NOT CLEAN'}; "
              f"verify_rips {verify_status or 'unknown'}")
        print(f"  Genre:            {genre or '(none -- GENRE tag unset)'}")
        if lyrics_proc is not None:
            print(f"  Lyrics:           see fetch_lyrics.py summary above (exit {lyrics_proc.returncode})")
        flags = []
        if rescued:
            flags.append("RESCUED-TRACKS.txt present")
        if missing_genre:
            flags.append("missing genre")
        if not verified:
            flags.append(f"verify_rips verdict {verify_status or 'unknown'}")
        print(f"  Flags:            {', '.join(flags) if flags else '(none)'}")
        print(f"  Synced:           {'yes' if synced else 'no'} ({sync_reason})")
        print("=" * 60)

    except Abort as e:
        print(f"\nAborting: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
