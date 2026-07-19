#!/usr/bin/env -S uv run --script
"""Audit which of the library's CD rips have a disc ID that MusicBrainz doesn't know.

whipper stamps every rip with a MUSICBRAINZ_DISCID tag -- a hash of the disc's
TOC that identifies the exact pressing. This walks the library, and for each
album's disc ID asks MusicBrainz whether that disc ID is attached to any release
(GET /ws/2/discid/{id}). The ones it *doesn't* know are the interesting output:
they're candidates to contribute back, since attaching a disc's TOC to a MB
release is what makes the next rip of that disc auto-match.

A "missing" disc ID splits into two cases, and the report separates them:

  * release known, TOC not attached -- the FLACs already carry a
    MUSICBRAINZ_ALBUMID (we matched the release by hand), MB just has no disc ID
    on it yet. Follow the printed cdtoc/attach URL and attach it to that release.
  * no MB match at all -- no MUSICBRAINZ_ALBUMID either; the disc is unknown to
    MB. Identify/create the release first, then attach.

Scope note: only FLAC rips carry a disc ID (whipper is the only source). Albums
that are all MP3/M4A -- imports, not rips -- have no disc ID and are counted as
"no disc ID (not a FLAC rip)", not as missing. A FLAC-holding dir whose tracks
somehow lack the tag is reported the same way so it can't hide.

This is read-only: it makes GET requests to MusicBrainz and changes nothing.

Usage:
  scripts/audit_missing_discids.py                     # audit ~/Music/curated
  scripts/audit_missing_discids.py ~/Music/curated/"Various Artists"
  scripts/audit_missing_discids.py --verbose           # also list the registered ones
  scripts/audit_missing_discids.py --limit 10          # stop after 10 MB lookups (spot check)

Environment:
  MUSIC_DIR   music library root (default: ~/Music); audits $MUSIC_DIR/curated
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

MUSIC_DIR = Path(os.environ.get("MUSIC_DIR", Path.home() / "Music")).expanduser()
CURATED_DIR = MUSIC_DIR / "curated"

MB_USER_AGENT = "music-curation/1.0 (tristan@havelick.com)"
MB_RATE_LIMIT = 1.1  # seconds between calls; MusicBrainz asks for <=1 request/second

MB_LOOKUP_URL_RE = re.compile(r"MusicBrainz lookup URL:\s*(\S+)")


def find_album_dirs(paths):
    """Yield directories that directly contain FLAC files -- one per disc.

    Same rule verify_rips.py uses: a dir holding FLACs is one CD, which handles
    multi-disc sets (each "Disc N/" is its own unit, the parent holds no FLACs).
    """
    units = []
    for base in paths:
        base = Path(base)
        if not base.exists():
            print(f"!! path does not exist: {base}", file=sys.stderr)
            continue
        for dirpath, _, filenames in os.walk(base):
            if any(f.lower().endswith(".flac") for f in filenames):
                units.append(Path(dirpath))
    return sorted(units)


def first_flac(album_dir):
    flacs = sorted(f for f in album_dir.iterdir() if f.suffix.lower() == ".flac")
    return flacs[0] if flacs else None


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


def mb_lookup_url(album_dir):
    """The cdtoc/attach URL whipper logged for this disc, or None.

    whipper computes the disc's TOC and disc ID whether or not a release matched,
    and logs a URL that attaches that TOC to a release -- exactly what you follow
    to register a missing disc ID.
    """
    for log in sorted(album_dir.glob("*.log")):
        m = MB_LOOKUP_URL_RE.search(log.read_text(encoding="utf-8", errors="replace"))
        if m:
            return m.group(1)
    return None


def discid_lookup(discid):
    """Ask MB if a disc ID is attached to any release.

    Returns ("registered", [release titles]) | ("not_found", []) | ("error", msg).
    The /ws/2/discid/{id} resource 404s when the disc ID isn't attached to a
    release -- that 404 is the whole point of the audit, not a failure.
    """
    url = f"https://musicbrainz.org/ws/2/discid/{urllib.parse.quote(discid, safe='')}?fmt=json"
    req = urllib.request.Request(url, headers={"User-Agent": MB_USER_AGENT})
    time.sleep(MB_RATE_LIMIT)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return "not_found", []
        return "error", f"HTTP {e.code}"
    except Exception as e:  # network hiccup, timeout, bad JSON
        return "error", str(e)
    # A disc ID often maps to several releases (reissues/variants of one
    # pressing); dedupe identical titles so the display isn't 6x the same name.
    titles = list(dict.fromkeys(r.get("title", "?") for r in data.get("releases", [])))
    return "registered", titles


def rel(path):
    """Path relative to the library root, for compact display."""
    try:
        return str(path.relative_to(CURATED_DIR))
    except ValueError:
        return str(path)


def main():
    ap = argparse.ArgumentParser(
        description="Audit which library disc IDs MusicBrainz doesn't have attached to a release"
    )
    ap.add_argument("paths", nargs="*", help="dirs to audit (default: $MUSIC_DIR/curated)")
    ap.add_argument("--verbose", action="store_true", help="also list the registered disc IDs")
    ap.add_argument("--limit", type=int, default=None,
                    help="stop after this many MusicBrainz lookups (spot check)")
    args = ap.parse_args()

    paths = args.paths or [CURATED_DIR]
    album_dirs = find_album_dirs(paths)
    if not album_dirs:
        sys.exit("No FLAC albums found to audit.")

    # Map disc ID -> album dirs carrying it (dedupe so each disc is looked up once).
    by_discid = {}
    no_discid = []
    for d in album_dirs:
        flac = first_flac(d)
        discid = get_tag(flac, "MUSICBRAINZ_DISCID") if flac else None
        if discid:
            by_discid.setdefault(discid, []).append(d)
        else:
            no_discid.append(d)

    print(f"Auditing {len(album_dirs)} album(s): "
          f"{len(by_discid)} unique disc ID(s), {len(no_discid)} without a disc ID.\n")

    missing_known = []   # (album_dir, albumid, attach_url)
    missing_unknown = []  # (album_dir, attach_url)
    registered = []      # (album_dir, [titles])
    errors = []          # (album_dir, msg)

    for i, (discid, dirs) in enumerate(sorted(by_discid.items(), key=lambda kv: rel(kv[1][0])), 1):
        if args.limit is not None and i > args.limit:
            print(f"  (stopping after --limit {args.limit} lookups; "
                  f"{len(by_discid) - args.limit} disc ID(s) not checked)")
            break
        primary = dirs[0]
        status, info = discid_lookup(discid)
        print(f"  [{i}/{len(by_discid)}] {status:10s} {rel(primary)}")
        if status == "registered":
            registered.append((primary, info))
        elif status == "not_found":
            albumid = get_tag(first_flac(primary), "MUSICBRAINZ_ALBUMID")
            attach = mb_lookup_url(primary)
            if albumid:
                missing_known.append((primary, albumid, attach))
            else:
                missing_unknown.append((primary, attach))
        else:
            errors.append((primary, info))

    print("\n" + "=" * 60)
    print("MISSING FROM MUSICBRAINZ (disc ID not attached to a release)")
    print("=" * 60)

    if missing_known:
        print(f"\nRelease known, TOC not attached ({len(missing_known)}) -- "
              "just attach the disc ID to the release below:")
        for album_dir, albumid, attach in missing_known:
            print(f"\n  {rel(album_dir)}")
            print(f"    release: https://musicbrainz.org/release/{albumid}")
            print(f"    attach:  {attach or '(no cdtoc/attach URL in log)'}")

    if missing_unknown:
        print(f"\nNo MB match at all ({len(missing_unknown)}) -- "
              "identify/create the release, then attach:")
        for album_dir, attach in missing_unknown:
            print(f"\n  {rel(album_dir)}")
            print(f"    attach:  {attach or '(no cdtoc/attach URL in log)'}")

    if not missing_known and not missing_unknown:
        print("\n  None -- every disc ID checked is already in MusicBrainz.")

    if args.verbose and registered:
        print("\n" + "-" * 60)
        print(f"Registered ({len(registered)}):")
        for album_dir, titles in registered:
            print(f"  {rel(album_dir)}  ->  {', '.join(titles) or '(release list empty)'}")

    if no_discid:
        print("\n" + "-" * 60)
        print(f"No disc ID -- not a FLAC rip, skipped ({len(no_discid)}):")
        for d in no_discid:
            print(f"  {rel(d)}")

    if errors:
        print("\n" + "-" * 60)
        print(f"Lookup errors ({len(errors)}) -- rerun to retry:")
        for album_dir, msg in errors:
            print(f"  {rel(album_dir)}: {msg}")

    print("\n" + "=" * 60)
    print(f"SUMMARY: {len(missing_known)} to-attach, {len(missing_unknown)} unmatched, "
          f"{len(registered)} registered, {len(no_discid)} non-rip, {len(errors)} error(s)")
    print("=" * 60)

    # Exit nonzero if any disc IDs are missing, so this can gate a workflow.
    return 1 if (missing_known or missing_unknown) else 0


if __name__ == "__main__":
    sys.exit(main())
