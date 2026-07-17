# Music Library Organization Guide

This guide explains how to properly organize, tag, and prepare music files for the curated music library.

## Overview

This repository holds the helper scripts and the guide I use to curate my music library: a high-quality, properly tagged collection with complete metadata and artwork. The goal is to have the best available quality for each album, organized consistently so it works seamlessly with Jellyfin and other media servers that expect an Artist/Album folder structure.

The music files themselves live outside this repo (by default in `~/Music`); this repo is just the tooling and documentation.

### Running the scripts

The Python scripts under `scripts/` declare their dependencies inline ([PEP 723](https://peps.python.org/pep-0723/)) and have a `uv` shebang, so once [`uv`](https://docs.astral.sh/uv/) is installed you can run them directly:

```bash
scripts/verify_rips.py
```

The shebang runs each script through `uv`, which creates a throwaway environment with the right dependencies automatically. If you'd rather manage dependencies yourself, the requirements are `mutagen`, `musicbrainzngs`, and `requests`. Several scripts also shell out to external tools (`ffmpeg`/`ffprobe`, `metaflac`, `rsgain`, `mono`, `rsync`, `imv`) — see each section for specifics.

## Configuration

Scripts and workflow commands are parameterized by environment variables so there's nothing machine-specific hardcoded. Set the ones relevant to what you're doing:

| Variable | Used by | Default | Purpose |
|----------|---------|---------|---------|
| `MUSIC_DIR` | most scripts | `~/Music` | Local music library root (contains `curated/`, `classical/`, etc.) |
| `RIP_HOST` | ripping workflow | — | SSH host/alias of the machine with the optical drive |
| `RIP_OFFSET` | `rip/rip-cd.sh` | — | Your optical drive's read offset (find with `whipper offset find`) |
| `JELLYFIN_HOST` | `sync_to_jellyfin.py` | — | Hostname of the remote Jellyfin server (rsync/SSH target) |
| `JELLYFIN_FILES_PATH` | `sync_to_jellyfin.py` | — | Base path for music files on the Jellyfin server |
| `JELLYFIN_API_KEY` | `sync_to_jellyfin.py --scan` | — | Jellyfin API key (Dashboard → API Keys); only needed for `--scan` |
| `JELLYFIN_API_URL` | `sync_to_jellyfin.py --scan` | `http://$JELLYFIN_HOST:8096` | Jellyfin web API base URL |
| `MONO_BIN` | `verify_rips.py` | `mono` | Path to the `mono` binary |
| `ARCUE_EXE` | `verify_rips.py` | `$MUSIC_DIR/tools/CUETools_2.2.6/CUETools.ARCUE.exe` | Path to the CUETools verifier |

## Ripping CDs

CDs are ripped with [whipper](https://github.com/whipper-team/whipper), on the machine with the optical drive (referenced here via the `$RIP_HOST` SSH alias).

whipper drives `cdparanoia` underneath, but adds what matters for an archive: it **verifies each track against AccurateRip as it rips**, **corrects the drive read offset** (good rips then match at offset 0), **retries** unreadable tracks, and writes a per-track log. So you learn a disc read badly while it's still in the drive, instead of months later.

### Accurate ripping with whipper

**Setup:** whipper has fiddly native deps, so it runs from its **official container** via **rootful** podman — rootless podman can't reach the optical drive through the user-namespace mapping. This relies on passwordless `sudo podman` (a rule in `/etc/sudoers.d/podman-nopasswd`). The wrapper script [`rip/rip-cd.sh`](rip/rip-cd.sh) reads the drive's read offset from `$RIP_OFFSET` — find yours once with `whipper offset find` against a disc that's in AccurateRip. Deploy the wrapper to the rip host, e.g.:

```bash
scp rip/rip-cd.sh rip/rescue-skipped.py "$RIP_HOST":~/whipper/
```

(Deploy both files together — `rip-cd.sh` runs `rescue-skipped.py` from the same directory; see [Recovering skipped tracks](#recovering-skipped-tracks) below.)

**To rip a disc** (insert it first; `-t` gives an interactive terminal for the release prompt):

```bash
ssh -t "$RIP_HOST" 'RIP_OFFSET=6 ~/whipper/rip-cd.sh'
```

The wrapper runs:

```bash
sudo podman run --rm -it --device /dev/sr0 --user 0 \
  -v ~/whipper/out:/output:Z \
  docker.io/whipperteam/whipper:latest \
  cd rip -o "$RIP_OFFSET" -O /output -C complete -k -p
```

- **`-o "$RIP_OFFSET"`** read offset · **`-p`** prompt to pick the MusicBrainz release · **`-C complete`** cover art · **`-k`** keep going if a track fails
- Output lands in `~/whipper/out/<release-type>/<Artist> - <Album>/` on the rip host (one flat folder per disc), with a per-track AccurateRip verdict in the log. `<release-type>` is the release's **MusicBrainz type, lowercased** — whipper's default disc template leads with it — so most rips land in `out/album/`, but a live album lands in `out/live/`, an EP in `out/ep/`, and a disc with no MusicBrainz match in [`out/unknown/`](#discs-musicbrainz-doesnt-know---unknown). Don't assume `out/album/`; check `ls ~/whipper/out/`.

After whipper finishes, the wrapper runs a **rescue pass** (below) to fill in any tracks whipper skipped, so the output dir is always complete. It also prints a one-line speed summary (audio runtime vs. reconstructed rip time); a low average × flags a disc the drive had to throttle down to re-read.

**Pull it down.** The files are root-owned but world-readable, so a plain `rsync` reads them (no `sudo` — the rip host has no passwordless `sudo` anyway):

```bash
rsync -av "$RIP_HOST:whipper/out/album/Artist - Album/" \
  ~/Music/curated/"Artist"/"Album"/
```

(Swap `album` for whatever release-type dir the rip actually landed in — `live`, `ep`, ….)

**Then curate.** A whipper rip lands unnamed/undertagged, so before the [Processing Workflow](#processing-workflow) fix these up:

- **Rename** `NN. Artist - Title.flac` → `NN Title.flac` (the library layout).
- **DATE** → `YYYY` (whipper writes e.g. `2007-11`).
- **GENRE** — missing; set it (e.g. from the `<genre>` [`fetch_nfo.py`](#getting-nfo-metadata) writes to `album.nfo`).
- **ReplayGain** — missing; add per [ReplayGain](#replaygain-volume-normalization).

**Or automate the whole thing.** [`scripts/curate_whipper_rip.py`](scripts/curate_whipper_rip.py) drives the entire post-rip flow above end to end — pull, rename, NFO, tags, ReplayGain, artist image, lyrics, sidecar cleanup, verification, tag/image checks, and (if everything checks out) sync to Jellyfin:

```bash
scripts/curate_whipper_rip.py                 # curate the newest rip on $RIP_HOST
scripts/curate_whipper_rip.py battle          # curate the rip whose folder name matches "battle"
scripts/curate_whipper_rip.py --wait-for-rip  # wait for the running rip to finish, then curate it
```

It searches **every** release-type dir under `~/whipper/out/`, so it finds a `live/` or `ep/` rip the same as an `album/` one — with no argument it takes the newest rip of any type, and a substring matches against the `<Artist> - <Album>` folder name (not the type prefix, so `live` won't match every live rip). The release types it will curate are listed in `CURATABLE_RELEASE_TYPES` at the top of the script. That list gates *curation*, not *discovery*: a rip in some type dir not on the list still shows up and aborts with a message naming it, so a type MusicBrainz adds later can't silently vanish — it just needs adding to the list.

**`--wait-for-rip`** lets you kick off curation while a disc is still ripping and hand off automatically: it blocks until the [`rip-cd.sh`](#accurate-ripping-with-whipper) wrapper on the rip host exits, then curates the newest rip. Because a wrapper that was interrupted (e.g. Ctrl-C'd partway) *also* just disappears, process-exit alone can't distinguish a finished rip from an aborted one — so `rip-cd.sh` touches a `.rip-complete` sentinel in the output folder only after a full rip **and** rescue pass (its `set -e` aborts earlier on any failure), and `--wait-for-rip` refuses to curate a rip that lacks the sentinel rather than pull a partial one. The flag watches only the `rip-cd.sh` wrapper (not a bare `whipper` call), must be passed while the rip is already running (it doesn't wait for a *future* rip to appear), and is a no-op — proceeding straight to curation — when nothing is running. The sentinel stays on the rip host; it's excluded from the library pull. Deploy the wrapper that writes it with the `scp` above.

It only syncs to Jellyfin automatically when **all** of these hold: every track's AccurateRip verdict in the whipper log was a verified match (and no `RESCUED-TRACKS.txt`), [`verify_rips.py`](#verifying-rips-accuraterip) confirms `OK`/`OK*`, and a genre was found in MusicBrainz (the release group's top-voted genre, falling back to the artist's — the same source [`fetch_nfo.py`](#getting-nfo-metadata) uses, but looked up independently, so a `fetch_nfo.py` failure doesn't by itself block the sync). Otherwise it still curates the album but prints exactly which gate(s) failed and skips the sync. It aborts to manual curation for a rip folder that looks like one disc of a [multi-disc set](#multi-disc-albums), or if the destination album folder already exists (in which case, investigate and resume with the individual scripts). A disc with [no MusicBrainz match at all](#discs-musicbrainz-doesnt-know---unknown) is curated as far as it can go without metadata, then staged in `incoming/` for hand-tagging.

**Speed:** whipper is deliberately slow — a one-time subchannel scan plus careful per-track extraction — and it's interactive. That's the price of a verified rip.

#### Recovering skipped tracks

whipper only writes a track once it has a clean, consistent read — if it can't get one (a scratch or grime it can't read through), it **skips** the track entirely: no file, just a hole in the album and a warning in the log. That's the right call for a bit-perfect archive, but for a used CD you usually still want *something* listenable rather than a silent gap.

So `rip-cd.sh` runs [`rip/rescue-skipped.py`](rip/rescue-skipped.py) inside the same container right after whipper (the disc is still in the drive). For each track the cue references but has no file, it:

- re-extracts that one track with **`cd-paranoia`** in its error-concealing "paranoid" mode — unlike whipper, this always produces audio, even when it can't be verified;
- encodes it to FLAC and tags it to **match its sibling tracks** (album-level tags copied from a sibling; track number/title from the filename; per-track MusicBrainz IDs looked up best-effort);
- writes it into the album dir under the exact filename the cue expects.

Rescued tracks are **best-effort and NOT AccurateRip-verified.** The script says so on the console, drops a `RESCUED-TRACKS.txt` breadcrumb in the album dir, and — because used discs often aren't in AccurateRip/CTDB at all, so [`verify_rips.py`](#verifying-rips-accuraterip) can't help — prints the timestamp region where the drive's read-correction was heaviest, so you know **where to listen**. Treat a rescued track like a `DIFFERS`: [listen at the flagged spot](#what-to-actually-worry-about); if it's audibly bad, clean the disc and re-rip, or replace it. If whipper skipped nothing, the pass is silent.

#### Discs MusicBrainz doesn't know (`--unknown`)

An obscure or homemade disc may have no MusicBrainz release *and* no CD-Text. whipper's release prompt then has nothing to offer and it refuses to rip, so pass `--unknown` through the wrapper:

```bash
ssh -t "$RIP_HOST" 'RIP_OFFSET=6 ~/whipper/rip-cd.sh --unknown'
```

The audio is still ripped accurately — only the *metadata* is missing. With no release to name it after, whipper files it under the `unknown` release type, keyed by disc ID, with placeholder tags and filenames:

```
~/whipper/out/unknown/Unknown Artist - ZKI8bsYgnj8JMN8xw_UNOzyLbVQ-/
  01. Unknown Artist - Unknown Track 1.flac
  ...
```

[`curate_whipper_rip.py`](scripts/curate_whipper_rip.py) still handles these, but only as far as it can without metadata. It recognizes the placeholder and **stages the album in `$MUSIC_DIR/incoming/<discid>/` instead of `curated/`** — deliberately outside the library, so a nameless disc can never reach Jellyfin — then does everything that doesn't need MusicBrainz and stops:

| Runs | Skipped (all need an artist/album to look up) |
|------|-----------------------------------------------|
| Preflight (AccurateRip verdicts from the whipper log) | NFO + genre |
| `rsync` pull, rename to `NN Title.flac` | Artist image |
| `TRACKNUMBER` (zero-padded) + `TRACKTOTAL` | Lyrics |
| ReplayGain | Tag/image checks |
| [`verify_rips.py`](#verifying-rips-accuraterip) | Jellyfin sync (never, for placeholder tags) |

Track numbers come off the disc and ReplayGain is pure audio analysis, so both work fine with no metadata. **Verification works too** — AccurateRip and CTDB are keyed on the disc **TOC**, not on MusicBrainz, so a disc MusicBrainz has never heard of can still verify perfectly. (The example disc above returns `[OK] AccurateRip 13/13, CTDB 13/13` despite having no MusicBrainz release at all.) A genuinely homemade disc will come back `NOT IN DB`; that's expected, and the whipper log's per-track verdicts are then your only verification.

It finishes by printing what's left for you, including the **MusicBrainz `cdtoc/attach` URL** whipper logged for the disc. That one's worth following: whipper computes the disc ID and TOC whether or not a release matched, and attaching the TOC to a release (creating it if needed) is what makes the *next* rip of that disc match automatically. Then, per [Tagging Files](#tagging-files): tag `ARTIST`/`ALBUM`/`TITLE`/`DATE`/`GENRE`, rename to the real titles, drop in a `cover.jpg` by hand (the [artwork](#getting-artwork) scripts need a MusicBrainz match), move it from `incoming/` into `curated/<Artist>/<Album>/`, and [sync](#syncing-to-jellyfin-server).

Note that `unknown/` means whipper found **no release *type***, which isn't quite the same as no metadata — a release that MusicBrainz knows but whose release group has no type set also lands there, with its real artist and album intact. Those curate normally; only the `Unknown Artist - <discid>` placeholder gets staged in `incoming/`.

**Gotchas:**
- The read offset is specific to **your drive**. Find it once with `whipper offset find` (against a disc that's in AccurateRip) and set `RIP_OFFSET`.
- Volume mounts need the `:Z` SELinux label or whipper can't write to `/output`.

## Verifying Rips (AccurateRip)

You can confirm a rip is bit-accurate after the fact by checking it against the **AccurateRip** and **CUETools (CTDB)** databases — these compare your tracks' checksums against thousands of other people's rips of the same disc. whipper already checks AccurateRip during a rip, but this works on the FLACs you already have, so it covers albums that predate whipper or came from elsewhere.

This works on the FLAC files you already have — the original CD is **not** needed. AccurateRip identifies a disc by its track layout, which is reconstructed from the FLAC sample counts.

### Tooling

Verification uses [CUETools](http://cuetools.net/)' headless verifier (`CUETools.ARCUE.exe`) run under mono:

- **mono** — `sudo pacman -S mono`
- **CUETools** — extracted to `$MUSIC_DIR/tools/CUETools_2.2.6/` (download `CUETools_<version>.zip` from the [releases page](https://github.com/gchudov/cuetools.net/releases) and unzip it there), or point `ARCUE_EXE` wherever you put it

### Usage

The helper script `scripts/verify_rips.py` handles everything — it finds each album of FLAC tracks, builds a temporary cue sheet, runs the verifier, and prints a verdict:

```bash
# Verify the whole curated library
scripts/verify_rips.py

# Verify one artist or album
scripts/verify_rips.py ~/Music/curated/"Pink Floyd"

# Verify a different library root
scripts/verify_rips.py --root ~/Music/classical

# Show the full ARCUE output per album
scripts/verify_rips.py --verbose
```

Multi-disc albums are handled automatically (each `Disc N/` folder is verified as its own CD). Non-FLAC albums (`mp3/`, `m4a/`, etc.) are skipped — AccurateRip only works on lossless CD rips.

If `mono`/CUETools live elsewhere, point the script at them with the `MONO_BIN` and `ARCUE_EXE` environment variables.

### Reading the results

| Status | Meaning |
|--------|---------|
| `OK` | Matched AccurateRip at offset 0 — bit-perfect. |
| `OK*` | Matched, but only at a **nonzero read offset** (or via CTDB). The audio is accurate; the offset just means the rip wasn't offset-corrected. Normal and inaudible. |
| `DIFFERS` | The disc is in a database, but your tracks don't match — worth investigating (bad rip, or a different master/pressing). |
| `NOT IN DB` | Neither database has this disc. Expected for vinyl rips, downloads, obscure pressings, or anything not ripped from a common CD — **not** a sign of a bad rip. |
| `ERROR` | The verifier failed to run (e.g. timeout). |

**Note on read offset:** [whipper](#accurate-ripping-with-whipper) corrects the drive's read offset, so its rips match at offset 0. Albums ripped without that correction are still perfectly accurate — they just match at a small offset like `+6` (your drive's offset showing through) and report `OK*`. Both mean the audio is correct.

Treat a **low-confidence** AccurateRip offset with suspicion. A match at `conf 1`–`2` and some wild offset (`+1765`, `-1098`) is one or two strangers' rips aligning by chance, not a real offset — check what CTDB says at its much higher confidence before believing it.

### What to actually worry about

AccurateRip verifies **bit-perfect archival correctness — not audible quality.** A track that fails verification is almost always still perfectly listenable. Don't panic at `DIFFERS` or `OK*`; triage instead:

- **`OK` / `OK*`** — done. (`OK*` is just an uncorrected drive offset; the audio is correct and sounds identical.)
- **`NOT IN DB`** — not a problem. Expected for vinyl rips, downloads, digital-only releases, and obscure pressings — there's simply no reference to check against.
- **`DIFFERS`** — the only status worth a look, and even then usually benign. Use `--verbose` to see CTDB's per-track "differs in N samples @timestamps" and judge by **magnitude and contiguity**:
  - **A few dozen scattered samples** (sub-millisecond, non-contiguous) → cdparanoia smoothly concealed a tiny read hiccup. **Inaudible.** Leave it.
  - **Thousands of samples and/or long contiguous ranges** (e.g. `@2:04:10-2:11:25`) → potentially audible (clicks, dropouts). Worth investigating.

**The real test is your ears.** Before chasing a re-rip or a download, just listen at the flagged timestamps. Two things to keep in mind:
- "Fails AccurateRip" ≠ "sounds bad." Plenty of imperfect rips are sonically flawless.
- **Dense or distorted music masks artifacts** — and fools automated click-detectors. What looks like a glitch in the numbers is often the song's own transients or intentional noise. Trust the listen over the numbers.

**Decision guide for a genuine `DIFFERS`:**
1. **Listen** at the timestamps. Hear nothing? → leave it; it's an archival-only blemish, not a quality problem.
2. Hear a click/dropout? → **clean the disc and re-rip with [whipper](#accurate-ripping-with-whipper)** (it reports per-track whether you recovered it). For scratches, polish first, then re-rip.
3. Can't re-rip, or the re-rip differs the same way? → if CTDB has parity for the disc, you can **reconstruct the bad samples from other people's rips** — no disc needed. See [Repairing from CTDB parity](#repairing-from-ctdb-parity).
4. Disc physically unrecoverable? → **obtain a replacement copy of the same pressing** (match the TOC; verify the replacement with this same tool — `verify_rips.py /path/to/copy`) and splice in just the bad tracks.
5. Most of the time, step 1 ends it.

**Bottom line:** verification protects the archive's integrity, not your listening experience. Run it, fix the genuinely-audible cases, and don't lose sleep over the rest.

### Repairing from CTDB parity

For the rare `DIFFERS` that's genuinely audible and can't be re-ripped, CTDB entries carry Reed-Solomon parity, so the bad samples can often be **reconstructed from other people's rips** — no disc required. It's a manual path with a heavier setup than the rest of this repo (.NET 10 plus a source build of [`ctdb-cli`](https://github.com/Masterisk-F/ctdb-cli)), and it works even when the disc isn't in AccurateRip at all.

See **[docs/ctdb-repair.md](docs/ctdb-repair.md)** for setup, the TOC-alignment gotcha that makes it silently report `disk not present in database`, and a worked example.

## Directory Structure

### Organized Collections

Both `curated/` and `classical/` follow this structure:

```
curated/
├── Artist Name/
│   ├── folder.jpg              # Artist image (portrait/photo)
│   ├── Album Title/
│   │   ├── 01 - Track Name.flac
│   │   ├── 02 - Track Name.flac
│   │   ├── ...
│   │   ├── cover.jpg           # Album cover
│   │   └── Artwork/            # Optional: additional artwork
│   │       ├── Front.jpg
│   │       ├── Back.jpg
│   │       ├── Booklet 01.jpg
│   │       └── ...
│   └── Another Album/
│       └── ...
```

### Key Points

- **Artist folders** contain one `folder.jpg` artist image shared across all albums
- **Album folders** contain tracks numbered as `NN Track Name.ext` (no dash between number and title)
- **Album artwork** includes `cover.jpg` at the album root
- **Additional artwork** (alternative covers, booklets, back covers, etc.) goes in `Artwork/` subdirectory
- **EP naming** - EPs are named without "EP" suffix in folder names (e.g., "The Guitar" not "The Guitar EP")
- **Multi-disc albums** - Use `Disc 1/`, `Disc 2/` subdirectories within the album folder (see below)

### Multi-Disc Albums

For albums with multiple discs, use subdirectories within the album folder:

```
curated/
├── Artist Name/
│   ├── folder.jpg
│   └── Album Title/
│       ├── cover.jpg           # Album cover at root level
│       ├── Disc 1/
│       │   ├── 01 Track Name.flac
│       │   ├── 02 Track Name.flac
│       │   └── ...
│       └── Disc 2/
│           ├── 01 Track Name.flac
│           ├── 02 Track Name.flac
│           └── ...
```

**Important:** Each track must have the `discnumber` metadata tag set (1, 2, etc.) to ensure proper organization in media players. Track numbering restarts at 01 for each disc.

### Legacy Format Folders

Folders organized by format (`flac/`, `m4a/`, `mp3/`, `ogg/`) contain music that hasn't been properly cleaned up yet. These are candidates for reorganization into the proper artist/album structure.

## Quality Standards

The library prioritizes quality over format consistency. Use the best available quality:

1. **FLAC** (lossless) - preferred when available
2. **M4A/AAC** - high quality lossy (256-320 kbps)
3. **MP3** - 320 kbps preferred, lower bitrates acceptable if that's all that's available
4. **OGG Vorbis** - acceptable if that's the source format

Don't transcode between lossy formats - keep the original format even if quality is lower.

## Processing Workflow

### Complete Album Processing Checklist

When adding a new album to the curated library:

1. **Split multi-track files** (if needed) - see [Splitting FLAC+CUE Files](#splitting-flaccue-files)
2. **Create directory structure** - `Artist Name/Album Title/`
3. **Apply metadata tags** - see [Tagging Files](#tagging-files)
4. **Add ReplayGain tags** (if missing) - see [ReplayGain](#replaygain-volume-normalization)
5. **Fetch artwork** - see [Getting Artwork](#getting-artwork)
6. **Fetch lyrics** - run `scripts/fetch_lyrics.py` on the album to pull synced `.lrc` sidecars - see [Getting Lyrics](#getting-lyrics)
6a. **Fetch NFO metadata** - run `scripts/fetch_nfo.py` to write `artist.nfo`/`album.nfo` (bio, review, rating) - see [Getting NFO Metadata](#getting-nfo-metadata)
7. **Verify quality** - check files play correctly with proper durations
8. **Verify rip accuracy** (CD rips) - run `scripts/verify_rips.py` on the album and triage per [What to actually worry about](#what-to-actually-worry-about)
9. **Remove `.m3u` and `.cue` sidecars** - delete any `.m3u` playlist and `.cue` sheet from the album dir; they reference the pre-curation filenames and go stale once tracks are renamed. Keep the `.log`/`.toc` as rip provenance.
10. **Move to final location** - place in `curated/` or `classical/`

### Splitting FLAC+CUE Files

When you have a single FLAC file with a CUE sheet (common for album rips):

#### Step 1: Split the Audio

Use the splitting script:

```bash
scripts/split_flac.py /path/to/album/directory
```

This will:
- Automatically find the FLAC and CUE files in the directory
- Parse the CUE sheet to extract track information
- Split the FLAC file into individual tracks named `NN - Track Title.flac`
- Re-encode to FLAC to ensure correct durations and checksums

If there are multiple FLAC or CUE files in the directory, specify them explicitly:

```bash
scripts/split_flac.py /path/to/album --flac album.flac --cue album.cue
```

**Important:** The script uses `-c:a flac` (re-encoding) which ensures files have correct durations and checksums. Using `-c copy` is faster but creates incorrect metadata that needs fixing later.

#### Step 2: Verify Split Quality

After splitting, verify one track plays correctly and has the right duration:

```bash
ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 "01 - Track Name.flac"
```

### Tagging Files

#### Required Metadata Tags

All files should have these tags:

- **ALBUMARTIST** - Album artist name (must match the artist folder name exactly)
- **ARTIST** - Track artist (can be different for individual tracks)
  - For multiple artists, use semicolon separator: `Artist 1; Artist 2`
  - Do NOT use "feat." format - convert `Santana feat. Dave Matthews` to `Santana; Dave Matthews`
- **ALBUM** - Album title (should match album folder name)
- **TITLE** - Track title
- **DATE** - Release year (YYYY format)
- **GENRE** - Music genre
- **TRACKNUMBER** - Track number (01, 02, etc.)
- **TRACKTOTAL** - Total number of tracks
- **musicbrainz_artistid** - MusicBrainz artist ID (optional but recommended)

#### Reading Metadata Tags

Use `mutagen-inspect` to read tags from any audio format:

```bash
mutagen-inspect "track.flac"    # FLAC
mutagen-inspect "track.mp3"     # MP3
mutagen-inspect "track.m4a"     # M4A/AAC
mutagen-inspect "track.ogg"     # OGG Vorbis
```

#### Applying Tags by Format

**FLAC files** - use `metaflac`:

```bash
metaflac --set-tag=ALBUMARTIST="Artist Name" "track.flac"
metaflac --set-tag=ARTIST="Artist Name" "track.flac"
metaflac --set-tag=ALBUM="Album Title" "track.flac"
metaflac --set-tag=TITLE="Track Title" "track.flac"
metaflac --set-tag=DATE="2012" "track.flac"
metaflac --set-tag=GENRE="Genre" "track.flac"
metaflac --set-tag=TRACKNUMBER="01" "track.flac"
metaflac --set-tag=TRACKTOTAL="13" "track.flac"
```

**MP3 files** - use `mid3v2`:

```bash
mid3v2 --TPE2 "Artist Name" "track.mp3"        # Album artist
mid3v2 --artist "Artist Name" "track.mp3"      # Track artist
mid3v2 --album "Album Title" "track.mp3"
mid3v2 --song "Track Title" "track.mp3"
mid3v2 --year "2012" "track.mp3"
mid3v2 --genre "Genre" "track.mp3"
mid3v2 --track "1/13" "track.mp3"              # Track number/total
```

**M4A files** - use Python with mutagen:

```python
from mutagen.mp4 import MP4

audio = MP4("track.m4a")
audio["aART"] = ["Artist Name"]           # Album artist
audio["\xa9ART"] = ["Artist Name"]        # Track artist
audio["\xa9alb"] = ["Album Title"]        # Album
audio["\xa9nam"] = ["Track Title"]        # Title
audio["\xa9day"] = ["2012"]               # Year
audio["\xa9gen"] = ["Genre"]              # Genre
audio["trkn"] = [(1, 13)]                 # Track number, total tracks
audio.save()
```

**Important:** The `ALBUMARTIST` tag must exactly match the artist folder name. This ensures consistency across the library and proper grouping in music players.

#### Using Helper Scripts

The `scripts/` directory has automated tagging helpers. Each takes a directory — a
library root, an artist folder, or a single album — and operates on every track found.

**Report `ALBUMARTIST` tags that don't match the artist folder name:**
```bash
scripts/check_tags.py ~/Music/curated
```

**Fix album artist tags** (sets `ALBUMARTIST` to the artist folder name; `--dry-run` to preview):
```bash
scripts/fix_album_artist.py ~/Music/curated --dry-run
```

**Find multi-artist tracks not using the `; ` delimiter:**
```bash
scripts/check_multi_artist.py ~/Music/curated
```

### ReplayGain (volume normalization)

ReplayGain tags let players level playback volume so tracks don't jump in loudness. **Always make sure an album has ReplayGain tags before moving it to the library — add them if they're missing.**

**whipper does not apply ReplayGain**, and `metaflac --remove-all-tags` (used when retagging from scratch) wipes any existing `REPLAYGAIN_*`. So fresh rips and freshly-retagged albums need it added by hand. Note album gain is a property of the whole album, so recompute across every track whenever any one of them changes.

Check whether tags exist:

```bash
metaflac --export-tags-to=- "01 Track Name.flac" | grep -i replaygain || echo "no replaygain"
```

There are two tools, depending on the file. Use `metaflac` for ordinary stereo
FLAC; use `rsgain` for everything `metaflac` can't do (MP3, M4A/AAC, 5.1/surround,
and the occasional FLAC whose decoder `metaflac` chokes on). Note the library is
mixed-format — FLAC, MP3, and M4A — so when checking coverage, enumerate all
three extensions; a FLAC-only scan silently skips whole albums.

#### Stereo FLAC — `metaflac`

Add tags for a whole album at once (run from inside the album folder):

```bash
metaflac --add-replay-gain *.flac
```

This writes per-track `REPLAYGAIN_TRACK_*` plus a shared `REPLAYGAIN_ALBUM_*`
computed across all the files passed in one invocation. Run it once per disc so
each disc's album gain is computed over its own tracks.

`metaflac`'s ReplayGain analyzer is **stereo-only** and refuses multichannel
files (`# of channels (6) is not supported`). It also occasionally fails a
single track with `ERROR: during analysis (decoding file)`. In both cases, fall
back to `rsgain` below.

#### MP3, M4A, 5.1 surround, and problem FLAC — `rsgain`

[`rsgain`](https://github.com/complexlogic/rsgain) (`brew install rsgain`) uses
libebur128 (ITU-R BS.1770 loudness), so it handles **MP3**, **M4A/AAC**,
**multichannel (5.1/7.1)** — excluding the LFE channel per spec — and FLACs that
`metaflac` can't decode. Album mode, writing ReplayGain 2.0 tags in place:

```bash
rsgain custom -a -s i *.flac        # or *.mp3, *.m4a
```

`-a` = compute album gain, `-s i` = scan and write tags. Add `-s s` to scan and
print values **without** writing (a dry run). MP3/M4A tags are written as ID3v2
`TXXX:REPLAYGAIN_*` frames / MP4 atoms; FLAC tags are the same `REPLAYGAIN_*`
Vorbis comments `metaflac` writes, so players read either identically. (Note:
`rsgain` references −18 LUFS while `metaflac` references 89 dB SPL / ReplayGain 1
— the resulting gain values differ slightly, which is cosmetic and doesn't affect
leveling.)

#### Various-artists compilations

For compilations the tracks come from unrelated sources, so play them in **track
mode** (per-song leveling) rather than album mode — the shared album gain is
meaningless across a grab-bag of masters.

### Getting Artwork

#### Album Art

Use the album art fetching script:

```bash
scripts/fetch_album_art.py --directory ~/Music/curated/Artist\ Name
```

This will:
- Search MusicBrainz for the album
- Download cover art from Cover Art Archive
- Save as `cover.jpg` in album directories

#### Artist Images

Use the artist image fetching script:

```bash
scripts/fetch_artist_image.py --artist-dir ~/Music/curated/Artist\ Name
```

This will:
- Look for MusicBrainz artist ID in tags
- Search MusicBrainz if not found
- Download artist image from TheAudioDB
- Save as `folder.jpg` in artist directory
- Tag all files with MusicBrainz artist ID

**If artist image isn't available from TheAudioDB:**
1. Manually download an artist image
2. Copy to `Artist Name/folder.jpg`

#### Review and Verify Artwork

Use review scripts to check what's missing:

```bash
scripts/review_covers.sh ~/Music/curated         # Page through album covers with imv
scripts/review_artist_images.sh ~/Music/curated  # Page through artist images with imv
scripts/check_missing_images.py --directory ~/Music/curated  # Missing-image report
```

### Getting Lyrics

Fetch time-synced lyrics as `.lrc` sidecar files so Jellyfin shows scrolling,
karaoke-style lyrics during playback. Jellyfin attaches a lyric file to a track
when their basenames match (`01 Song.flac` ↔ `01 Song.lrc`), and picks them up
on a library scan — so fetch lyrics **before** [syncing to Jellyfin](#syncing-to-jellyfin-server).

Use the lyrics fetching script, which reads each track's `ARTIST`/`TITLE`/`ALBUM`
tags and duration, looks the song up on [LRCLIB](https://lrclib.net) (free, no API
key), and writes a matching `.lrc` next to each track:

```bash
scripts/fetch_lyrics.py ~/Music/curated/"Artist Name"/"Album Title"   # one album
scripts/fetch_lyrics.py ~/Music/curated/"Artist Name"                 # one artist
scripts/fetch_lyrics.py                                               # whole curated library
```

- Writes **synced** lyrics as `.lrc`. Tracks that already have a sidecar are
  skipped (pass `--overwrite` to replace them).
- LRCLIB matches on artist/title/album **and duration**, so accurate tags and
  correct durations give the best hit rate. This is another reason to
  [split by re-encoding](#splitting-flaccue-files) rather than stream-copying —
  wrong durations cause lyric misses just like they break rip verification.
- For tracks LRCLIB only has **unsynced** lyrics for, nothing is written by
  default; pass `--plain` to save those as `.txt` (Jellyfin still displays them,
  just without timing).

### Getting NFO Metadata

Write `artist.nfo` / `album.nfo` sidecars carrying the fields that have **no home
in audio tags** — artist biography, formed year, album review/rating, and
mood/style — so Jellyfin can show a proper artist bio and album review. These are
[Kodi-format](https://kodi.wiki/view/NFO_files/Music) XML files (Jellyfin reads the
same schema); the data is scraped from [TheAudioDB](https://www.theaudiodb.com)
(the same source as [artist images](#artist-images)), keyed off the MusicBrainz IDs
already embedded in your tracks. Genre/year still live in your tags — NFO only adds
what tags can't hold.

The script auto-detects whether the path is an album, an artist, or a library
root, and writes `artist.nfo` next to each `folder.jpg` and `album.nfo` next to each
`cover.jpg`. Fetch **before** [syncing to Jellyfin](#syncing-to-jellyfin-server) so
a scan picks them up:

```bash
scripts/fetch_nfo.py ~/Music/curated/"Artist Name"/"Album Title"   # one album.nfo
scripts/fetch_nfo.py ~/Music/curated/"Artist Name"                 # artist + its albums
scripts/fetch_nfo.py                                               # whole curated library
```

- Existing `.nfo` files are skipped; pass `--overwrite` to replace them.
- Uses the MusicBrainz **artist** ID for `artist.nfo` and the **release group** ID
  (`MUSICBRAINZ_RELEASEGROUPID`) for `album.nfo` — so accurate MB tags are what make
  a match. If a track has no MB IDs, run [`fetch_artist_image.py`](#artist-images)
  first (it looks them up and tags the files).
- Biographies and reviews are normalized to blank-line-separated paragraphs —
  TheAudioDB inconsistently uses single newlines, which Jellyfin would otherwise
  render as one wall of text.
- Jellyfin's music UI is thinner than its video UI: it renders the bio, genre,
  year, review, and rating, but folds `style` into artist **tags** and doesn't
  surface `mood` on music pages. The fields are still written (they feed tag-based
  browsing and Instant Mixes, and other clients may show them).
- `--lang xx` picks the biography language (default `en`), e.g. `--lang de`.
- NFO is a Kodi/Jellyfin/Emby convention, so unlike embedded tags it won't carry
  over to Plex/Navidrome — a small dent in the library's otherwise format-agnostic
  portability.

## Common Tasks

### Replacing Albums with Better Versions

When replacing an existing album in the curated library with a higher quality version:

1. **Archive the old version:**
   ```bash
   # Move old version to outdated folder with descriptive name
   mv "curated/Artist Name/Album Title" "outdated/Artist - Album Title (format quality)"
   ```

   Examples:
   - `outdated/TMBG - Indestructible Object (MP3 192kbps)`
   - `outdated/Pink Floyd - The Wall (MP3 256kbps)`

2. **Process the new version** following the standard [Complete Album Processing Checklist](#complete-album-processing-checklist)

3. **Verify the upgrade:**
   - Check file quality is better than archived version
   - Verify metadata is complete
   - Test playback

4. **Optional cleanup:**
   - Once confirmed the new version is working, you can delete from `outdated/`
   - Or keep archived versions as backup

### Reorganizing Format-Based Folders

When music is organized by format (in `flac/`, `m4a/`, etc.):

1. **Use reorganization script** (`--dry-run` to preview):
   ```bash
   scripts/reorganize_to_artist_folders.py --directory ~/Music/flac --dry-run
   ```

2. **This will:**
   - Read artist/album from file tags
   - Create proper Artist/Album directory structure
   - Move files to correct locations
   - Preserve existing artwork

### Adding Disc Number Metadata

For multi-disc albums, after organizing tracks into `Disc 1/` and `Disc 2/` subdirectories, add the `discnumber` metadata tag to each track:

**FLAC files:**
```bash
# For all tracks in Disc 1/
metaflac --set-tag=DISCNUMBER="1" Disc\ 1/*.flac

# For all tracks in Disc 2/
metaflac --set-tag=DISCNUMBER="2" Disc\ 2/*.flac
```

**MP3 files:**
```bash
# For all tracks in Disc 1/
for f in Disc\ 1/*.mp3; do mid3v2 --TPOS "1" "$f"; done

# For all tracks in Disc 2/
for f in Disc\ 2/*.mp3; do mid3v2 --TPOS "2" "$f"; done
```

**M4A files:** Use Python with mutagen (see Tagging Files section), setting the `disk` atom to `[(1, 2)]` for disc 1 of 2, etc.

You can also use the helper script, which reads the disc number from each `Disc N/`
subfolder and tags the tracks inside it (works on a single album or a whole root;
`--dry-run` to preview):
```bash
scripts/add_disc_numbers.py ~/Music/curated/"Pink Floyd"/"The Wall"
```

### Checking Album Names

Verify album folder names match metadata (checks `curated/` and `classical/` under `$MUSIC_DIR`):

```bash
scripts/check_album_names.sh
```

### Syncing to Jellyfin Server

After processing albums, sync the entire library to the remote Jellyfin server:

#### Using the Sync Script (Recommended)

**Always run with `--scan`.** It syncs *and* triggers the Jellyfin library scan so new music shows up immediately instead of waiting for the scheduled scan. Drop `--scan` only if you have a specific reason not to refresh the server.

```bash
# Standard sync — do this every time (dry-run first to preview)
scripts/sync_to_jellyfin.py --dry-run
scripts/sync_to_jellyfin.py --scan

# Sync classical/ directory instead
scripts/sync_to_jellyfin.py --classical --scan

# Sync with delete enabled (removes files on remote that don't exist locally)
scripts/sync_to_jellyfin.py --delete --scan

# Override environment variables via command line
scripts/sync_to_jellyfin.py --host myserver --path /mnt/music
```

**Required environment variables:**
- `JELLYFIN_HOST` - Remote Jellyfin server hostname (can override with `--host`)
- `JELLYFIN_FILES_PATH` - Remote base path for music files (can override with `--path`)
- `JELLYFIN_API_KEY` - API key for `--scan` (Dashboard → API Keys). Only needed when using `--scan`; if unset, `--scan` is skipped with a warning.
- `JELLYFIN_API_URL` - *(optional)* Jellyfin web API base URL for `--scan`. Defaults to `http://$JELLYFIN_HOST:8096` (the HTTP API port, separate from the SSH host used for rsync).

The script automatically:
1. Syncs the entire `curated/` or `classical/` directory to the remote server
2. Fixes permissions so Jellyfin can read the files
3. Sets ownership to `jellyfin:jellyfin`
4. Sets proper read/write/execute permissions
5. *(with `--scan`)* Triggers a Jellyfin library scan via the API so new music appears without waiting for the scheduled scan

The remote rsync runs under `sudo` (`--rsync-path="sudo rsync"`) so it can update files already owned by `jellyfin`; this relies on passwordless `sudo` on the Jellyfin host.

#### Manual Sync (Alternative)

If you need to sync specific artists/albums manually:

```bash
# Step 1: Sync files to remote server
rsync -avz --progress ~/Music/curated/Artist\ Name/ remote-host:/path/to/music/Artist\ Name/

# Step 2: SSH to remote server and fix permissions for Jellyfin
ssh remote-host
sudo chown -R jellyfin:jellyfin "/path/to/music/Artist Name"
sudo chmod -R a+r "/path/to/music/Artist Name"
sudo chmod a+x "/path/to/music/Artist Name"
sudo chmod a+x "/path/to/music/Artist Name"/*
```

**Important**: Files synced to the remote server need proper permissions for Jellyfin to read them. The `jellyfin` user must be able to read all files and execute (access) all directories. The `chown` and `chmod` commands above ensure this.

## Helper Scripts

All scripts live in `scripts/`. The Python ones declare their dependencies inline
and have a `uv` shebang, so run them directly as `scripts/<name>.py` (see [Running the scripts](#running-the-scripts)).
The shell scripts run directly. The CD-rip helpers (for the optical-drive machine)
live in `rip/`.

### Splitting and Processing
- `split_flac.py` — Split a FLAC+CUE into individual tracks

### Organization
- `reorganize_to_artist_folders.py` — Convert flat `Artist - Album` folders to `Artist/Album/`
- `check_album_names.sh` — Verify folder names match `ARTIST`/`ALBUM` tags

### Tagging
- `check_tags.py` — Report `ALBUMARTIST` tags that don't match the artist folder name
- `fix_album_artist.py` — Set `ALBUMARTIST` to the artist folder name
- `check_multi_artist.py` — Find multi-artist tracks not using the `; ` delimiter
- `add_disc_numbers.py` — Set `DISCNUMBER` from `Disc N/` subfolders

### Artwork
- `fetch_album_art.py` — Download album covers from Cover Art Archive / TheAudioDB
- `fetch_artist_image.py` — Download artist images from TheAudioDB
- `review_covers.sh` — Page through album covers with `imv`
- `review_artist_images.sh` — Page through artist images with `imv`
- `check_missing_images.py` — Report albums/artists missing artwork

### Lyrics
- `fetch_lyrics.py` — Download synced `.lrc` lyrics from LRCLIB (see [Getting Lyrics](#getting-lyrics))

### Metadata Sidecars
- `fetch_nfo.py` — Write `artist.nfo`/`album.nfo` (bio, review, rating, mood) from TheAudioDB (see [Getting NFO Metadata](#getting-nfo-metadata))

### Quality Checks
- `get_runtime.py` — Calculate total runtime of audio files in a directory
- `verify_rips.py` — Verify rips against AccurateRip/CTDB (see [Verifying Rips](#verifying-rips-accuraterip))
- `ctdb_repair.py` — Check whether CTDB can repair a `DIFFERS` album, and with `--apply` do it end to end (see [docs/ctdb-repair.md](docs/ctdb-repair.md))
- `ctdb-cli.sh` — Wrapper around `ctdb-cli`, for repairing a `DIFFERS` rip from CTDB parity. Needs a one-time source build (see [docs/ctdb-repair.md](docs/ctdb-repair.md))
- `curate_whipper_rip.py` — Automate the whole post-whipper curation flow: pull, rename, tag, verify, and sync (see [Accurate ripping with whipper](#accurate-ripping-with-whipper))

### Syncing
- `sync_to_jellyfin.py` — Sync the library to a Jellyfin server with automatic permission fixing

### CD Ripping (`rip/`)
- `rip-cd.sh` — whipper wrapper for AccurateRip-verified rips (uses `$RIP_OFFSET`); also runs the skipped-track rescue
- `rescue-skipped.py` — recover tracks whipper skipped (best-effort `cd-paranoia`), flag them, and point you where to listen; run automatically by `rip-cd.sh`

## Quick Reference: Processing a New Album

Given a folder to clean up, follow these steps:

```bash
# 1. If you have a FLAC+CUE file, split it first
#    Create and run a splitting script based on the template above

# 2. Create proper directory structure
mkdir -p ~/Music/curated/"Artist Name"/"Album Title"
mv tracks/*.flac ~/Music/curated/"Artist Name"/"Album Title"/

# 3. Tag all files
cd ~/Music/curated/"Artist Name"/"Album Title"
# Use metaflac to apply tags (see Tagging Files section)

# 4. Fetch artwork
scripts/fetch_album_art.py --directory ~/Music/curated/"Artist Name"
scripts/fetch_artist_image.py --artist-dir ~/Music/curated/"Artist Name"

# 5. Manually add artist image if needed
cp ~/Downloads/artist-image.jpg ~/Music/curated/"Artist Name"/folder.jpg

# 6. Fetch synced lyrics (.lrc sidecars)
scripts/fetch_lyrics.py ~/Music/curated/"Artist Name"/"Album Title"

# 6a. Fetch NFO metadata (artist.nfo / album.nfo)
scripts/fetch_nfo.py ~/Music/curated/"Artist Name"

# 7. Verify everything
ls -R ~/Music/curated/"Artist Name"
metaflac --list "01 - Track Name.flac" | grep comment

# 8. Sync to Jellyfin server (always use --scan to refresh the library)
scripts/sync_to_jellyfin.py --scan
```

## Troubleshooting

### Missing MusicBrainz IDs

If artwork scripts can't find albums/artists, you may need to manually look up IDs on https://musicbrainz.org and add them to tags:

```bash
metaflac --set-tag=musicbrainz_artistid="MBID-HERE" *.flac
```

### File Permission Issues

Ensure files are readable and directories are accessible:

```bash
chmod 644 *.flac *.jpg
chmod 755 .
```
