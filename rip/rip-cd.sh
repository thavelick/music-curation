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
