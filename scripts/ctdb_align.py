#!/usr/bin/env -S uv run --script
"""Build a TOC-aligned cue for an album and report whether CTDB can repair it.

This automates the fiddly half of docs/ctdb-repair.md. CTDB identifies a disc by
its TOC, so a cue whose TOC doesn't match the disc's gets you "disk not present
in database" -- even for a disc with thousands of submissions. The usual cause is
that the disc doesn't start track 1 at sector 0, which a naive one-file-per-track
cue assumes it does.

What it does:
  1. builds a naive cue from the FLAC tracks
  2. runs `ctdb-cli lookup`, which queries with fuzzy=1 and so finds the disc
     anyway, and compares the TOC it sent against the TOC on the best entry
  3. if they differ by a constant N sectors, prepends N*588 samples of silence to
     a scratch copy of track 1 and declares it as an INDEX 00/INDEX 01 pregap
  4. runs `ctdb-cli verify` and reports CanRecover plus any unmatched tracks

The library is never modified: the work directory holds symlinks to the real
FLACs, plus the one shifted copy of track 1. Use --work to keep it -- the cue it
leaves behind is the input to `ctdb-cli.sh repair`.

Verification here is CTDB-only, and repairability is not the same as damage: an
entry needs `hasparity` for repair to be possible at all.

Prerequisites:
  - ctdb-cli built (see docs/ctdb-repair.md); scripts/ctdb-cli.sh wraps it
  - ffmpeg (only when a shift is needed)
  - network access (queries CTDB)

Usage:
  scripts/ctdb_align.py ~/Music/curated/"Nine Inch Nails"/"Pretty Hate Machine"
  scripts/ctdb_align.py --work /tmp/nin ALBUM     # keep the cue for repair

Environment overrides:
  CTDB_CLI    path to the ctdb-cli wrapper  (default: alongside this script)
"""

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SAMPLES_PER_SECTOR = 588  # a CD sector is 588 stereo samples

DEFAULT_CTDB_CLI = Path(__file__).resolve().parent / "ctdb-cli.sh"

# `toc=` on the request URL ctdb-cli prints, and on each <entry> it returns.
SENT_TOC_RE = re.compile(r"[?&]toc=([\d:]+)")
ENTRY_RE = re.compile(r"<entry\s([^>]*)>")
ENTRY_TOC_RE = re.compile(r'toc="([\d:]+)"')
ENTRY_CONF_RE = re.compile(r'confidence="(\d+)"')
ENTRY_CRC_RE = re.compile(r'crc32="([0-9a-f]+)"')
UNMATCHED_RE = re.compile(r"Track (\d+): Local=(\w+) Remote=(\w+) \[Unmatched\]")
RECOVER_RE = re.compile(r"CanRecover: (True|False)")
CORRECTABLE_RE = re.compile(r"Correctable Errors: (\d+)")
PADDING_RE = re.compile(r"Padding (\d+) samples")


def track_sorted(flacs):
    """Order FLACs by their leading track number, not lexically."""
    def key(p):
        m = re.match(r"(\d+)", p.name)
        return int(m.group(1)) if m else 0
    return sorted(flacs, key=key)


def write_cue(path, files, pregap=0):
    lines = ['PERFORMER ""', 'TITLE ""']
    for i, f in enumerate(files, 1):
        lines.append(f'FILE "{f.name}" WAVE')
        lines.append(f"  TRACK {i:02d} AUDIO")
        if i == 1 and pregap:
            # INDEX 00/01, not PREGAP: PREGAP fixes the TOC but leaves the audio
            # at sector 0, which misaligns every track.
            lines.append("    INDEX 00 00:00:00")
            lines.append(f"    INDEX 01 00:00:{pregap:02d}")
        else:
            lines.append("    INDEX 01 00:00:00")
    path.write_text("\n".join(lines) + "\n")


def run(cmd, cwd=None):
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    return proc.stdout + proc.stderr


def best_entry(output):
    """Highest-confidence CTDB entry as (confidence, toc, crc32, hasparity)."""
    best = None
    for attrs in ENTRY_RE.findall(output):
        toc = ENTRY_TOC_RE.search(attrs)
        if not toc:
            continue
        conf = ENTRY_CONF_RE.search(attrs)
        crc = ENTRY_CRC_RE.search(attrs)
        cand = (
            int(conf.group(1)) if conf else 0,
            toc.group(1),
            crc.group(1) if crc else None,
            "hasparity" in attrs,
        )
        if best is None or cand[0] > best[0]:
            best = cand
    return best


def entry_block(output, crc):
    """The `verify` output block for one entry, keyed by its CRC.

    verify emits `Entry N:` blocks for every CTDB entry, each with its own
    per-track Matched/Unmatched lines. Only the block for the entry we're
    targeting says anything about our rip.
    """
    blocks = re.split(r"^Entry \d+:", output, flags=re.M)[1:]
    for b in blocks:
        m = re.search(r"CRC:\s*([0-9a-f]+)", b)
        if m and crc and m.group(1) == crc:
            return b
    return blocks[0] if blocks else None


def sector_delta(sent, entry):
    """Constant sector shift between two TOCs, or None if there isn't one."""
    a = [int(x) for x in sent.split(":")]
    b = [int(x) for x in entry.split(":")]
    if len(a) != len(b):
        return None
    deltas = {y - x for x, y in zip(a, b)}
    return deltas.pop() if len(deltas) == 1 else None


def main():
    ap = argparse.ArgumentParser(
        description="Build a TOC-aligned cue and report CTDB repairability."
    )
    ap.add_argument("album", type=Path, help="album directory of FLAC tracks")
    ap.add_argument("--work", type=Path, help="work dir to create and keep "
                                              "(default: a temp dir, removed on exit)")
    args = ap.parse_args()

    ctdb_cli = Path(os.environ.get("CTDB_CLI", DEFAULT_CTDB_CLI))
    if not ctdb_cli.exists():
        sys.exit(f"error: ctdb-cli wrapper not found: {ctdb_cli}")

    album = args.album.expanduser()
    flacs = track_sorted(album.glob("*.flac"))
    if not flacs:
        sys.exit(f"error: no FLAC files in {album}")

    tmp = None
    if args.work:
        work = args.work.expanduser()
        work.mkdir(parents=True, exist_ok=True)
    else:
        tmp = tempfile.TemporaryDirectory()
        work = Path(tmp.name)

    print(f"{album.name}  ({len(flacs)} tracks)")

    for f in flacs:
        link = work / f.name
        if not link.exists():
            link.symlink_to(f.resolve())
    linked = track_sorted(work.glob("*.flac"))

    naive = work / "naive.cue"
    write_cue(naive, linked)

    out = run([str(ctdb_cli), "lookup", str(naive)], cwd=work)
    sent = SENT_TOC_RE.search(out)
    entry = best_entry(out)
    if not sent or not entry:
        print("  not found in CTDB (no entries) -- nothing to repair against")
        return
    conf, etoc, crc, haspar = entry
    print(f"  best entry: confidence {conf}, crc32 {crc}, hasparity={haspar}")
    if not haspar:
        print("  !! entry has no parity data -- repair is impossible even if damaged")

    delta = sector_delta(sent.group(1), etoc)
    if delta is None:
        print("  !! TOC shape differs from the entry's (not a constant shift).")
        print("     Different disc layout -- align by hand; see docs/ctdb-repair.md")
        return
    print(f"  TOC shift: {delta} sectors ({delta * SAMPLES_PER_SECTOR} samples)")

    if delta == 0:
        cue = naive
    else:
        first = linked[0]
        shifted = work / f"shifted_{first.name}"
        if not shifted.exists():
            rc = subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-i", str(first.resolve()),
                 "-af", f"adelay={delta * SAMPLES_PER_SECTOR}S:all=1",
                 "-c:a", "flac", "-sample_fmt", "s16", str(shifted)],
                capture_output=True, text=True,
            )
            if rc.returncode:
                sys.exit(f"error: ffmpeg failed shifting track 1:\n{rc.stderr}")
        cue = work / "aligned.cue"
        write_cue(cue, [shifted] + linked[1:], pregap=delta)

    out = run([str(ctdb_cli), "verify", str(cue)], cwd=work)

    pad = PADDING_RE.search(out)
    if pad:
        print(f"  !! verify padded {pad.group(1)} samples -- alignment is still wrong")

    # verify prints a block per CTDB entry (hundreds of them, one per known
    # variant of the disc). Only our target entry's block is meaningful --
    # the rest compare us against other people's rips.
    block = entry_block(out, crc)
    if block is None:
        print("  !! target entry absent from verify output")
        return

    recover = RECOVER_RE.search(block)
    correctable = CORRECTABLE_RE.search(block)
    unmatched = UNMATCHED_RE.findall(block)
    matched = block.count("[Matched]")

    print(f"  tracks: {matched} matched, {len(unmatched)} unmatched")

    if not unmatched:
        print("  rip already matches CTDB -- nothing to repair")
        return

    for tn, local, remote in unmatched:
        print(f"    track {tn}: local {local} != remote {remote}")
    if correctable:
        print(f"  correctable errors: {correctable.group(1)}")

    can = recover is not None and recover.group(1) == "True"
    print(f"  CanRecover: {can}")
    if can:
        print(f"\n  repair with:\n    scripts/ctdb-cli.sh repair {cue} --target {crc}")
        if not args.work:
            print("  (re-run with --work DIR to keep the cue)")
    else:
        print("  damage exceeds CTDB's parity -- re-rip or replace the disc")

    if tmp:
        tmp.cleanup()


main()
