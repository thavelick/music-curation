# Repairing a rip from CTDB parity

When [`verify_rips.py`](../README.md#verifying-rips-accuraterip) reports `DIFFERS` and the damage is **genuinely audible**, CTDB can often *reconstruct* the bad samples. CTDB entries carry Reed-Solomon parity, so a handful of damaged samples can be corrected from other people's rips of the same pressing. The original disc is not required.

Read [What to actually worry about](../README.md#what-to-actually-worry-about) first. Most `DIFFERS` results are sub-millisecond and inaudible — **listen before you repair.** This page is for the rare case where you've listened, it's bad, and re-ripping isn't an option.

[`scripts/ctdb_repair.py`](../scripts/ctdb_repair.py) drives the whole thing — see [Usage](#usage). The rest of this page is what it does and why, which is worth reading when it reports something odd, or if you're doing it by hand.

**CTDB only.** AccurateRip stores no parity, so a disc reported `disk not present in database` on the AccurateRip line can still be repairable via CTDB — the two databases are independent.

The one-time setup is heavier than anything else in this repo: it needs the .NET 10 SDK and a source build.

## Tooling

The repair itself runs through [`scripts/ctdb-cli.sh`](../scripts/ctdb-cli.sh), a thin wrapper. But the tool it wraps is **not packaged and not in this repo** — you have to build it from source first, or the wrapper will tell you it's missing.

[`ctdb-cli`](https://github.com/Masterisk-F/ctdb-cli) is a CLI over the same cuetools.net libraries CUETools uses. Its README says Linux, but it targets .NET 10 and builds and runs fine on macOS. Nothing already in `$MUSIC_DIR/tools/` helps: `CUETools.ARCUE.exe` only verifies, and `CUETools.exe` is a WinForms GUI that won't start under mono on macOS.

### Building it

```bash
brew install dotnet                                  # .NET 10 SDK (or your distro's package)
git clone https://github.com/Masterisk-F/ctdb-cli.git ~/src/ctdb-cli
cd ~/src/ctdb-cli
./setup.sh                                           # clones cuetools.net v2.2.6 + applies patches
dotnet publish CTDB.CLI/CTDB.CLI.csproj -c Release -o publish/dependent
```

`~/src/ctdb-cli` is where the wrapper looks by default; clone elsewhere and you'll need to set `CTDB_CLI_DLL`. `setup.sh` clones cuetools.net (pinned to v2.2.6, the same version `verify_rips.py` uses) plus its submodules and applies four patches, so it needs network access and is not a quick rebuild.

Confirm it worked:

```bash
scripts/ctdb-cli.sh          # prints usage
```

Two traps worth knowing, both already handled by the wrapper:

- **Don't run `make install` on macOS.** The upstream README says `./configure && make && sudo make install`, but the Makefile's install target uses GNU `realpath --relative-to`, which BSD realpath rejects.
- **Don't run the published native binary directly.** `publish/dependent/ctdb-cli` only searches the default .NET install locations, so against a Homebrew dotnet it fails with `.NET location: Not found`. The wrapper runs the DLL through `dotnet` instead, which works wherever the runtime lives.

Overrides: `CTDB_CLI_DLL` (path to `ctdb-cli.dll`), `DOTNET_BIN` (path to `dotnet`).

## Usage

Check first — this writes nothing:

```bash
scripts/ctdb_repair.py ~/Music/curated/"Nine Inch Nails"/"Pretty Hate Machine"
```
```
Pretty Hate Machine  (10 tracks)
  best entry: confidence 1872, crc32 cbdb0832, hasparity=True
  TOC shift: 32 sectors (18816 samples)
  tracks: 6 matched, 4 unmatched
    track 01: local 53297e71 != remote 313c1ad8
    ...
  correctable errors: 3718
  CanRecover: True

  repair with:
    scripts/ctdb_repair.py --apply "/Users/tristan/Music/curated/Nine Inch Nails/Pretty Hate Machine"
```

`CanRecover: True` means CTDB has enough parity to reconstruct the damage. Then:

```bash
scripts/ctdb_repair.py --apply ALBUM
```

`--apply` repairs the disc, cuts the damaged tracks back out, splices them into a **copy**, and verifies that copy against CTDB. Only if the copy comes back clean does it back up the originals, carry the tags across, install, and recompute ReplayGain across the album. If the copy doesn't verify, it refuses to install and nothing is touched.

Originals go to `$MUSIC_DIR/.ctdb-backups/<artist>/<album>/` — outside `curated/`, so a library sync won't pick them up. Override with `--backup DIR`, and keep the intermediates (including the repaired WAV) with `--work DIR`.

Repair is **whole-disc**: an album with four damaged tracks is one pass, not four.

Afterwards, confirm through the normal path:

```bash
scripts/verify_rips.py ~/Music/curated/"Nine Inch Nails"/"Pretty Hate Machine"
```

## Building the cue

> Everything from here down is what `ctdb_repair.py` does internally, and how to do it by hand if you need to. Skip it unless the tool reports something odd.

Everything below works off a cue sheet describing the disc. CTDB identifies a disc by its **TOC**, which is reconstructed from the cue — so the cue is what has to be right (see the next section). Work on a copy, not your library: the album directory should stay clean.

For an album of one-FLAC-per-track, the cue is a `FILE`/`TRACK`/`INDEX` triplet per track, in track order:

```
PERFORMER "Pet Shop Boys"
TITLE "Nightlife"
FILE "01 For Your Own Good.flac" WAVE
  TRACK 01 AUDIO
    INDEX 01 00:00:00
FILE "02 Closer to Heaven.flac" WAVE
  TRACK 02 AUDIO
    INDEX 01 00:00:00
```

`verify_rips.py` builds exactly this shape internally (see `write_cue()`), so it's a reasonable starting point — but note it's the *naive* form that assumes track 1 starts at sector 0, which is the trap described next.

## The gotcha: the cue's TOC must match the disc exactly

This is the part `ctdb_repair.py` exists to handle, and the part that will waste your afternoon if you do it by hand.

If the cue's TOC doesn't match, `verify` reports `disk not present in database` — even when the disc is right there with hundreds of submissions. It fails **silently and misleadingly**, so suspect this first.

The trap is that not every disc starts track 1 at sector 0, and a naive one-file-per-track cue assumes it does. Pet Shop Boys' *Nightlife* starts at 32 — whipper's log says `Start sector: 32`, and MusicBrainz carries both a 150- and a 182-offset disc ID for it.

To find the shift, run:

```bash
scripts/ctdb-cli.sh lookup album.cue
```

It queries with `fuzzy=1`, so it finds the disc regardless of the mismatch, and prints both the `toc=` it **sent** and the `toc=` on each returned **entry**:

```
Fetching XML from: http://db.cuetools.net/lookup2.php?...&toc=0:23513:42005:...:234585
 <entry confidence="250" crc32="63a5853b" hasparity="..." toc="32:23545:42037:...:234617" ... />
```

Here every value differs by a constant 32, so N = 32. Fix it in two parts.

First, prepend `N × 588` samples of silence to your **copy** of track 1 (a sector is 588 stereo samples), so its real audio starts at sector N:

```bash
# N=32 -> 32 * 588 = 18816
ffmpeg -i "01 Track.flac" -af "adelay=18816S:all=1" \
  -c:a flac -sample_fmt s16 "01 Track (shifted).flac"
```

Then point the cue at the shifted file and declare those N frames as the pregap:

```
FILE "01 Track (shifted).flac" WAVE
  TRACK 01 AUDIO
    INDEX 00 00:00:00
    INDEX 01 00:00:32     # N = 32
```

Use `INDEX 00`/`INDEX 01`, **not** `PREGAP`. `PREGAP` corrects the TOC but leaves the audio at sector 0, which misaligns every track and mismatches all of them.

Sanity check before going further: total samples across all files should equal the entry's leadout (its last `toc=` value) × 588. If `verify` warns `Audio files combined shorter than TOC. Padding N samples`, the shift is wrong.

`hasparity` on an entry is what makes repair possible — an entry without it can be matched but not repaired.

## Workflow

```bash
scripts/ctdb-cli.sh verify album.cue                 # Conf, CRC, CanRecover, Correctable Errors
scripts/ctdb-cli.sh repair album.cue --target <crc>  # -> album_repaired.wav (the WHOLE disc)
```

Before repairing, `verify` must report `CanRecover: True` **and** every undamaged track must show `[Matched]`:

```
Track 04: Local=886d449a Remote=886d449a [Matched]
Track 05: Local=db6cece3 Remote=36194aaa [Unmatched]
...
Repair Info:
  Correctable Errors: 31
```

If tracks you know are fine show `[Unmatched]`, the alignment is still wrong — fix that before repairing. `--target` selects the entry by CRC; omit it to use the highest-confidence entry.

## Splicing the tracks back

Repair emits a single whole-disc WAV covering every track, damaged or not, so extract the repaired tracks yourself. The entry's `toc=` gives each track's sector range, and a sector is 588 stereo samples — so track *i* spans samples `toc[i-1] * 588` to `toc[i] * 588`.

**Splice into a copy, not your library.** Symlink the undamaged tracks, write the repaired ones alongside, and verify that directory before anything is overwritten:

```bash
# track 5 spans sectors 82375..96725 -> samples 48436500..56874300
ffmpeg -i album_repaired.wav -af "atrim=start_sample=48436500:end_sample=56874300" \
  -c:a flac -compression_level 8 -sample_fmt s16 "copy/05 Track.flac"

scripts/ctdb_repair.py copy/      # must report: rip already matches CTDB
```

That check is the whole safety net, and it's worth more than it looks: it re-derives the TOC from scratch and compares every track against CTDB, so an off-by-one in the sector math shows up as unmatched tracks instead of silently corrupting a track that was only slightly damaged. Don't install until the copy comes back clean.

Only then carry the tags over, minus ReplayGain (which must be recomputed), and install:

```bash
metaflac --export-tags-to=- "$ORIGINAL" | grep -v "^REPLAYGAIN_" > tags.txt
metaflac --remove-all-tags --import-tags-from=tags.txt "copy/05 Track.flac"
cp "copy/05 Track.flac" "$ORIGINAL"          # back the original up first
metaflac --add-replay-gain *.flac            # album gain spans all tracks
scripts/verify_rips.py ~/Music/curated/"Artist"/"Album"
```

### Don't verify the splice by comparing CRC32 to `Remote=`

It's tempting to check your extracted track's CRC32 against the `Remote=` value `verify` printed. **That only works when the entry says `Offset: 0`.**

```
Conf: 1872, CRC: cbdb0832, Offset: -6
  Track 02: Local=4d362a45 Remote=4d362a45 [Matched]
```

Here CTDB matched the disc at a **-6 sample read offset**, so the `Local=`/`Remote=` CRCs are computed over offset-shifted audio and will never equal a plain CRC32 of the file — track 02 is `[Matched]`, yet its file's CRC32 is `81b28a81`, nothing like `4d362a45`. Comparing them makes a perfectly good repair look broken. Verify the spliced copy with `ctdb_repair.py` instead; it handles the offset.

## Worked examples

**Pet Shop Boys / *Nightlife*** (US, Sire 31086-2), track 5 — `Differs in 31 samples @01:38:46`. Found the hard way, before `ctdb_repair.py` existed:

| Step | Result |
|---|---|
| Naive cue (track 1 @ sector 0) | `disk not present in database` |
| `PREGAP 00:00:32` | TOC right, audio padded — all 12 tracks `[Unmatched]` |
| Prepend 18816 samples + `INDEX 00`/`01` | 11/12 `[Matched]`, `CanRecover: True` |
| `repair --target 63a5853b` | 28 stereo frames changed |
| `verify_rips.py` | `DIFFERS` → `[OK*] CTDB 12/12 conf 255` |

The disc was never in AccurateRip at all; the whole repair ran on CTDB.

**Nine Inch Nails / *Pretty Hate Machine*** — 4 damaged tracks (1, 3, 4, 10), worst 3566 samples (81ms) scattered over four minutes:

| Step | Result |
|---|---|
| `ctdb_repair.py` | shift 32 sectors, 3718 correctable, `CanRecover: True` |
| `repair --target cbdb0832` | one pass fixes all four tracks |
| Splice into a copy, `ctdb_repair.py copy/` | 10/10 matched — safe to install |
| `verify_rips.py` | `DIFFERS` → `[OK*] AccurateRip 10/10, CTDB 10/10 conf 1934` |

3718 is the sum of all four tracks' damage — repair is whole-disc, so one pass covers the album. This entry matched at `Offset: -6`, which is what makes the CRC32-vs-`Remote=` check useless here.

Damage size is a poor guide to repairability: 31 samples and 3718 samples were both `CanRecover: True`. Run `ctdb_repair.py` and let it answer.
