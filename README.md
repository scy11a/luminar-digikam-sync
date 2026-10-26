# luminar-digikam-sync

Two-way sync of **flags** (Favorite / Rejected) and **star ratings** between **Luminar Neo** and **digiKam** on macOS.

Luminar Neo is a good editor, but it has no real asset management and no way to export flags or ratings as XMP. digiKam is a full DAM that reads and writes XMP sidecar files. This tool bridges the two:

- **Luminar → digiKam:** writes the XMP sidecars next to your images, exactly the way digiKam itself writes them. After a re-read, digiKam shows what you did in Luminar.
- **digiKam → Luminar** (optional): writes values you changed in digiKam back into the Luminar catalog, while Luminar is closed.

| Luminar Neo | digiKam |
|---|---|
| Favorite | Pick Label: Accepted |
| Rejected | Pick Label: Rejected |
| 0–5 stars | 0–5 stars |

There is a command-line tool (`luminar_sync.py`, Python standard library only) and an optional native macOS app (SwiftUI) on top of it.

> **Unofficial.** The Luminar catalog format is not documented, and writing to it is not supported by Skylum. The tool refuses to run against an untested catalog version, keeps backups and can undo every write. Still, keep your own backups and use it at your own risk.

## Tested with

| | Version |
|---|---|
| macOS | 27 |
| Luminar Neo | 1.28.1 (catalog schema `user_version` 155) |
| digiKam | 9.1.0 |
| exiftool | 13.58 (bundled with digiKam) |
| Python | 3.9 (the system `/usr/bin/python3`) and 3.12 |

Other versions may work, but a different Luminar catalog schema is rejected until the tool has been checked against it (see `SUPPORTED_SCHEMA` in `luminar_sync.py`).

## Requirements

- macOS with Luminar Neo and digiKam.
- Python 3.9 or newer. The system Python is enough; no packages are needed.
- exiftool. The copy bundled with digiKam is used automatically.
- For the app: the Xcode command-line tools.

The tool assumes the usual digiKam setup: metadata is written to XMP sidecars, and sidecars are read. Both settings are under *Settings → Configure digiKam → Metadata*. The tool reads the sidecar naming (`image.jpg.xmp`, or `image.xmp` with “compatible file names”) and the database location from digiKam's own settings file.

The terminal (or the app) needs file access to your photo folders. For folders under `~/Library/CloudStorage` grant it under *System Settings → Privacy & Security → Files and Folders* (or *Full Disk Access*).

## Usage

```sh
# dry run: shows what would change, writes nothing
python3 luminar_sync.py "/path/to/Photos/2026/2026-08"

# several folders or single images; -r walks subfolders
python3 luminar_sync.py -r "/path/to/Photos/2026"
python3 luminar_sync.py "/path/to/IMG_0001.RAF" "/path/to/IMG_0002.RAF"

# write the sidecars (quit digiKam first)
python3 luminar_sync.py --apply "/path/to/Photos/2026/2026-08"
```

Then, in digiKam, select the album and run **Album → Reread Metadata From Files**. This step is required: digiKam does not notice changed sidecars on its own, not even at startup.

Writing changes made in digiKam back to Luminar (quit Luminar first; digiKam may keep running):

```sh
python3 luminar_sync.py --apply --to-luminar "/path/to/Photos/2026/2026-08"
```

Luminar may stay open while you only read: the catalog is read from a consistent snapshot.

### Options

| Option | Meaning |
|---|---|
| `-r` | walk folders recursively |
| `--apply` | really write; without it everything is a dry run |
| `--to-luminar` | also write values changed in digiKam back to the Luminar catalog (Luminar must be closed) |
| `--prefer luminar` | Luminar's value always wins, removals included (default: `auto`) |
| `--prefer digikam` | digiKam's value always goes to Luminar, removals included; implies `--to-luminar` |
| `-v` | detailed list, including the digiKam values that are kept |
| `--overview` | per-folder totals: differences and images waiting for a re-read |
| `--check-digikam` | what a digiKam re-read would overwrite or lose (read-only) |
| `--include-cloud` | let `--check-digikam` also read cloud-only images (downloads them) |
| `--audit` | catalog report: orphaned, missing and duplicate entries |
| `--undo FOLDER` | undo an earlier `--apply` from its backup folder |
| `--files-from FILE` | more paths from a file, one per line |
| `--json` | machine-readable output (used by the app) |
| `--catalog`, `--exiftool`, `--state` | override the default locations |

## Who wins in a conflict?

Luminar does not record when a flag or rating was changed. The tool therefore remembers what it saw on both sides at the last `--apply` (in `~/Library/Application Support/LuminarSync/state.json`) and decides per field:

| Situation | Default (`auto`) |
|---|---|
| both values are equal | nothing to do |
| changed only in Luminar | **write** the sidecar (removals included) |
| changed only in digiKam | **→ Luminar**: written back with `--to-luminar`, otherwise kept |
| the sidecar went back to its pre-sync value while digiKam's database did not change | **conflict**: digiKam probably overwrote it before the album was re-read, so it is not written back |
| changed on both sides | **conflict**: reported, nothing written |
| no history yet, digiKam empty | **write** (filling a gap) |
| no history yet, Luminar empty | keep digiKam's value (e.g. ratings imported from Lightroom) |
| no history yet, both set and different | **conflict** |

Conflicts are resolved with `--prefer luminar` / `--prefer digikam`, or by making the two values equal in either program.

> **Re-read before you edit.** After the tool writes sidecars, re-read the album in digiKam before editing those images there. Otherwise digiKam rewrites the sidecars from its stale database. The tool detects this and reports a conflict instead of writing the old value back to Luminar, but it saves you a round trip.

## Safety

- Dry run by default; a dry run writes nothing, not even the state file.
- Before every `--apply` the affected sidecars are copied to `~/Library/Application Support/LuminarSync/backups/<timestamp>/`, and the Luminar catalog too when it is written. `--undo` restores them.
- Every write is read back and verified.
- Sidecars are not written while digiKam is running (digiKam could overwrite them from its database).
- New sidecars include the image's own embedded XMP. Once a sidecar exists, digiKam ignores the embedded XMP, and keywords stored there would otherwise disappear from view.
- **Writing the Luminar catalog:**
  - only while Luminar is not running;
  - the whole catalog is backed up first;
  - one transaction, followed by `PRAGMA quick_check`;
  - aborts if any value changed in Luminar since it was read;
  - only the flag and rating columns are touched.
  - Undo restores image by image, and skips images changed in Luminar since.
- An unknown catalog schema version stops the tool.

## The macOS app

A native SwiftUI front-end that runs the same command-line tool in the background. It has no sync logic of its own, so backups, undo and verification work exactly the same.

```sh
gui/build_app.sh        # result: gui/build/LuminarSync.app
```

- **Left:** the Luminar folder tree. The orange number counts differences between Luminar and digiKam; the blue ↻ number counts images already in sync that digiKam has not re-read yet. This view is a quick estimate from the two databases.
- **Right:** the images of the selected folder, Luminar and digiKam side by side, with the suggested action. This view reads the sidecars.
- **Buttons:**
  - Apply suggested;
  - Selected: Luminar → digiKam;
  - Selected: digiKam → Luminar;
  - digiKam check;
  - Undo.
- **Status bar:** shows whether Luminar and digiKam are running; writes that would conflict with a running app are disabled.

The app uses the system `/usr/bin/python3`. It is signed ad hoc, so macOS asks for folder access on first use, and may ask again after a rebuild.

## Cloud folders

Photo libraries in cloud folders (Synology Drive, iCloud Drive and other File Provider based clients) often keep files online-only. Reading such a file, which includes digiKam's re-read, downloads it. The tool avoids this wherever it can:

- it only reads images when there is no sidecar and no digiKam data;
- `--check-digikam` skips cloud-only images unless `--include-cloud` is given.

## Tests

The tests run in Docker, nothing needs to be installed on the Mac:

```sh
docker build --build-arg PY=3.9 -t luminar-digikam-sync-test -f docker/Dockerfile.test docker/
docker run --rm -v "$PWD":/w luminar-digikam-sync-test
```

They use a synthetic Luminar catalog with the real schema, a minimal digiKam database and real sidecars written by exiftool.

## Technical notes

[docs/TECHNICAL.md](docs/TECHNICAL.md) collects what was found out about the Luminar catalog, digiKam's sidecar and re-read behavior, and the pitfalls on the way. It may be useful even if you do not use this tool.

## License

MIT, see [LICENSE](LICENSE).
