#!/usr/bin/env -S uv run --script
"""Curate a whipper rip end to end: pull it from the rip host, rename/tag/verify

it, and (if it's clean) sync it to Jellyfin.

whipper lands rips unnamed/undertagged in `~/whipper/out/<release-type>/<Artist>
- <Album>/` on the rip host, where <release-type> is the release's MusicBrainz
type lowercased -- "album", "live", "ep", ... (see README "Accurate ripping with
whipper"). This script automates the whole post-rip workflow described in the
README's "Processing Workflow" checklist:

  1. Pick a rip on the rip host (newest, or by substring match), across all
     release types.
  2. Preflight: parse the whipper `.log` for per-track AccurateRip verdicts
     and check for a `RESCUED-TRACKS.txt` breadcrumb.
  3. Pull it down with rsync into $MUSIC_DIR/curated/<Artist>/<Album>/.
  4. Rename tracks to the library's `NN Title.flac` convention.
  5. Fetch artist + album NFO metadata (scripts/fetch_nfo.py), and derive the
     album's genre from MusicBrainz (release-group's top-voted genre, falling
     back to the artist's).
  6. Fix tags with metaflac: DATE truncated to a year, zero-padded
     TRACKNUMBER, TRACKTOTAL, and GENRE (from MusicBrainz).
  7. Add ReplayGain.
  8. Fetch an artist image (scripts/fetch_artist_image.py) -- best effort.
  9. Fetch lyrics (scripts/fetch_lyrics.py), retried once on error.
 10. Delete the now-stale `.m3u`/`.cue` sidecars (keep `.log`/`.toc`).
 11. Verify the rip (scripts/verify_rips.py) and run the tag/image checks
     (scripts/check_tags.py, scripts/check_missing_images.py).
 12. Auto-sync to Jellyfin (scripts/sync_to_jellyfin.py --scan) iff the
     preflight was clean, verify_rips confirms it, and a genre was set.
 13. Print a summary.

One disc of a multi-disc set is curated in place: whipper names such a rip
"<Artist> - <Album> (Disc N of M)", so the disc marker is stripped off the album
title and the tracks land in $MUSIC_DIR/curated/<Artist>/<Album>/Disc N/, with
the shared cover art moved up to the album folder and DISCNUMBER/DISCTOTAL
tagged (see README "Multi-Disc Albums"). Run it once per disc; curating disc 2
expects the album folder to already exist from disc 1. Pass --disc-total when
MusicBrainz's medium count doesn't match the discs you actually have.

A rip whose destination disc folder already exists aborts immediately, as does
one in an unrecognized release-type dir (see CURATABLE_RELEASE_TYPES).

A disc with no MusicBrainz match at all can't be named or looked up, so it's
staged in $MUSIC_DIR/incoming/<discid>/ rather than the library, gets only the
steps that need no metadata (1-3, 6-7, 10-11 above), and stops with a list of
what to do by hand. See is_placeholder_rip.

Usage:
  scripts/curate_whipper_rip.py                # curate the newest rip
  scripts/curate_whipper_rip.py battle          # curate the rip matching "battle"
  scripts/curate_whipper_rip.py --wait-for-rip  # wait for the running rip, then curate it
  scripts/curate_whipper_rip.py "disc 2"         # curate one disc of a multi-disc set
  scripts/curate_whipper_rip.py --disc-total 2   # ...tagging DISCTOTAL=2 instead of MB's count

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
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MUSIC_DIR = Path(os.environ.get("MUSIC_DIR", Path.home() / "Music")).expanduser()
CURATED_DIR = MUSIC_DIR / "curated"
# Staging area for rips we can't name yet (see is_placeholder_rip). Deliberately
# outside curated/ so untagged discs never reach the library or a Jellyfin sync.
INCOMING_DIR = MUSIC_DIR / "incoming"
# Relative to the rip host's home dir. whipper's default disc template starts
# with %r (the release type, lowercased), so rips land one level down in
# out/<type>/, e.g. out/album/, out/live/, out/unknown/.
REMOTE_RIP_DIR = "whipper/out"

# The disc marker whipper appends for one disc of a set. Covers both the bare
# "(Disc 2)"/"(CD 2)" form and the "(Disc 1 of 3)" form whipper emits when
# MusicBrainz knows the total medium count, so the " of N" group is optional.
# Groups: 2 = this disc's number, 3 = the set's disc count (None if absent).
DISC_MARKER_RE = re.compile(r"\s*\((disc|cd)\s*(\d+)(?:\s+of\s+(\d+))?\)", re.IGNORECASE)
VERIFIED_STATUSES = {"OK", "OK*"}

# rip-cd.sh touches this in a rip's output folder only after a full rip +
# rescue pass (set -e aborts it earlier on failure/interrupt). --wait-for-rip
# requires it so an interrupted rip -- whose wrapper process also just
# "disappeared" -- can't be mistaken for a finished one and half-curated.
RIP_COMPLETE_SENTINEL = ".rip-complete"

# whipper's %r is the release-group's *legacy combined* MusicBrainz type,
# lowercased (whipper/common/program.py). "Combined" means a secondary type
# overrides the primary one -- The Cure's "Show" is primary=Album,
# secondary=Live, and lands in live/, not album/. whipper also runs every
# template value through PathFilter, which rewrites "/" to "_", so
# "Mixtape/Street" arrives as "mixtape_street" and a type can never nest deeper
# than one dir.
#
# This list is the closed vocabulary of that legacy field. It gates *curation*,
# not *discovery*: an unrecognized type still shows up in the scan and aborts
# loudly (see main), so a type MusicBrainz adds later can never silently vanish
# -- it just needs adding here.
CURATABLE_RELEASE_TYPES = frozenset({
    # primary types
    "album", "single", "ep", "broadcast", "other",
    # secondary types (override the primary in the combined field)
    "compilation", "soundtrack", "spokenword", "interview", "audiobook",
    "audio drama", "live", "remix", "dj-mix", "mixtape_street", "demo",
    "field recording",
})
# whipper's fallbacks when it has no MusicBrainz release to name things after.
UNKNOWN_RELEASE_TYPE = "unknown"
PLACEHOLDER_ARTIST = "Unknown Artist"
KNOWN_RELEASE_TYPES = CURATABLE_RELEASE_TYPES | {UNKNOWN_RELEASE_TYPE}


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


def wait_for_rip(rip_host, poll_interval=20):
    """Block until the whipper rip wrapper finishes on the rip host.

    The documented rip flow runs rip/rip-cd.sh, which drives whipper and then
    a rescue pass (see README "Accurate ripping with whipper"); only when that
    wrapper exits is the output dir complete. With nothing running this returns
    immediately, so --wait-for-rip is safe to pass even after a rip is done.

    The pgrep pattern is bracketed ('[r]ip-cd.sh') so it can't match the pgrep
    process's own command line -- the classic self-match guard -- and `|| true`
    swallows pgrep's exit code 1 when there's no match (which ssh_output would
    otherwise treat as a failure).
    """
    def running_pids():
        out = ssh_output(rip_host, "pgrep -f '[r]ip-cd.sh' || true")
        return [p for p in out.split() if p.strip()]

    pids = running_pids()
    if not pids:
        print("  No rip in progress on the rip host; proceeding to curate.")
        return
    print(f"  Rip in progress (rip-cd.sh PID {', '.join(pids)}); waiting for it to finish...")
    while running_pids():
        time.sleep(poll_interval)
    print("  Rip finished; proceeding to curate.")


def assert_rip_complete(rip_host, rip_path):
    """Abort unless the rip folder has rip-cd.sh's completion sentinel.

    wait_for_rip only knows the wrapper is gone, which is equally true whether
    the rip finished or was Ctrl-C'd partway. rip-cd.sh writes RIP_COMPLETE_SENTINEL
    only on a full rip + rescue, so its absence means an interrupted (partial)
    rip -- refuse it rather than half-curate. Only enforced under --wait-for-rip;
    manually curating an older rip predating the sentinel stays fine.
    """
    remote_dir = f"~/{shlex.quote(f'{REMOTE_RIP_DIR}/{rip_path}')}"
    sentinel = shlex.quote(RIP_COMPLETE_SENTINEL)
    out = ssh_output(rip_host, f"test -e {remote_dir}/{sentinel} && echo yes || echo no")
    if out.strip() != "yes":
        raise Abort(
            f"{rip_path!r} has no {RIP_COMPLETE_SENTINEL} sentinel -- the rip looks "
            "incomplete (interrupted partway?). rip-cd.sh writes it only after a full "
            "rip + rescue. If this rip predates that change, redeploy rip/rip-cd.sh to "
            "the rip host, or curate it explicitly (by name, without --wait-for-rip)."
        )


def list_rip_dirs(rip_host):
    """Return [(mtime, rel_path), ...] of rip folders on the rip host, newest last.

    rel_path is "<release-type>/<Artist> - <Album>", relative to REMOTE_RIP_DIR.
    """
    # -printf with a tab keeps folder names (spaces, unicode) unambiguous.
    # %P prints the path relative to the starting point, keeping the type dir.
    out = ssh_output(
        rip_host,
        f"find ~/{REMOTE_RIP_DIR} -mindepth 2 -maxdepth 2 -type d -printf '%T@\\t%P\\n'",
    )
    rows = []
    for line in out.splitlines():
        if not line.strip():
            continue
        mtime_str, rel_path = line.split("\t", 1)
        rows.append((float(mtime_str), rel_path))
    return sorted(rows)


def select_rip(rip_host, substring):
    """Return the rel_path of the rip to curate (newest, or matching substring)."""
    rows = list_rip_dirs(rip_host)
    if not rows:
        raise Abort(f"No rip folders found in ~/{REMOTE_RIP_DIR}/*/ on {rip_host}")

    if substring is None:
        _, rel_path = rows[-1]
        print(f"No argument given; selecting newest rip: {rel_path!r}")
        return rel_path

    # Match on the folder name only: matching the whole rel_path would make
    # "live" (or any release type) match every rip under that type's dir.
    matches = [p for _, p in rows if substring.lower() in rip_folder_name(p).lower()]
    if len(matches) == 1:
        print(f"Selected rip matching {substring!r}: {matches[0]!r}")
        return matches[0]
    if not matches:
        candidates = "\n".join(f"  - {p}" for _, p in rows)
        raise Abort(f"No rip folder matches {substring!r}. Candidates:\n{candidates}")
    candidates = "\n".join(f"  - {p}" for p in matches)
    raise Abort(f"Multiple rip folders match {substring!r}:\n{candidates}")


def rip_folder_name(rip_path):
    """The "<Artist> - <Album>" folder name, without the release-type prefix."""
    return rip_path.split("/")[-1]


def rip_release_type(rip_path):
    """The whipper release-type dir a rip sits in ("album", "live", "unknown"...)."""
    return rip_path.split("/")[0]


def is_placeholder_rip(rip_path):
    """True if whipper had no MusicBrainz release and fell back to placeholders.

    Don't mistake the unknown/ dir for this. whipper defaults %r to "unknown"
    *before* it looks at the release, and only overwrites it if the release
    group has a type set (whipper/common/program.py):

        v['A'] = 'Unknown Artist'
        v['r'] = 'unknown'
        if metadata:
            v['A'] = metadata.artist
            if metadata.releaseType:      # <-- an untyped release group skips this
                v['r'] = metadata.releaseType.lower()

    So unknown/ means "no release *type*", which covers two different discs: one
    with no MusicBrainz match at all (placeholder artist + disc ID for a title),
    and one that matched fine but whose release group is untyped -- the latter
    arrives as "unknown/<Real Artist> - <Real Album>" with usable metadata and
    curates normally. The placeholder artist, not the dir, is the real signal.
    """
    return (
        rip_release_type(rip_path) == UNKNOWN_RELEASE_TYPE
        and rip_folder_name(rip_path).startswith(f"{PLACEHOLDER_ARTIST} - ")
    )


def rip_disc_id(rip_path):
    """The disc ID a placeholder rip is named after ("Unknown Artist - <discid>")."""
    return rip_folder_name(rip_path).split(" - ", 1)[1]


MB_LOOKUP_URL_RE = re.compile(r"MusicBrainz lookup URL:\s*(\S+)")


def mb_lookup_url(album_dir):
    """The cdtoc/attach URL whipper logs for a disc it couldn't identify.

    whipper computes the disc's TOC and MusicBrainz disc ID regardless of
    whether a release matched, and logs a URL that attaches this TOC to a
    release. Following it (and creating the release if it doesn't exist) is what
    makes the *next* rip of this disc match automatically -- so it's the most
    useful thing to hand back for a placeholder rip.
    """
    for log in sorted(album_dir.glob("*.log")):
        m = MB_LOOKUP_URL_RE.search(log.read_text(encoding="utf-8", errors="replace"))
        if m:
            return m.group(1)
    return None


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


def preflight(rip_host, rip_path):
    """Fetch the whipper log + rescue breadcrumb; return (clean, tracks, rescued)."""
    # ~/ must stay outside the quoting or the remote shell won't expand it
    remote_dir = f"~/{shlex.quote(f'{REMOTE_RIP_DIR}/{rip_path}')}"
    # find the actual .log filename in case it's not exactly "<folder name>.log"
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


def split_artist_album(rip_path):
    name = rip_folder_name(rip_path)
    if " - " not in name:
        raise Abort(f"Rip folder name {name!r} doesn't look like 'Artist - Album'")
    artist, album = name.split(" - ", 1)
    return artist.strip(), album.strip()


def split_disc_marker(album_name):
    """Split "Album (Disc 1 of 3)" into ("Album", 1, 3).

    Returns (album_title, disc_number, disc_total) with disc_number/disc_total
    None when this isn't one disc of a set (disc_total alone is None for the
    "(Disc 2)" form, where whipper had no medium count to print). The marker is
    stripped so every disc of a set files under one album folder, with the
    tracks in a "Disc N/" subfolder -- see README "Multi-Disc Albums".
    """
    m = DISC_MARKER_RE.search(album_name)
    if not m:
        return album_name, None, None
    title = (album_name[: m.start()] + album_name[m.end():]).strip()
    total = int(m.group(3)) if m.group(3) else None
    return title, int(m.group(2)), total


def pull_rip(rip_host, rip_path, album_dir):
    album_dir.mkdir(parents=True, exist_ok=True)
    remote = f"{rip_host}:{REMOTE_RIP_DIR}/{rip_path}/"
    # -s (--secluded-args) sends the path over the protocol instead of through a
    # remote shell. Modern rsync escapes spaces in remote args by default, but
    # deliberately leaves the wildcards *, ?, [ and ] unescaped so they can
    # expand -- and album titles ending in "?" are common. Without -s, a rip of
    # "Artist - Album?" alongside a sibling "Artist - AlbumX" would glob to both
    # and silently pull two albums' tracks into one folder.
    # Exclude rip-cd.sh's .rip-complete sentinel -- it's a rip-host completion
    # marker (see assert_rip_complete), not something the library should carry.
    cmd = ["rsync", "-av", "-s", "--exclude", RIP_COMPLETE_SENTINEL, remote, f"{album_dir}/"]
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
    """Write this album's album.nfo and its artist's artist.nfo via fetch_nfo.py.

    --include-artist scopes the run to this album plus its parent artist: the
    artist's other albums are never touched, so re-ripping into an existing
    artist can't rewrite (or newly create) NFOs for their older albums.
    """
    script = REPO_ROOT / "scripts" / "fetch_nfo.py"
    proc = run([str(script), "--include-artist", str(album_dir)])
    if proc.returncode != 0:
        print(f"  ! fetch_nfo.py exited {proc.returncode} (continuing without NFO)")


MB_USER_AGENT = "music-curation/1.0 (tristan@havelick.com)"
MB_RATE_LIMIT = 1.1  # seconds between calls; MusicBrainz asks for <=1 request/second


def mb_get(path, params):
    """GET a MusicBrainz ws/2 JSON resource, spaced to respect the 1 req/s limit."""
    query = urllib.parse.urlencode({**params, "fmt": "json"})
    url = f"https://musicbrainz.org/ws/2/{path}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": MB_USER_AGENT})
    time.sleep(MB_RATE_LIMIT)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def canonical_genre(name):
    """Title-case a MusicBrainz genre to the library's tag style.

    MusicBrainz genres are lowercase ("blues rock", "hip-hop"); title-casing
    yields "Blues Rock", "Hip-Hop". A few initialisms are fixed up so they don't
    come out as "Uk"/"R&b". Kept in sync with scripts/fetch_genres.py.
    """
    titled = name.title()
    fixups = {"Uk": "UK", "Us": "US", "R&B": "R&B", "Edm": "EDM", "Dj": "DJ"}
    return " ".join(fixups.get(w) or w for w in titled.split(" "))


def top_genre(genres):
    """Canonical name of the highest-voted MusicBrainz genre, or None."""
    if not genres:
        return None
    return canonical_genre(max(genres, key=lambda g: g.get("count", 0))["name"])


def fetch_mb_genre(flacs):
    """Best album genre from MusicBrainz, or None.

    Prefers the release group's top-voted genre, falling back to the artist's.
    MusicBrainz genres are a community-voted controlled vocabulary keyed off the
    MBIDs already in the tags -- more accurate and consistent than TheAudioDB's
    single hand-entered strGenre (which e.g. files The Black Keys under "Indie").
    scripts/fetch_genres.py is the standalone/backfill version of this.
    """
    rg_mbid = get_tag(flacs[0], "MUSICBRAINZ_RELEASEGROUPID")
    if not rg_mbid:
        release_mbid = get_tag(flacs[0], "MUSICBRAINZ_ALBUMID")
        if release_mbid:
            try:
                data = mb_get(f"release/{release_mbid}", {"inc": "release-groups"})
                rg_mbid = data.get("release-group", {}).get("id")
            except Exception as e:
                print(f"  ! MusicBrainz release-group lookup failed ({e})")
    if rg_mbid:
        try:
            genre = top_genre(mb_get(f"release-group/{rg_mbid}", {"inc": "genres"}).get("genres"))
            if genre:
                return genre
        except Exception as e:
            print(f"  ! MusicBrainz release-group genre lookup failed ({e})")

    artist_mbid = get_tag(flacs[0], "MUSICBRAINZ_ALBUMARTISTID")
    if artist_mbid:
        try:
            genre = top_genre(mb_get(f"artist/{artist_mbid}", {"inc": "genres"}).get("genres"))
            if genre:
                return genre
        except Exception as e:
            print(f"  ! MusicBrainz artist genre lookup failed ({e})")
    return None


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


def fix_tags(flacs, genre, disc_number=None, disc_total=None):
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
        # Rewrite the disc tags rather than trusting whipper's: --disc-total can
        # override a MusicBrainz medium count that doesn't match the physical
        # set in hand (e.g. a bonus disc that varies between copies).
        if disc_number is not None:
            removes.append("--remove-tag=DISCNUMBER")
            adds.append(f"--set-tag=DISCNUMBER={disc_number}")
        if disc_total is not None:
            removes.append("--remove-tag=DISCTOTAL")
            adds.append(f"--set-tag=DISCTOTAL={disc_total}")
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


def relocate_cover(disc_dir, album_dir):
    """Move whipper's cover art up to the album root for a multi-disc set.

    Jellyfin looks for the album cover beside the album folder, not inside each
    "Disc N/" subfolder, and every disc of a set ships the same art -- so the
    first disc's copy becomes the album cover and later discs' identical copies
    are dropped rather than left lying around in the disc folders.
    """
    moved = []
    for name in ("cover.jpg", "cover.png"):
        src = disc_dir / name
        if not src.exists():
            continue
        dst = album_dir / name
        if dst.exists():
            src.unlink()
            moved.append(f"{name} (album already has one; dropped duplicate)")
        else:
            src.rename(dst)
            moved.append(f"{name} -> {dst.parent.name}/{name}")
    return moved


def cleanup_sidecars(album_dir):
    removed = []
    for ext in (".m3u", ".cue"):
        for path in album_dir.glob(f"*{ext}"):
            path.unlink()
            removed.append(path.name)
    return removed


# --- Verify / checks --------------------------------------------------------


def run_verify_rips(album_dir):
    """Verify just this album. verify_rips treats any FLAC-holding dir as a unit,
    so pointing it at the album dir works whether it landed in curated/ or
    incoming/ -- and it doesn't re-verify the artist's other albums."""
    script = REPO_ROOT / "scripts" / "verify_rips.py"
    proc = subprocess.run(
        [str(script), str(album_dir)],
        capture_output=True,
        text=True,
    )
    print(proc.stdout)
    if proc.stderr:
        print(proc.stderr, file=sys.stderr)

    status = None
    for line in proc.stdout.splitlines():
        if album_dir.name.lower() in line.lower():
            # verify_rips pads the status to 9 chars ("[OK       ]"), and one of
            # them -- "NOT IN DB", the routine verdict for a disc the databases
            # don't have -- contains spaces, so \S+ can't match it. Take
            # everything up to the "]" and drop the padding instead.
            m = re.match(r"\s*\[([^\]]+?)\s*\]", line)
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
    parser.add_argument(
        "--wait-for-rip",
        action="store_true",
        help="block until an in-progress whipper rip (rip-cd.sh) on the rip host "
             "finishes before selecting and curating a rip",
    )
    parser.add_argument(
        "--disc-total",
        type=int,
        default=None,
        metavar="N",
        help="DISCTOTAL to tag for a multi-disc set, overriding the count in the "
             "rip folder name (use when MusicBrainz's medium count doesn't match "
             "the discs you actually have)",
    )
    args = parser.parse_args()

    rip_host = os.environ.get("RIP_HOST")
    if not rip_host:
        sys.exit("Error: RIP_HOST environment variable must be set (ssh alias of the rip host)")

    try:
        if args.wait_for_rip:
            print("--- Waiting for in-progress rip ---")
            wait_for_rip(rip_host)

        rip_path = select_rip(rip_host, args.substring)

        # Under --wait-for-rip, make sure the rip we picked actually finished --
        # a wrapper killed partway leaves a partial folder but no sentinel.
        if args.wait_for_rip:
            assert_rip_complete(rip_host, rip_path)

        release_type = rip_release_type(rip_path)
        if release_type not in KNOWN_RELEASE_TYPES:
            raise Abort(
                f"{rip_path!r} sits in an unrecognized release-type dir "
                f"{release_type + '/'!r}. whipper names that dir after the release's "
                "MusicBrainz type, so this is most likely a type that isn't in "
                "CURATABLE_RELEASE_TYPES yet -- add it there (top of this script) "
                "if it's a normal release. Recognized: "
                f"{', '.join(sorted(KNOWN_RELEASE_TYPES))}."
            )

        # A placeholder rip has no artist/album to file under, so it stages in
        # incoming/<discid>/ and skips every step that needs MusicBrainz. It also
        # never carries a disc marker (it's named after the disc ID), so
        # multi-disc handling only applies to a matched rip.
        placeholder = is_placeholder_rip(rip_path)
        disc_number = disc_total = None
        if placeholder:
            artist_name, artist_dir = None, None
            album_name = rip_disc_id(rip_path)
            album_dir = INCOMING_DIR / album_name
        else:
            artist_name, raw_album = split_artist_album(rip_path)
            album_name, disc_number, disc_total = split_disc_marker(raw_album)
            if args.disc_total is not None:
                disc_total = args.disc_total
            artist_dir = CURATED_DIR / artist_name
            album_dir = artist_dir / album_name

        # Where this disc's tracks land. For a single-disc rip that's the album
        # folder itself; for one disc of a set it's "<Album>/Disc N/", leaving the
        # album folder to hold the shared cover art (see README "Multi-Disc Albums").
        disc_dir = album_dir if disc_number is None else album_dir / f"Disc {disc_number}"

        # Guard on the *disc* folder, not the album: curating disc 2 of a set is
        # expected to find the album folder already there from disc 1.
        if disc_dir.exists():
            resume = (
                "It's already staged; finish tagging it by hand"
                if placeholder
                else "Investigate and resume manually with the individual scripts "
                     "(fetch_nfo.py, verify_rips.py, etc.)"
            )
            raise Abort(f"{disc_dir} already exists. {resume} instead.")

        if placeholder:
            print(f"\n=== Curating (no metadata): {album_name} ===\n")
            print("  This disc has no MusicBrainz match, so whipper fell back to")
            print("  placeholder tags. Running the steps that don't need metadata")
            print(f"  and staging it in {INCOMING_DIR}/ for hand-tagging.\n")
        elif disc_number is not None:
            of_total = f" of {disc_total}" if disc_total else ""
            print(f"\n=== Curating: {artist_name} / {album_name} "
                  f"[disc {disc_number}{of_total}] ===\n")
        else:
            print(f"\n=== Curating: {artist_name} / {album_name} ===\n")

        print("--- Step 1: Preflight (whipper log) ---")
        clean, _tracks, rescued = preflight(rip_host, rip_path)

        print("\n--- Step 2: Pull rip ---")
        pull_rip(rip_host, rip_path, disc_dir)
        if disc_number is not None:
            for note in relocate_cover(disc_dir, album_dir):
                print(f"  Cover art: {note}")

        print("\n--- Step 3: Rename tracks ---")
        flacs = rename_tracks(disc_dir)
        if not flacs:
            raise Abort("No FLAC tracks found/renamed after pulling the rip.")

        print("\n--- Step 4: NFO metadata + genre ---")
        genre = None
        if placeholder:
            print("  Skipped -- no MusicBrainz match, so no artist/album to look up")
        else:
            fetch_nfo(album_dir)
            genre = fetch_mb_genre(flacs)
        missing_genre = genre is None
        if genre:
            print(f"  Genre from MusicBrainz: {genre}")
        elif not placeholder:
            print("  ! No MusicBrainz genre found; GENRE tag will be left unset")

        # Track numbers come off the disc, not MusicBrainz, so TRACKNUMBER and
        # TRACKTOTAL get set either way; fix_tags skips DATE/GENRE when unset.
        print("\n--- Step 5: Tags ---")
        fix_tags(flacs, genre, disc_number, disc_total)

        print("\n--- Step 6: ReplayGain ---")
        add_replaygain(flacs)

        print("\n--- Step 7: Artist image ---")
        if placeholder:
            print("  Skipped -- no artist to look up")
        else:
            fetch_artist_image(artist_dir)

        print("\n--- Step 8: Lyrics ---")
        lyrics_proc = None
        if placeholder:
            print("  Skipped -- no artist/title to search on")
        else:
            lyrics_proc = fetch_lyrics(disc_dir)

        print("\n--- Step 9: Cleanup sidecars ---")
        removed = cleanup_sidecars(disc_dir)
        print(f"  Removed: {', '.join(removed) if removed else '(none found)'}")

        # AccurateRip/CTDB are keyed on the disc TOC, not MusicBrainz, so this is
        # worth running even with no metadata -- though an obscure disc MB has
        # never seen is unlikely to be in those databases either (NOT IN DB).
        print("\n--- Step 10: Verify rip (AccurateRip/CTDB) ---")
        verify_status = run_verify_rips(disc_dir)
        verified = verify_status in VERIFIED_STATUSES

        print("\n--- Step 11: Tag/image checks ---")
        if placeholder:
            print("  Skipped -- tags stay placeholders until you fill them in")
        else:
            run_checks(artist_dir, artist_name)

        print("\n--- Step 12: Auto-sync ---")
        synced = False
        if placeholder:
            sync_reason = "placeholder tags -- never sync an untagged disc"
            print(f"  Skipping sync -- {sync_reason}")
        else:
            gates = {
                "preflight clean": clean,
                "verify_rips verdict OK/OK*": verified,
                "genre set": not missing_genre,
            }
            failing_gates = [name for name, ok in gates.items() if not ok]
            if not failing_gates:
                synced = sync_to_jellyfin()
                sync_reason = "all gates passed" if synced else "sync_to_jellyfin.py failed"
            else:
                sync_reason = "gate(s) failed: " + ", ".join(failing_gates)
                print(f"  Skipping sync -- {sync_reason}")

        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)
        if placeholder:
            print(f"  Album:            (no metadata -- disc ID {album_name})")
            print(f"  Staged in:        {album_dir}")
        else:
            print(f"  Album:            {artist_name} / {album_name}")
        if disc_number is not None:
            of_total = f" of {disc_total}" if disc_total else ""
            print(f"  Disc:             {disc_number}{of_total}  ->  {disc_dir}")
        print(f"  Tracks:           {len(flacs)}")
        print(f"  AccurateRip:      preflight {'clean' if clean else 'NOT CLEAN'}; "
              f"verify_rips {verify_status or 'unknown'}")
        if not placeholder:
            print(f"  Genre:            {genre or '(none -- GENRE tag unset)'}")
        if lyrics_proc is not None:
            print(f"  Lyrics:           see fetch_lyrics.py summary above (exit {lyrics_proc.returncode})")
        flags = []
        if rescued:
            flags.append("RESCUED-TRACKS.txt present")
        if placeholder:
            flags.append("no MusicBrainz metadata")
        elif missing_genre:
            flags.append("missing genre")
        if not verified:
            flags.append(f"verify_rips verdict {verify_status or 'unknown'}")
        print(f"  Flags:            {', '.join(flags) if flags else '(none)'}")
        print(f"  Synced:           {'yes' if synced else 'no'} ({sync_reason})")
        print("=" * 60)

        if placeholder:
            print()
            print("STOPPING: no metadata to work with. Done automatically:")
            print("  - pulled, renamed to 'NN Title.flac' (titles are placeholders)")
            print("  - TRACKNUMBER + TRACKTOTAL tagged, ReplayGain added, rip verified")
            print("Left for you, by hand:")
            url = mb_lookup_url(album_dir)
            if url:
                print("  1. Identify the disc. whipper logged a MusicBrainz URL that")
                print("     attaches this disc's TOC to a release -- do that and the")
                print("     next rip of it matches automatically:")
                print(f"     {url}")
            else:
                print("  1. Identify the disc (no MusicBrainz lookup URL in the log).")
            print("  2. Tag ARTIST/ALBUM/TITLE/DATE/GENRE -- see README 'Tagging Files'")
            print("  3. Rename the files to their real titles")
            print("  4. Add cover.jpg (the artwork scripts need a MusicBrainz match)")
            print(f"  5. Move it into {CURATED_DIR}/<Artist>/<Album>/")
            print("  6. Sync: scripts/sync_to_jellyfin.py --scan")
            print("See README \"Discs MusicBrainz doesn't know (--unknown)\".")

    except Abort as e:
        print(f"\nAborting: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
