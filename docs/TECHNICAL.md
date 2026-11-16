# Technical notes

What was found out while building this tool. Unless noted otherwise, everything was verified on macOS 27 with Luminar Neo 1.28.1 (the catalog schema was re-checked with 1.29.0), digiKam 9.1.0 and exiftool 13.58. None of it is official documentation.

## Luminar Neo catalog

**Location.** The active catalog is a SQLite file, e.g. `~/Pictures/<name>/<name>.luminarneo`. Its path is stored in the preference `MPDAMCurrentCatalogContentsFilePathKey` of `~/Library/Preferences/com.skylum.luminarneo.plist`. The journal mode is `delete` (not WAL). `PRAGMA user_version` is `155` for Luminar Neo 1.28 and `156` for 1.29.

**Version 1.29** changed no table or index definition: only `user_version`, and with it `settings.db_version`, went from 155 to 156. Flags, ratings and the path tables kept their meaning. The tool keeps a list of the schema versions it has been checked against and refuses any other; after a Luminar update, compare `sqlite_master` with the previous version before adding the new number.

**Flags and ratings** live in `image_user_attributes`, one row per image (`_out_id_int_64` → `images._id_int_64`):

| Column | Meaning |
|---|---|
| `marker` | `0` = none, `1` = Favorite, `2` = Rejected |
| `rating` | `0`–`5` |
| `edit_date_int_64` | Unix time of the **last edit**, not of the last flag or rating change. Setting a flag or a rating does not touch it. |
| `virtual_copy_bool`, `trash_bool` | virtual copies and trashed images |

The mapping of `marker` was confirmed by cross-checking against digiKam, where the same images carried Accepted/Rejected pick labels, and by setting flags in Luminar and watching the column change.

**Luminar keeps no timestamp for flag or rating changes.** A “newest wins” rule is therefore impossible from the catalog alone; this tool uses a three-way merge with its own state file instead.

**Paths.** An image's full path is assembled from three tables:

```
volumes.info_wide_ch   JSON, key "kMountPointSerializationKey"   e.g. "/" or "/Volumes/Disk"
  + paths.path_wide_ch  (via paths_images: _key = path id, _val = image id)   relative to the mount point
  + images.path_wide_ch file name
```

- `paths.path_wide_ch` may or may not start with `/`; strip it before joining.
- **Paths are partly stored in NFD** (decomposed Unicode); in one catalog, about 8% of them. File systems and digiKam use NFC, so normalize before comparing.
- `reffolders` → `paths` lists the folders added to the catalog.

**Moving a folder leaves orphans.** When the source folder of the catalog is moved to another disk, Luminar can create new rows for the new location and keep the old rows. The old rows stay on the old volume and are no longer under any `reffolders` entry. Image counts then look inflated: one catalog showed ~98 000 images for ~66 000 files. The old rows may even hold ratings the new rows lack. `--audit` reports these entries; the sync only uses entries under a referenced folder.

**Other observations:**
- There are no triggers on the relevant tables.
- The cloud sync columns (`cloud_id_wide_ch`, `sync_status`, `updated_at_int_64`) were unused in the tested catalog.
- `cache_db.db` holds previews; `~/Library/Application Support/com.skylum.luminarneo/Entities` holds thumbnails and presets. Neither holds flags or ratings.

**Writing the catalog.** Updating `marker` and `rating` while Luminar is **closed** works: Luminar shows the new values at its next start (verified with 1.28; 1.29 uses identical tables). This was tried on a copy of the catalog opened via *File → Open Catalog* before touching the real one. While Luminar is running it keeps the catalog in memory and writes it continuously, so external changes are unsafe. Luminar also keeps its own `.lumBackup` copies in the catalog's `Backups` folder.

## digiKam

**Settings** live in `~/Library/Preferences/digikamrc` (an INI file):

| Section / key | Meaning |
|---|---|
| `[Metadata Settings] Metadata Writing Mode` | `1` = write to XMP sidecar only |
| `[Metadata Settings] Use XMP Sidecar For Reading` | read sidecars |
| `[Metadata Settings] Use Compatible File Name` | `false`: `image.jpg.xmp` (default); `true`: `image.xmp` |
| `[Database Settings] Database Name`, `Database Type` | folder of `digikam4.db` (SQLite, WAL mode) |
| `[DMetadata Settings][write…Namespaces]` | which XMP fields digiKam writes for each kind of data |

**Fields digiKam writes** (default namespaces):

| Data | XMP fields |
|---|---|
| pick label | `digiKam:PickLabel` (0 none, 1 rejected, 2 pending, 3 accepted), `xmpDM:pick` (1 / −1), `xmpDM:good` (`False` for rejected) |
| rating | `xmp:Rating`, `acdsee:rating`, `MicrosoftPhoto:Rating` (as a percentage: 0, 1, 25, 50, 75, 99) |
| color label | `digiKam:ColorLabel`, `xmp:Label`, `photoshop:Urgency` |
| tags | `digiKam:TagsList` (`A/B`), `MicrosoftPhoto:LastKeywordXMP`, `lr:hierarchicalSubject` (`A|B`), `mediapro:CatalogSets`, `acdsee:categories`, `dc:subject` (leaf names) |

- Setting the pick label to *None* removes the pick fields altogether.
- *Accepted* is written as `PickLabel=3` and `xmpDM:pick=1`, without `xmpDM:good`.

**Reading behavior – the important pitfalls:**

1. **A sidecar replaces the embedded XMP.** Once `image.ext.xmp` exists, digiKam ignores the XMP embedded in the image; EXIF and IPTC are still read from the image. A new sidecar must therefore start from the image's embedded XMP, or keywords and labels stored there vanish from digiKam's view.
2. **Tags are read from `digiKam:TagsList` first.** If it is not empty, the other tag fields (`lr:hierarchicalSubject`, `dc:subject`, …) are ignored. Whoever adds a tag to `TagsList` must write the **complete** tag list, not just the new tag.
3. **A re-read removes database-only tags.** *Reread Metadata From Files* deletes tags from digiKam's database that are not in the files.
4. **Changed sidecars are not picked up automatically.** Neither the startup scan nor “Rescan File If Modified” noticed sidecars changed by another program. Use *Album → Reread Metadata From Files*.
5. **Editing before a re-read overwrites the sidecar.** When you change any metadata of an image in digiKam, it writes all fields from its database. If the album was not re-read after an external change, that change is silently reverted.
6. **A re-read reads the image files.** For online-only files in a cloud folder this downloads them.

**Database (`digikam4.db`)**, the parts used here:

| Table | Use |
|---|---|
| `AlbumRoots.specificPath` + `Albums.relativePath` (`/` for the root) + `Images.name` | the image path |
| `ImageInformation.rating` | `-1` = no rating |
| `Tags` (`id`, `pid`, `name`) + `ImageTags` | tags, including the internal tags under `_Digikam_Internal_Tags_`: `Pick Label None/Rejected/Pending/Accepted` and `Color Label None/Red/…/White` |
| `ImageComments` | captions (type 1), headlines (2), titles (3) |
| `ImagePositions` | GPS |
| `ImageTagProperties` (`tagRegion`) | face regions |

Open the database read-only through the SQLite backup API. With `mode=ro`, a WAL database without its `-shm` file cannot be opened; `immutable=1` works when nobody is writing.

## exiftool

- **Tag names:** `XMP-digiKam:PickLabel`, `XMP-digiKam:ColorLabel`, `XMP-digiKam:TagsList`, `XMP-xmpDM:Pick`, `XMP-xmpDM:Good`, `XMP-acdsee:Rating`, `XMP-microsoft:RatingPercent` (= `MicrosoftPhoto:Rating`), `XMP-microsoft:LastKeywordXMP`, `XMP-lr:HierarchicalSubject`.
- **Creating a sidecar that keeps the embedded XMP:** `exiftool -tagsfromfile IMAGE -xmp:all -TAG=VALUE … -o IMAGE.ext.xmp IMAGE`. Assignments override the copied values.
- **Lists:** `-TAG=a -TAG=b` replaces the whole list; `-TAG-=a -TAG+=a` adds `a` without duplicating it.
- **MicrosoftPhoto URI:** digiKam (Exiv2) writes `xmlns:MicrosoftPhoto="http://ns.microsoft.com/photo/1.0/"`. exiftool rewrites it without the trailing slash and prints a minor warning. Exiv2 0.28 reads both.
- **Field-by-field copying is not verbatim.** `-tagsfromfile` copies only tags exiftool knows, and normalizes some values (e.g. `true` → `True`). To carry an XMP block over unchanged, copy it at the XML level.
- **Argument files:** with `-@ ARGFILE` every line is one argument, which is handy for many files and non-ASCII paths. A value cannot contain a newline; use `-TAG<=FILE` for multi-line values.

## macOS cloud folders (File Provider)

- **Online-only files:** they have the `SF_DATALESS` flag (`0x40000000` in `st_flags`), and `st_blocks == 0` while `st_size > 0`. Opening one downloads it in full, even if only the header is read.
- **Removing the local copy** (the Finder's “Remove Download”) can be done from code with `FileManager.default.evictUbiquitousItem(at:)`. It also works for third-party File Provider domains such as Synology Drive.
- **Upload state of a file:** `fileproviderctl evaluate PATH` shows it (`isUploaded`, `isDownloaded`, …). The client app's own log may not show these uploads.
- **Access:** a terminal or app needs explicit permission for `~/Library/CloudStorage`. Ad-hoc signed apps may be asked again after every rebuild.

## Design of the three-way sync

For every image and field the tool stores `[Luminar value, sidecar value, digiKam database value]` as seen at the last `--apply`. On the next run:

| Situation | Decision |
|---|---|
| only the Luminar value differs from the stored one | Luminar changed → write the sidecar |
| only the sidecar value differs | digiKam changed → write back to Luminar (with `--to-luminar`) |
| both differ | conflict |
| the sidecar is back at the stored digiKam database value, the database value has not changed, and the sidecar originally received something else | digiKam rewrote the sidecar from a stale database (pitfall 5) → conflict, never written back |

The image file is only opened when there is neither a sidecar nor digiKam data, to avoid downloads from cloud folders.
