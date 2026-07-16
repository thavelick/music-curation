#!/usr/bin/env -S uv run --script
"""Verify CD rips against the AccurateRip and CUETools (CTDB) databases.

For each album of FLAC tracks, this builds a temporary per-track cue sheet and
runs CUETools' headless verifier (CUETools.ARCUE.exe, via mono). ARCUE
reconstructs the disc TOC from the FLAC track lengths, looks the disc up in the
AccurateRip and CTDB databases, and reports whether your rip matches other
people's rips of the same pressing.

Why this works without the original disc: AccurateRip identifies a disc by its
track layout, which is recoverable from the FLAC sample counts. No cue/log from
the original rip is needed.

Read offset: a rip made without drive read-offset correction is still perfectly
good, but matches AccurateRip only at a small nonzero offset (e.g. +6 samples,
the drive's own offset). That still confirms the audio is correct; the offset is
reported so you can see it. whipper corrects the offset, so its rips match at 0.

Prerequisites:
  - mono           (Arch: sudo pacman -S mono)
  - CUETools       extracted to ~/Music/tools/CUETools_2.2.6/ (see README)
  - network access (queries the online AccurateRip + CTDB databases)

Usage:
  scripts/verify_rips.py                      # verify everything under ~/Music/curated
  scripts/verify_rips.py ~/Music/curated/Pink\\ Floyd   # one artist (or album)
  scripts/verify_rips.py --root ~/Music/classical
  scripts/verify_rips.py --verbose            # show ARCUE output for each album

Environment overrides:
  MUSIC_DIR   music library root                (default: ~/Music)
  MONO_BIN    path to the mono binary           (default: mono)
  ARCUE_EXE   path to CUETools.ARCUE.exe        (default: $MUSIC_DIR/tools/CUETools_2.2.6/CUETools.ARCUE.exe)
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

MUSIC_DIR = Path(os.environ.get("MUSIC_DIR", Path.home() / "Music")).expanduser()
DEFAULT_ROOT = MUSIC_DIR / "curated"
DEFAULT_ARCUE = MUSIC_DIR / "tools" / "CUETools_2.2.6" / "CUETools.ARCUE.exe"
CUE_NAME = ".verify_rips.cue"  # temp cue written into each album dir, then removed
MIN_AR_SUBMISSIONS = 2  # fewer AccurateRip submissions than this is not a reliable reference
# CTDB confidence below which "some people match us" is noise rather than evidence
# of a real pressing. A track where only 1-2 rips agree with ours while hundreds
# disagree is a damaged rip; one where 181 agree is a second pressing.
MIN_CTDB_VARIANT = 10

# A track line in an AccurateRip offset block, e.g.
#  " 01     [ba7ff2f8] (130/440) Accurately ripped"
#  " 01     [1c33611e|a4714e85] (000+000/440) No match"
AR_TRACK_RE = re.compile(
    r"^\s*(\d+)\s+\[[0-9a-f|]+\]\s+\((\d+)(?:\+\d+)?/(\d+)\)\s+(Accurately ripped|No match)"
)
AR_OFFSET_RE = re.compile(r"^Offsetted by (-?\d+):")
# A per-track CTDB result line (non-verbose ARCUE), e.g.
#  "  5   | (1157/1180) Accurately ripped"
#  "  1   | (903/1180) Accurately ripped, or (230/1180) differs in 261 samples @..."
#  " 29   | (  1/389) Accurately ripped, or (339/389) differs in 43 samples @..."
#  "  5   | (250/261) Differs in 31 samples @01:38:46"
# ARCUE right-aligns the counts, so they can carry leading spaces ("(  1/389)").
CTDB_TRACK_RE = re.compile(r"^\s*(\d+)\s*\|\s*(.+)$")
# Confidence of the entry that matches *us*, and of any entries that don't.
CTDB_ACC_RE = re.compile(r"\(\s*(\d+)/\s*(\d+)\)\s+Accurately ripped")
CTDB_DIFF_RE = re.compile(r"\(\s*(\d+)/\s*(\d+)\)\s+[Dd]iffers in \d+ samples?")


def find_units(paths):
    """Yield directories that directly contain FLAC files.

    Each such directory is one "CD" to verify. This naturally treats multi-disc
    albums correctly: a "Disc 1/" subfolder holds the FLACs and is verified on
    its own, while the parent album folder holds no FLACs and is skipped.
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


def natural_flacs(unit):
    return sorted(
        (f for f in unit.iterdir() if f.suffix.lower() == ".flac"),
        key=lambda p: p.name,
    )


def write_cue(unit, flacs):
    cue = unit / CUE_NAME
    lines = ['PERFORMER ""', 'TITLE ""']
    for i, f in enumerate(flacs, 1):
        lines.append(f'FILE "{f.name}" WAVE')
        lines.append(f"  TRACK {i:02d} AUDIO")
        lines.append("    INDEX 01 00:00:00")
    cue.write_text("\n".join(lines) + "\n")
    return cue


def run_arcue(mono, arcue, cue):
    proc = subprocess.run(
        [mono, str(arcue), str(cue)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    return proc.stdout + proc.stderr


def parse_arcue(output, n_tracks):
    """Reduce ARCUE output to a verdict for one disc."""
    lines = output.splitlines()
    ar_start = next((i for i, l in enumerate(lines) if "AccurateRip ID" in l), len(lines))
    ctdb_lines, ar_lines = lines[:ar_start], lines[ar_start:]

    # --- CTDB (CUETools database; normalizes read offset internally) ---
    ctdb_found = any("CTDB TOCID" in l and "found" in l and "not found" not in l for l in ctdb_lines)
    ctdb_ok = set()        # track numbers our rip matches, and isn't outvoted on
    ctdb_minconf = None
    for l in ctdb_lines:
        m = CTDB_TRACK_RE.match(l)
        if not m:
            continue
        track, rest = int(m.group(1)), m.group(2)
        acc = CTDB_ACC_RE.search(rest)
        if not acc:
            continue  # "Differs in N samples": nothing in CTDB matches us at all
        conf = int(acc.group(1))
        # A line can offer alternatives ("..., or (339/389) differs in 43 samples"):
        # entries holding audio unlike ours. Being outvoted only means we are
        # damaged if hardly anyone shares our audio -- if hundreds do, the disc
        # simply has more than one pressing and ours is a legitimate one.
        rival = max((int(c) for c, _ in CTDB_DIFF_RE.findall(rest)), default=0)
        if rival > conf and conf < MIN_CTDB_VARIANT:
            continue
        ctdb_ok.add(track)
        ctdb_minconf = conf if ctdb_minconf is None else min(ctdb_minconf, conf)
    ctdb_full = n_tracks > 0 and len(ctdb_ok) == n_tracks

    # --- AccurateRip (matched per read offset) ---
    ar_found = any("AccurateRip ID" in l and "found." in l and "not found" not in l for l in ar_lines)
    offset = 0  # the first (unlabeled) block is offset 0
    ar_total = 0              # total submissions for this disc (denominator in "(x/total)")
    accurate_by_offset = {}   # offset -> set(track numbers accurately ripped)
    minconf_by_offset = {}    # offset -> min confidence across that offset's accurate tracks
    for l in ar_lines:
        mo = AR_OFFSET_RE.match(l)
        if mo:
            offset = int(mo.group(1))
            continue
        mt = AR_TRACK_RE.match(l)
        if mt:
            ar_total = max(ar_total, int(mt.group(3)))
            if mt.group(4) == "Accurately ripped":
                track, conf = int(mt.group(1)), int(mt.group(2))
                accurate_by_offset.setdefault(offset, set()).add(track)
                prev = minconf_by_offset.get(offset)
                minconf_by_offset[offset] = conf if prev is None else min(prev, conf)

    # Offset where every track matched; prefer offset 0, then smallest magnitude.
    full = [o for o, tracks in accurate_by_offset.items() if len(tracks) == n_tracks]
    ar_offset = min(full, key=lambda o: (o != 0, abs(o))) if full else None
    # Best partial match (for reporting when not all tracks match at one offset).
    best = max(accurate_by_offset, key=lambda o: len(accurate_by_offset[o]), default=None)

    return {
        "ctdb_found": ctdb_found,
        "ctdb_full": ctdb_full,
        "ctdb_minconf": ctdb_minconf,
        "ctdb_matched": len(ctdb_ok),
        "ar_found": ar_found,
        "ar_total": ar_total,
        "ar_offset": ar_offset,
        "ar_minconf": minconf_by_offset.get(ar_offset) if ar_offset is not None else None,
        "ar_best": (best, len(accurate_by_offset[best])) if best is not None else None,
        "n_tracks": n_tracks,
    }


def verdict(r):
    """Map parsed results to (status, detail). status is a short label.

    An album passes if either database confirms all tracks. AccurateRip matching
    only at a nonzero offset (OK*) still means the audio is correct -- it just
    reflects a rip made without drive read-offset correction.
    """
    n = r["n_tracks"]
    ar_full = r["ar_offset"] is not None
    ctdb_full = r["ctdb_full"]
    if ar_full or ctdb_full:
        bits = []
        if ar_full:
            off = r["ar_offset"]
            off_note = "offset 0" if off == 0 else f"offset {off:+d}"
            bits.append(f"AccurateRip {n}/{n} conf {r['ar_minconf']} ({off_note})")
        elif r["ar_best"]:  # not all tracks, but report how close
            boff, bn = r["ar_best"]
            bits.append(f"AccurateRip {bn}/{n} (offset {boff:+d})")
        if ctdb_full:
            bits.append(f"CTDB {n}/{n} conf {r['ctdb_minconf']}")
        elif r["ctdb_matched"]:
            bits.append(f"CTDB {r['ctdb_matched']}/{n}")
        status = "OK" if (ar_full and r["ar_offset"] == 0) else "OK*"
        return status, ", ".join(bits)
    if r["ar_found"] or r["ctdb_found"]:
        # Too few submissions to be a meaningful reference (e.g. a lone CD-R rip
        # of a download-only release) -> inconclusive, not a real mismatch.
        if not r["ctdb_found"] and r["ar_total"] < MIN_AR_SUBMISSIONS:
            return "NOT IN DB", f"only {r['ar_total']} AccurateRip submission(s), CTDB absent - inconclusive"
        matched = max(r["ctdb_matched"], r["ar_best"][1] if r["ar_best"] else 0)
        return "DIFFERS", f"in database, only {matched}/{n} tracks match"
    return "NOT IN DB", "no AccurateRip/CTDB entry for this disc"


def main():
    ap = argparse.ArgumentParser(description="Verify rips against AccurateRip/CTDB.")
    ap.add_argument("paths", nargs="*", help="artist/album dirs to verify (default: --root)")
    ap.add_argument("--root", default=str(DEFAULT_ROOT), help=f"library root (default: {DEFAULT_ROOT})")
    ap.add_argument("--verbose", action="store_true", help="print full ARCUE output per album")
    args = ap.parse_args()

    mono = os.environ.get("MONO_BIN", "mono")
    arcue = Path(os.environ.get("ARCUE_EXE", DEFAULT_ARCUE))
    if not arcue.exists():
        sys.exit(f"ARCUE not found at {arcue} (set ARCUE_EXE or install CUETools; see README)")

    paths = args.paths or [args.root]
    units = find_units(paths)
    if not units:
        sys.exit("No FLAC albums found to verify.")

    print(f"Verifying {len(units)} album(s)...\n")
    rows, counts = [], {}
    for unit in units:
        label = str(unit).replace(str(Path.home()), "~")
        flacs = natural_flacs(unit)
        cue = write_cue(unit, flacs)
        try:
            output = run_arcue(mono, arcue, cue)
            if args.verbose:
                print(f"----- {label} -----\n{output}")
            status, detail = verdict(parse_arcue(output, len(flacs)))
        except subprocess.TimeoutExpired:
            status, detail = "ERROR", "ARCUE timed out"
        except Exception as e:  # noqa: BLE001 - report any failure, keep going
            status, detail = "ERROR", str(e)
        finally:
            cue.unlink(missing_ok=True)
        counts[status] = counts.get(status, 0) + 1
        rows.append((status, label, len(flacs), detail))
        print(f"  [{status:9}] {label}  ({len(flacs)} tracks)  {detail}")

    print("\n=== Summary ===")
    for status in sorted(counts):
        print(f"  {status:9}: {counts[status]}")
    print("\nLegend: OK = matched at offset 0 | OK* = matched at a nonzero read offset "
          "(rip is accurate, drive offset uncorrected) | DIFFERS = in DB but mismatched "
          "| NOT IN DB = disc unknown to the databases | ERROR = verifier failed")


if __name__ == "__main__":
    main()
