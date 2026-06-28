#!/usr/bin/env bash
# Rip the CD currently in the drive with whipper (AccurateRip-verified).
#   - rootful podman (real root can access /dev/sr0)
#   - drive read offset from $RIP_OFFSET (find yours with `whipper offset find`)
#   - output to ~/whipper/out, cover art embedded
# Pass extra whipper args through, e.g.:
#   -R <musicbrainz-release-id>  skip the release prompt
#   --unknown                    rip a disc with no MusicBrainz/CD-Text metadata
#                                (obscure/homemade discs); tag by hand afterward
set -euo pipefail

# Drive read offset, specific to your optical drive. Find it once with
# `whipper offset find` against a disc that's in AccurateRip.
RIP_OFFSET="${RIP_OFFSET:?set RIP_OFFSET to your drive read offset, e.g. 6}"

sudo podman run --rm -it --device /dev/sr0 --user 0 \
  -v "$HOME/whipper/out:/output:Z" \
  docker.io/whipperteam/whipper:latest \
  cd rip -o "$RIP_OFFSET" -O /output -C complete -k -p "$@"
