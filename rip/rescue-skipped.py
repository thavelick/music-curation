#!/usr/bin/env python3
"""Rescue tracks that whipper skipped, so the album output dir is complete.

whipper rips bit-perfectly and AccurateRip-verifies every track, but when a
track can't be read cleanly (scratched/used disc) it *skips* it: no file is
written and the album in the output dir is left with a hole. For archival that's
correct, but if you just want a listenable copy of a used CD you're left
re-ripping by hand.

This runs right after whipper (see rip-cd.sh). For each track the cue references
but no file exists, it:

  1. Re-extracts that one track with `cd-paranoia` in its paranoid mode (retry +
     error-conceal), which - unlike whipper - always produces audio, even when it
     can't be verified.
  2. Encodes it to FLAC and tags it to match its sibling tracks (album-level tags
     copied from a sibling; track number and title from the cue's filename;
     per-track MusicBrainz ids looked up best-effort).
  3. Writes it into the album dir under the exact filename the cue expects.
  4. Flags it loudly as best-effort (NOT AccurateRip-verified) and reports the
     region of heaviest read-correction as a "listen here" hint - useful for
     used discs that aren't in AccurateRip/CTDB, where verify_rips.py can't help.

Runs inside the whipper container (stdlib + cd-paranoia/flac/metaflac only).
Reads RIP_OFFSET from the environment; device and output root are passed as args.

  python3 rescue-skipped.py /output --device /dev/sr0
"""

import argparse
import os
import re
import subprocess
import urllib.request
from pathlib import Path

# cd-paranoia -e progress line, e.g. "##: 3 [correction] @ 124695984".
# Positions are absolute interleaved-sample counts; type 3 is jitter correction.
PARANOIA_RE = re.compile(r"^##: (\d+) \[(\w+)\] @ (\d+)")
SAMPLES_PER_SEC = 44100 * 2  # interleaved L+R, matching cd-paranoia's position units

# Tags that are specific to one track; everything else is album-level and can be
# copied verbatim from a sibling track.
TRACK_SPECIFIC = {
    "TITLE", "TRACKNUMBER",
    "MUSICBRAINZ_TRACKID", "MUSICBRAINZ_RELEASETRACKID",
    "MUSICBRAINZ_WORKID", "ISRC",
}
MB_UA = "music-curation rescue-skipped (https://github.com/; tristan@havelick.com)"


def run(cmd, **kw):
    return subprocess.run(cmd, text=True, capture_output=True, **kw)


def newest_cue(root: Path):
    """The album whipper just wrote is the most recently modified .cue."""
    cues = sorted(root.rglob("*.cue"), key=lambda p: p.stat().st_mtime, reverse=True)
    return cues[0] if cues else None


def parse_cue(cue: Path):
    """Map track number -> referenced filename from a whipper cue.

    whipper writes one FILE per track, named "NN. Artist - Title.flac". Its
    pregap handling (the next track's INDEX 00 sits under the *previous* file's
    block, and the real FILE line carries only an INDEX 01 with no TRACK line)
    makes FILE/TRACK ordering unreliable -- so key off the leading number in the
    filename instead, which is unambiguous.
    """
    files = {}
    for raw in cue.read_text(errors="replace").splitlines():
        m = re.match(r'FILE\s+"(.+)"\s+\w+$', raw.strip())
        if m:
            name = m.group(1)
            mn = re.match(r"(\d+)\.", name)
            if mn:
                files[int(mn.group(1))] = name
    return files


def title_from_filename(name: str) -> str:
    """Best-effort title from "NN. Artist - Title.flac" (fallback if MB lookup fails)."""
    stem = re.sub(r"^\d+\.\s*", "", name).rsplit(".flac", 1)[0]
    return stem.split(" - ", 1)[1] if " - " in stem else stem


def read_tags(flac: Path):
    out = run(["metaflac", "--export-tags-to=-", str(flac)]).stdout
    tags = {}
    for line in out.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            tags[k.upper()] = v
    return tags


def mb_track_ids(album_id: str, track_no: int):
    """Best-effort MusicBrainz lookup of per-track ids for one track. {} on failure."""
    def get(url):
        req = urllib.request.Request(url + "&fmt=json", headers={"User-Agent": MB_UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            import json
            return json.load(r)

    try:
        rel = get(f"https://musicbrainz.org/ws/2/release/{album_id}?inc=recordings+isrcs")
        track = next(
            t for med in rel["media"] for t in med["tracks"] if t["position"] == track_no
        )
        rec = track["recording"]
        ids = {
            "MUSICBRAINZ_RELEASETRACKID": track["id"],
            "MUSICBRAINZ_TRACKID": rec["id"],
            "TITLE": track["title"],
        }
        if rec.get("isrcs"):
            ids["ISRC"] = rec["isrcs"][0]
        recd = get(f"https://musicbrainz.org/ws/2/recording/{rec['id']}?inc=work-rels")
        works = [r["work"]["id"] for r in recd.get("relations", []) if r.get("work")]
        if works:
            ids["MUSICBRAINZ_WORKID"] = works[0]
        return ids
    except Exception as e:  # noqa: BLE001 - lookups are a nicety, never fatal
        print(f"   (MusicBrainz lookup failed, tagging from cue only: {e})")
        return {}


def correction_hint(paranoia_log: str):
    """Map cd-paranoia's jitter-correction density to a track-relative timestamp."""
    positions, origin_pos = [], []
    for line in paranoia_log.splitlines():
        m = PARANOIA_RE.match(line)
        if not m:
            continue
        code, pos = int(m.group(1)), int(m.group(3))
        if code in (0, 1, 14):  # read/verify/wrote carry absolute positions
            origin_pos.append(pos)
        elif code == 3:  # [correction] -- jitter correction; what we map
            positions.append(pos)
    if not positions or not origin_pos:
        return "clean read, no significant correction"
    origin = min(origin_pos)  # first sample read ~= track start (overlap events excluded)
    secs = {}
    for p in positions:
        secs[(p - origin) // SAMPLES_PER_SEC] = secs.get((p - origin) // SAMPLES_PER_SEC, 0) + 1
    busy = sorted(s for s, c in secs.items() if c >= 20)  # seconds with real activity
    if not busy:
        return "light, scattered correction (likely inaudible)"
    lo, hi = min(busy), max(busy)
    peak = max(secs, key=lambda s: secs[s])
    fmt = lambda s: f"{s // 60}:{s % 60:02d}"
    span = fmt(lo) if lo == hi else f"{fmt(lo)}-{fmt(hi)}"
    return f"heaviest read-correction {span} (peak {fmt(peak)}) -- listen there"


def rescue_track(album_dir: Path, track_no: int, name: str, sibling: Path,
                 device: str, offset: int):
    dest = album_dir / name
    wav = album_dir / f".rescue-{track_no:02d}.wav"
    print(f"\n!! Track {track_no} ({name}) was skipped by whipper -- rescuing.")

    # Paranoid single-track extraction (retry + conceal); -e logs per-op status.
    cp = run(["cd-paranoia", "-v", "-e", "-O", str(offset), "-d", device,
              str(track_no), str(wav)])
    if not wav.exists() or wav.stat().st_size == 0:
        print(f"   cd-paranoia produced no audio for track {track_no}; leaving the hole.")
        print(cp.stderr[-2000:])
        return None

    run(["flac", "--silent", "--best", "-f", "-o", str(dest), str(wav)])
    if wav.exists():  # not unlink(missing_ok=...) -- container python may be < 3.8
        wav.unlink()

    # Tag: album-level tags from the sibling, per-track from the filename, then
    # better per-track ids from MusicBrainz (which also corrects the title).
    sibling_tags = read_tags(sibling)
    tags = {k: v for k, v in sibling_tags.items() if k not in TRACK_SPECIFIC}
    tags["TRACKNUMBER"] = str(track_no)
    tags["TITLE"] = title_from_filename(name)
    album_id = sibling_tags.get("MUSICBRAINZ_ALBUMID")
    if album_id:
        tags.update(mb_track_ids(album_id, track_no))

    args = ["metaflac", "--remove-all-tags"]
    for k, v in tags.items():
        args.append(f"--set-tag={k}={v}")
    args.append(str(dest))
    run(args)

    hint = correction_hint(cp.stderr)
    print(f"   wrote {dest.name}")
    print(f"   {hint}")
    return {"track": track_no, "file": name, "title": tags.get("TITLE", "?"),
            "hint": hint}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("output_root")
    ap.add_argument("--device", default="/dev/sr0")
    args = ap.parse_args()

    offset = int(os.environ.get("RIP_OFFSET", "0"))
    root = Path(args.output_root)
    cue = newest_cue(root)
    if not cue:
        print("rescue: no cue found under output; nothing to do.")
        return
    album_dir = cue.parent
    tracks = parse_cue(cue)

    missing = {n: name for n, name in tracks.items()
               if not (album_dir / name).exists()}
    present = [album_dir / name for name in tracks.values()
               if (album_dir / name).exists()]
    if not missing:
        return  # whipper got everything; stay quiet
    if not present:
        print("rescue: every track is missing -- the rip failed wholesale, not rescuing.")
        return

    sibling = present[0]
    print(f"\n=== rescue: {len(missing)} track(s) skipped by whipper in {album_dir.name} ===")
    rescued = []
    for n in sorted(missing):
        r = rescue_track(album_dir, n, missing[n], sibling, args.device, offset)
        if r:
            rescued.append(r)

    if rescued:
        # Persist a breadcrumb so the suspect tracks are remembered after the rip.
        note = album_dir / "RESCUED-TRACKS.txt"
        lines = [
            "These tracks were skipped by whipper and recovered by best-effort",
            "cd-paranoia extraction. They are NOT AccurateRip-verified -- listen",
            "before trusting them, then verify with scripts/verify_rips.py.",
            "",
        ]
        lines += [f"track {r['track']:02d}  {r['title']}\n           {r['hint']}" for r in rescued]
        note.write_text("\n".join(lines) + "\n")
        print(f"\n!! {len(rescued)} best-effort track(s) added (see {note.name}). "
              "NOT verified -- listen, then run verify_rips.py.")


if __name__ == "__main__":
    main()
