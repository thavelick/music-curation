#!/bin/bash

# Wrapper around ctdb-cli, used to repair a rip from CTDB parity data.
# See docs/ctdb-repair.md for setup and the full repair workflow.
#
# ctdb-cli isn't packaged anywhere — it's a source build of
# https://github.com/Masterisk-F/ctdb-cli (a CLI over the cuetools.net
# libraries). This wrapper runs the built DLL under dotnet, so callers get a
# stable path instead of a long invocation.
#
# The published native binary (publish/dependent/ctdb-cli) is deliberately not
# used: it only searches the default .NET install locations, so it fails with
# ".NET location: Not found" against a Homebrew dotnet. Running the DLL through
# dotnet works regardless of where the runtime lives.
#
# CTDB_CLI_DLL  path to ctdb-cli.dll  (default: ~/src/ctdb-cli/publish/dependent/ctdb-cli.dll)
# DOTNET_BIN    path to dotnet        (default: dotnet)

CTDB_CLI_DLL="${CTDB_CLI_DLL:-$HOME/src/ctdb-cli/publish/dependent/ctdb-cli.dll}"
DOTNET_BIN="${DOTNET_BIN:-dotnet}"

if ! command -v "$DOTNET_BIN" >/dev/null 2>&1; then
    echo "error: '$DOTNET_BIN' not found — the .NET 10 SDK is required." >&2
    echo "       brew install dotnet   (or set DOTNET_BIN)" >&2
    echo "       See docs/ctdb-repair.md" >&2
    exit 1
fi

if [[ ! -f "$CTDB_CLI_DLL" ]]; then
    echo "error: ctdb-cli is not built at:" >&2
    echo "       $CTDB_CLI_DLL" >&2
    echo "       Build it (see docs/ctdb-repair.md), or set CTDB_CLI_DLL." >&2
    exit 1
fi

exec "$DOTNET_BIN" "$CTDB_CLI_DLL" "$@"
