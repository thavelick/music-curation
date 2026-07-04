#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#     "numpy>=1.26.0",
#     "soundfile>=0.12.1",
# ]
# ///
"""
Analyze the audio in an album directory for loudness-war symptoms.

For each track this reports:
  - Peak level (dBFS)
  - RMS level (dBFS)
  - Crest factor (peak - RMS, in dB) -- low values mean a crushed/limited master
  - DR: an approximation of the TT Dynamic Range meter (peak minus loud-RMS)
  - Clipping: count of clipped-sample runs (3+ consecutive samples at full scale)

Rough interpretation:
  DR >= 12   dynamic, healthy master
  DR 8-11    moderately compressed
  DR <= 7    crushed, loudness-war territory
Any real clipping runs are a strong sign of an over-hot master.
"""

import argparse
import math
from pathlib import Path

import numpy as np
import soundfile as sf

AUDIO_EXTS = {".flac", ".wav", ".aiff", ".aif"}
# A brickwall-limited master produces *long* flat runs at full scale. A few
# consecutive samples at 0 dBFS is just a peak normalized to the ceiling and is
# harmless, so we only count runs of this many samples or more as real clipping.
CLIP_RUN_MIN = 10


def db(x):
    """Convert a linear amplitude (0..1) to dBFS, guarding against log(0)."""
    return 20 * math.log10(x) if x > 0 else -math.inf


def count_clip_runs(sample_abs, threshold):
    """
    Detect brickwall clipping: runs of >= CLIP_RUN_MIN consecutive full-scale
    samples. Returns (run_count, clipped_sample_pct) where the percentage counts
    only samples inside a qualifying run, relative to the whole track.
    """
    clipped = sample_abs >= threshold
    if not clipped.any():
        return 0, 0.0
    # Find run lengths of consecutive True values.
    padded = np.concatenate(([False], clipped, [False]))
    edges = np.diff(padded.astype(np.int8))
    starts = np.flatnonzero(edges == 1)
    ends = np.flatnonzero(edges == -1)
    run_lengths = ends - starts
    qualifying = run_lengths[run_lengths >= CLIP_RUN_MIN]
    runs = int(qualifying.size)
    pct = 100 * int(qualifying.sum()) / sample_abs.size
    return runs, pct


def estimate_dr(mono, block_sec, sr):
    """
    Approximate the TT Dynamic Range meter: peak (dB) minus the mean RMS of the
    loudest 20% of blocks (dB). Not the exact algorithm, but tracks it closely.
    """
    block = max(1, int(block_sec * sr))
    n_blocks = len(mono) // block
    if n_blocks < 1:
        return 0.0
    trimmed = mono[: n_blocks * block].reshape(n_blocks, block)
    rms_blocks = np.sqrt(np.mean(trimmed**2, axis=1))
    rms_blocks = rms_blocks[rms_blocks > 0]
    if rms_blocks.size == 0:
        return 0.0
    loud = np.sort(rms_blocks)[-max(1, rms_blocks.size // 5):]
    peak = np.abs(mono).max()
    return db(peak) - db(np.sqrt(np.mean(loud**2)))


def analyze(path):
    """Return a dict of loudness metrics for one audio file."""
    data, sr = sf.read(str(path), always_2d=True)
    # Collapse to mono for level metrics; keep per-channel for clip detection.
    mono = data.mean(axis=1)
    abs_all = np.abs(data)

    peak = abs_all.max()
    rms = np.sqrt(np.mean(mono**2))
    # Full-scale threshold just below 1.0 to catch samples pinned at the ceiling.
    clip_runs, clip_pct = count_clip_runs(abs_all.max(axis=1), 0.9995)

    return {
        "name": path.name,
        "peak_db": db(peak),
        "rms_db": db(rms),
        "crest_db": db(peak) - db(rms),
        "dr": estimate_dr(mono, 0.3, sr),
        "clip_runs": clip_runs,
        "clip_pct": clip_pct,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Analyze an album directory for loudness-war symptoms"
    )
    parser.add_argument("directory", help="Album directory containing audio files")
    args = parser.parse_args()

    directory = Path(args.directory).expanduser()
    if not directory.is_dir():
        print(f"Error: '{directory}' is not a directory")
        return 1

    files = sorted(
        p for p in directory.iterdir() if p.suffix.lower() in AUDIO_EXTS
    )
    if not files:
        print(f"No audio files found in {directory}")
        return 1

    print(f"Analyzing {len(files)} tracks in {directory.name}\n")
    header = (
        f"{'Track':40s} {'Peak':>7s} {'RMS':>7s} {'Crest':>6s} "
        f"{'DR':>5s} {'Clips':>7s} {'Clip%':>7s}"
    )
    print(header)
    print("-" * len(header))

    drs = []
    for path in files:
        m = analyze(path)
        drs.append(m["dr"])
        # Only flag when the track has sustained clipping runs *and* they cover a
        # non-trivial fraction of the track -- that's true brickwall limiting.
        flag = "  <-- clipping" if m["clip_runs"] > 0 and m["clip_pct"] >= 0.01 else ""
        print(
            f"{m['name'][:40]:40s} "
            f"{m['peak_db']:6.2f} "
            f"{m['rms_db']:6.2f} "
            f"{m['crest_db']:5.1f} "
            f"{m['dr']:5.1f} "
            f"{m['clip_runs']:7d} "
            f"{m['clip_pct']:6.3f}%{flag}"
        )

    avg_dr = sum(drs) / len(drs)
    print("-" * len(header))
    print(f"\nAlbum average DR: {avg_dr:.1f}")
    if avg_dr <= 7:
        verdict = "crushed -- classic loudness-war master"
    elif avg_dr <= 11:
        verdict = "moderately compressed"
    else:
        verdict = "dynamic / healthy"
    print(f"Verdict: {verdict}")

    print(
        "\nHow to read this:\n"
        "  DR (Dynamic Range) is the main signal -- peak minus the RMS of the\n"
        "  loudest sections, in dB. Higher = more dynamic. Loudness-war mastering\n"
        "  shrinks it by pushing the average level up toward the peak ceiling.\n"
        "    DR 14+    very dynamic (jazz, classical, well-mastered rock)\n"
        "    DR 12-13  healthy, dynamic\n"
        "    DR 8-11   compressed\n"
        "    DR <= 7   crushed / brickwalled\n"
        "  Crest is peak minus whole-track RMS -- it tracks DR and tells the same\n"
        "  story. Clip% is only a curiosity: a low value does NOT mean a good\n"
        "  master. Modern limiters crush dynamics cleanly without hard-clipping,\n"
        "  so always judge by DR, not by clipping."
    )


if __name__ == "__main__":
    main()
