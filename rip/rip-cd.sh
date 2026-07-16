#!/usr/bin/env bash
# Rip the CD currently in the drive with whipper (AccurateRip-verified).
#   - rootful podman (real root can access /dev/sr0)
#   - drive read offset from $RIP_OFFSET (find yours with `whipper offset find`)
#   - output to ~/whipper/out, cover art embedded
#   - any track whipper skips (unreadable on a used/scratched disc) is then
#     rescued by rescue-skipped.py so the output dir is still complete; rescued
#     tracks are flagged best-effort (NOT AccurateRip-verified)
# Pass extra whipper args through, e.g.:
#   -R <musicbrainz-release-id>  skip the release prompt
#   --unknown                    rip a disc with no MusicBrainz/CD-Text metadata
#                                (obscure/homemade discs); tag by hand afterward
set -euo pipefail

# Drive read offset, specific to your optical drive. Find it once with
# `whipper offset find` against a disc that's in AccurateRip.
RIP_OFFSET="${RIP_OFFSET:?set RIP_OFFSET to your drive read offset, e.g. 6}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE=docker.io/whipperteam/whipper:latest
DEVICE=/dev/sr0

# Whipper: bit-perfect, AccurateRip-verified rip. Skips tracks it can't verify.
sudo podman run --rm -it --device "$DEVICE" --user 0 \
  -v "$HOME/whipper/out:/output:Z" \
  "$IMAGE" \
  cd rip -o "$RIP_OFFSET" -O /output -C complete -k -p "$@"

# Rescue pass: fill in any tracks whipper skipped with best-effort cd-paranoia
# extraction, so the output dir is complete. The disc is still in the drive.
# The script is fed over stdin, so nothing extra needs deploying to the host.
echo "Checking for tracks whipper skipped..."
sudo podman run --rm -i --device "$DEVICE" --user 0 \
  -v "$HOME/whipper/out:/output:Z" \
  -e RIP_OFFSET="$RIP_OFFSET" \
  --entrypoint python3 \
  "$IMAGE" - /output --device "$DEVICE" < "$SCRIPT_DIR/rescue-skipped.py"

# Speed summary: reconstruct how long the rip took vs. the disc's runtime from
# the whipper log (it records per-track length + extraction speed, not wall
# time). A loud, fast rip (high x) means a clean disc the drive could spin up
# on; a slow one means the drive throttled down to re-read a marginal disc.
# out/*/ is whipper's release-type dir (album, live, unknown, ...), so glob it.
LOG="$(ls -t "$HOME"/whipper/out/*/*/*.log 2>/dev/null | head -1)"
if [ -n "$LOG" ]; then
  python3 - "$LOG" <<'PY' || echo "  (speed analysis skipped)"
import re, sys

text = open(sys.argv[1], encoding="utf-8", errors="replace").read()

def frames_to_sec(s):  # TOC lengths are MM:SS:FF, 75 frames/sec
    m, sec, ff = map(int, s.split(":"))
    return m * 60 + sec + ff / 75.0

lengths = [frames_to_sec(x) for x in re.findall(r"Length:\s+(\d+:\d+:\d+)", text)]
speeds = [float(x) for x in re.findall(r"Extraction speed:\s+([\d.]+)\s*X", text)]
n = min(len(lengths), len(speeds))
lengths, speeds = lengths[:n], speeds[:n]
if not n:
    sys.exit("  (no speed data in log)")

runtime = sum(lengths)
riptime = sum(l / s for l, s in zip(lengths, speeds))  # length/speed = time/track
hms = lambda s: f"{int(s // 60)}:{int(s % 60):02d}"
print(
    f"Rip speed: {hms(runtime)} audio in {hms(riptime)} "
    f"(avg {runtime / riptime:.1f}x, range {min(speeds):.1f}-{max(speeds):.1f}x)"
)
PY
fi

echo "Ejecting $DEVICE..."
eject "$DEVICE" || echo "  (could not eject $DEVICE automatically -- eject it by hand)"
