#!/usr/bin/env -S uv run --script
"""Check whether CTDB can repair an album's rip, and optionally do it.

Without --apply this only reports; nothing is written. See docs/ctdb-repair.md
for background, and read "What to actually worry about" in the README first:
most DIFFERS results are sub-millisecond and inaudible, and listening beats
repairing.

The check half:
  CTDB identifies a disc by its TOC, so a cue whose TOC doesn't match the disc's
  gets "disk not present in database" -- even for a disc with thousands of
  submissions. The usual cause is a disc that doesn't start track 1 at sector 0,
  which a naive one-file-per-track cue assumes it does. So: build a naive cue,
  let `lookup` (which queries with fuzzy=1) find the disc anyway, compare the TOC
  we sent against the best entry's, and if they differ by a constant N sectors,
  prepend N*588 samples of silence to a scratch copy of track 1 and declare it as
  an INDEX 00/INDEX 01 pregap. Then `verify` reports CanRecover.

The --apply half:
  `repair` emits one WAV of the whole disc, so each damaged track is cut back out
  by sector math (the entry's toc gives its range; a sector is 588 stereo
  samples). Those cuts are spliced into a *copy* of the album -- symlinks for the
  undamaged tracks -- and that copy is verified against CTDB before anything is
  overwritten, which turns an off-by-one into unmatched tracks rather than a
  quietly corrupted file. Only then are originals backed up, tags carried across,
  and the tracks installed.

  ReplayGain is recomputed across the whole album, not just the repaired tracks:
  album gain is a property of every track together.

  Do not try to check a splice by comparing CRC32 against verify's Remote=
  values -- that only holds when the entry matched at Offset: 0. See
  docs/ctdb-repair.md.

Prerequisites:
  - ctdb-cli built (see docs/ctdb-repair.md); scripts/ctdb-cli.sh wraps it
  - ffmpeg, metaflac
  - network access (queries CTDB)

Usage:
  scripts/ctdb_repair.py ALBUM                       # check only; writes nothing
  scripts/ctdb_repair.py --apply ALBUM               # repair, verify, install
  scripts/ctdb_repair.py --apply --work DIR ALBUM    # keep intermediates

Environment overrides:
  MUSIC_DIR   music library root            (default: ~/Music)
  CTDB_CLI    path to the ctdb-cli wrapper  (default: alongside this script)
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SAMPLES_PER_SECTOR = 588  # a CD sector is 588 stereo samples

MUSIC_DIR = Path(os.environ.get("MUSIC_DIR", Path.home() / "Music")).expanduser()
DEFAULT_CTDB_CLI = Path(__file__).resolve().parent / "ctdb-cli.sh"
# Backups live outside curated/ so a library sync never picks them up.
DEFAULT_BACKUP_ROOT = MUSIC_DIR / ".ctdb-backups"

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
    """Order FLACs by track, for the cue -- so this must match the disc order.

    Prefer a leading track number ("05 Title.flac"), since lexical order breaks
    once a disc reaches track 10. Fall back to the name when there's no leading
    number: not every album here is named that way ("Artist - Album - 05 -
    Title.flac"), and a constant prefix with padded numbers sorts correctly.
    Getting this wrong is silent -- the cue's TOC comes out wrong and CTDB just
    says the disc isn't in the database.
    """
    def key(p):
        m = re.match(r"(\d+)", p.name)
        return (int(m.group(1)) if m else 0, p.name)
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
    proc = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, cwd=cwd)
    return proc.stdout + proc.stderr


def must_run(cmd, what):
    proc = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if proc.returncode:
        sys.exit(f"error: {what} failed:\n{proc.stderr[:500]}")
    return proc.stdout


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
    """The `verify` block for one entry, keyed by CRC.

    verify emits an `Entry N:` block per CTDB entry -- hundreds of them, one per
    known variant of the disc -- each with its own Matched/Unmatched lines. Only
    the block for the entry we target says anything about our rip.
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


def build_aligned_cue(ctdb_cli, flacs, work, quiet=False):
    """Link FLACs into `work` and build a TOC-correct cue for them.

    Returns (cue, entry, delta). cue is None when CTDB has no entry, or when the
    TOC can't be reconciled by a constant shift.
    """
    work.mkdir(parents=True, exist_ok=True)
    for f in flacs:
        link = work / f.name
        if not link.exists() and not link.is_symlink():
            link.symlink_to(f.resolve())
    linked = track_sorted(work.glob("*.flac"))

    naive = work / "naive.cue"
    write_cue(naive, linked)

    out = run([ctdb_cli, "lookup", naive], cwd=work)
    sent = SENT_TOC_RE.search(out)
    entry = best_entry(out)
    if not sent or not entry:
        return None, None, None

    conf, etoc, crc, haspar = entry
    if not quiet:
        print(f"  best entry: confidence {conf}, crc32 {crc}, hasparity={haspar}")
        if not haspar:
            print("  !! entry has no parity -- repair is impossible even if damaged")

    delta = sector_delta(sent.group(1), etoc)
    if delta is None:
        print("  !! TOC shape differs from the entry's (not a constant shift).")
        print("     Different disc layout -- align by hand; see docs/ctdb-repair.md")
        return None, entry, None
    if not quiet:
        print(f"  TOC shift: {delta} sectors ({delta * SAMPLES_PER_SECTOR} samples)")

    if delta == 0:
        return naive, entry, delta

    first = linked[0]
    shifted = work / f"shifted_{first.name}"
    if not shifted.exists():
        must_run(
            ["ffmpeg", "-v", "error", "-y", "-i", first.resolve(),
             "-af", f"adelay={delta * SAMPLES_PER_SECTOR}S:all=1",
             "-c:a", "flac", "-sample_fmt", "s16", shifted],
            "shifting track 1",
        )
    cue = work / "aligned.cue"
    write_cue(cue, [shifted] + linked[1:], pregap=delta)
    return cue, entry, delta


def check(ctdb_cli, cue, crc):
    """Run verify; return (matched, unmatched, can_recover, correctable)."""
    out = run([ctdb_cli, "verify", cue], cwd=cue.parent)
    pad = PADDING_RE.search(out)
    if pad:
        print(f"  !! verify padded {pad.group(1)} samples -- alignment is still wrong")
    block = entry_block(out, crc)
    if block is None:
        return None, None, None, None
    recover = RECOVER_RE.search(block)
    correctable = CORRECTABLE_RE.search(block)
    return (
        block.count("[Matched]"),
        UNMATCHED_RE.findall(block),
        recover is not None and recover.group(1) == "True",
        int(correctable.group(1)) if correctable else None,
    )


def extract_track(wav, toc, n, dst):
    """Cut track n out of the whole-disc WAV using the entry's sector map."""
    start, end = toc[n - 1] * SAMPLES_PER_SECTOR, toc[n] * SAMPLES_PER_SECTOR
    must_run(
        ["ffmpeg", "-v", "error", "-y", "-i", wav,
         "-af", f"atrim=start_sample={start}:end_sample={end}",
         "-c:a", "flac", "-compression_level", "8", "-sample_fmt", "s16", dst],
        f"extracting track {n}",
    )


def backup_subpath(album):
    """Where under the backup root an album's originals go.

    Mirror the album's path under MUSIC_DIR so multi-disc albums keep their
    artist: "curated/Pink Floyd/The Wall/Disc 2", not "The Wall/Disc 2". Taking
    just parent/name drops the artist for Artist/Album/Disc N/ layouts.
    """
    album = album.resolve()
    try:
        return album.relative_to(MUSIC_DIR.resolve())
    except ValueError:
        return Path(album.name)  # album lives outside the library


def copy_tags(src, dst, work):
    """Carry src's tags to dst, minus ReplayGain (recomputed across the album)."""
    tags = must_run(["metaflac", "--export-tags-to=-", src], f"reading tags from {src.name}")
    kept = [l for l in tags.splitlines() if not l.startswith("REPLAYGAIN_")]
    tagfile = work / "tags.txt"
    tagfile.write_text("\n".join(kept) + "\n")
    must_run(["metaflac", "--remove-all-tags", f"--import-tags-from={tagfile}", dst],
             f"writing tags to {dst.name}")
    return len(kept)


def apply_repair(ctdb_cli, album, flacs, work, cue, entry, unmatched, backup_root):
    _, etoc, crc, _ = entry
    toc = [int(x) for x in etoc.split(":")]
    if len(toc) != len(flacs) + 1:
        sys.exit(f"error: entry TOC has {len(toc)} points for {len(flacs)} tracks")

    damaged = sorted(int(tn) for tn, _, _ in unmatched)
    print(f"\n  repairing track(s) {', '.join(f'{d:02d}' for d in damaged)} ...")
    out = run([ctdb_cli, "repair", cue, "--target", crc], cwd=work)
    if "Repair completed successfully" not in out:
        sys.exit("error: repair failed:\n" + out[-800:])
    wavs = list(work.glob("*_repaired.wav"))
    if not wavs:
        sys.exit("error: repair reported success but wrote no WAV")
    wav = wavs[0]

    # Splice into a copy: symlink the good tracks, write the repaired ones.
    staged = work / "staged"
    staged.mkdir(exist_ok=True)
    for i, f in enumerate(flacs, 1):
        dst = staged / f.name
        if dst.exists() or dst.is_symlink():
            dst.unlink()
        if i in damaged:
            extract_track(wav, toc, i, dst)
        else:
            dst.symlink_to(f.resolve())

    # Verify the copy before touching the library. This is the safety net: a
    # sector-math error shows up here as unmatched tracks, not as a corrupted
    # file in the library.
    print("  verifying the repaired copy ...")
    scue, sentry, _ = build_aligned_cue(
        ctdb_cli, track_sorted(staged.glob("*.flac")), work / "recheck", quiet=True)
    if scue is None:
        sys.exit("error: could not verify the repaired copy -- nothing installed")
    matched, still_bad, _, _ = check(ctdb_cli, scue, sentry[2])
    if still_bad is None or still_bad:
        n = len(still_bad) if still_bad else "?"
        sys.exit(f"error: repaired copy still has {n} unmatched track(s) "
                 "-- refusing to install")
    print(f"  repaired copy verifies clean ({matched} tracks matched)")

    backup = backup_root / backup_subpath(album)
    backup.mkdir(parents=True, exist_ok=True)
    for i in damaged:
        orig = flacs[i - 1]
        shutil.copy2(orig, backup / orig.name)
        n = copy_tags(orig, staged / orig.name, work)
        shutil.copy2(staged / orig.name, orig)
        print(f"    installed {orig.name} ({n} tags)")
    print(f"  originals backed up to {backup}")

    # Album gain spans every track, so it must be redone when any one changes.
    must_run(["metaflac", "--add-replay-gain"] + [str(f) for f in flacs], "replaygain")
    print("  replaygain recomputed across the album")


def main():
    ap = argparse.ArgumentParser(
        description="Check whether CTDB can repair an album, and optionally do it.")
    ap.add_argument("album", type=Path, help="album directory of FLAC tracks")
    ap.add_argument("--apply", action="store_true",
                    help="perform the repair (default: report only, write nothing)")
    ap.add_argument("--work", type=Path,
                    help="work dir to create and keep (default: temp, removed on exit)")
    ap.add_argument("--backup", type=Path, default=DEFAULT_BACKUP_ROOT,
                    help=f"where to back up replaced tracks (default: {DEFAULT_BACKUP_ROOT})")
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

    try:
        print(f"{album.name}  ({len(flacs)} tracks)")
        cue, entry, _ = build_aligned_cue(ctdb_cli, flacs, work)
        if cue is None:
            if entry is None:
                print("  not found in CTDB -- nothing to repair against")
            return

        matched, unmatched, can_recover, correctable = check(ctdb_cli, cue, entry[2])
        if matched is None:
            print("  !! target entry absent from verify output")
            return

        print(f"  tracks: {matched} matched, {len(unmatched)} unmatched")
        if not unmatched:
            print("  rip already matches CTDB -- nothing to repair")
            return
        for tn, local, remote in unmatched:
            print(f"    track {tn}: local {local} != remote {remote}")
        if correctable is not None:
            print(f"  correctable errors: {correctable}")
        print(f"  CanRecover: {can_recover}")

        if not can_recover:
            print("  damage exceeds CTDB's parity -- re-rip or replace the disc")
            return
        if not args.apply:
            print(f'\n  repair with:\n    scripts/ctdb_repair.py --apply "{album}"')
            return

        apply_repair(ctdb_cli, album, flacs, work, cue, entry, unmatched, args.backup)
        print(f'\n  confirm with:\n    scripts/verify_rips.py "{album}"')
    finally:
        if tmp:
            tmp.cleanup()


if __name__ == "__main__":
    main()
