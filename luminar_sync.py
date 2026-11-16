#!/usr/bin/env python3
"""LuminarSync – two-way sync of flags and star ratings between Luminar Neo and digiKam.

Luminar → digiKam: writes the XMP sidecar files next to the images, the way digiKam itself writes them.
digiKam → Luminar (--to-luminar): writes the Luminar catalog, only while Luminar is not running.
By default it only prints what it would change (dry run); use --apply to write.
Uses only the Python standard library and exiftool (the copy bundled with digiKam).
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.request import pathname2url

HOME = Path.home()
LUMINAR_PREFS = HOME / "Library/Preferences/com.skylum.luminarneo.plist"
DIGIKAMRC = HOME / "Library/Preferences/digikamrc"
STATE_FILE = HOME / "Library/Application Support/LuminarSync/state.json"
EXIFTOOL_CANDIDATES = (
    "/Applications/digiKam.org/digikam.app/Contents/bin/exiftool",
    "/Applications/Luminar Neo.app/Contents/Frameworks/SLFoundation.framework/Resources/exiftool",
)
# Luminar catalog schema versions (PRAGMA user_version) the tool has been checked against.
# 1.29 bumped the version without changing any table definition.
SUPPORTED_SCHEMAS = {155: "Luminar Neo 1.28", 156: "Luminar Neo 1.29"}

# digiKam pick label values (Xmp.digiKam.PickLabel)
PICK_NONE, PICK_REJECTED, PICK_PENDING, PICK_ACCEPTED = 0, 1, 2, 3
PICK_NAMES = {PICK_NONE: "–", PICK_REJECTED: "Rejected", PICK_PENDING: "Pending", PICK_ACCEPTED: "Accepted"}
# Luminar image_user_attributes.marker -> digiKam pick label
MARKER_TO_PICK = {0: PICK_NONE, 1: PICK_ACCEPTED, 2: PICK_REJECTED}
# the other way round; Pending has no Luminar counterpart
PICK_TO_MARKER = {PICK_NONE: 0, PICK_REJECTED: 2, PICK_PENDING: 0, PICK_ACCEPTED: 1}
DIGIKAM_PICK_TAGS = {
    "Pick Label None": PICK_NONE,
    "Pick Label Rejected": PICK_REJECTED,
    "Pick Label Pending": PICK_PENDING,
    "Pick Label Accepted": PICK_ACCEPTED,
}
# digiKam writes a percentage into Xmp.MicrosoftPhoto.Rating
RATING_PERCENT = {0: 0, 1: 1, 2: 25, 3: 50, 4: 75, 5: 99}

FIELDS = ("pick", "rating")
FIELD_LABELS = {"pick": "flag", "rating": "rating"}

# decisions: WRITE = write the sidecar (Luminar → digiKam), PULL = write the Luminar catalog (digiKam → Luminar)
NONE, WRITE, PULL, KEEP, CONFLICT = "none", "write", "pull", "keep", "conflict"
ACTION_LABELS = {WRITE: "write", PULL: "→ Luminar", KEEP: "kept", CONFLICT: "CONFLICT"}

XMP_READ_TAGS = (
    "-XMP-digiKam:PickLabel",
    "-XMP-xmpDM:Pick",
    "-XMP-xmpDM:Good",
    "-XMP-xmp:Rating",
    "-XMP-acdsee:Rating",
    "-XMP-microsoft:RatingPercent",
)


class SyncError(Exception):
    pass


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def clamp_rating(value) -> int:
    try:
        return max(0, min(5, round(float(value))))
    except (TypeError, ValueError):
        return 0


def percent_to_stars(value) -> int:
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return 0
    for stars, limit in ((0, 1), (1, 25), (2, 50), (3, 75), (4, 99)):
        if pct < limit:
            return stars
    return 5


def process_running(name: str) -> bool:
    try:
        return subprocess.run(["pgrep", "-x", name], capture_output=True).returncode == 0
    except FileNotFoundError:
        return False


# ---------------------------------------------------------------- settings


def active_catalog() -> Path:
    try:
        with open(LUMINAR_PREFS, "rb") as f:
            path = plistlib.load(f).get("MPDAMCurrentCatalogContentsFilePathKey")
    except (OSError, plistlib.InvalidFileException):
        path = None
    if not path:
        raise SyncError("the active Luminar catalog was not found; pass it with --catalog")
    return Path(path)


def read_digikamrc(path: Path) -> dict[str, dict[str, str]]:
    """Simple INI parsing of digikamrc (configparser chokes on some of its lines)."""
    sections: dict[str, dict[str, str]] = {}
    current = None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return sections
    for line in lines:
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
        elif current is not None and "=" in line:
            key, value = line.split("=", 1)
            current[key] = value
    return sections


def digikam_db_from_rc(rc: dict[str, dict[str, str]]) -> Path | None:
    db = rc.get("Database Settings", {})
    if db.get("Database Type", "QSQLITE") != "QSQLITE" or not db.get("Database Name"):
        return None
    return Path(db["Database Name"]) / "digikam4.db"


def sidecar_path(image: str, compatible: bool) -> str:
    """digiKam: IMAGE.ext.xmp by default, IMAGE.xmp with “compatible file names”."""
    return os.path.splitext(image)[0] + ".xmp" if compatible else image + ".xmp"


def find_exiftool(explicit: str | None) -> str:
    if explicit:
        found = shutil.which(explicit)
        if not found:
            raise SyncError(f"exiftool cannot be run: {explicit}")
        return found
    for candidate in EXIFTOOL_CANDIDATES:
        if os.access(candidate, os.X_OK):
            return candidate
    found = shutil.which("exiftool")
    if not found:
        raise SyncError("exiftool not found; pass it with --exiftool")
    return found


# ---------------------------------------------------------------- databases


def open_snapshot(db: Path, tmpdir: str) -> sqlite3.Connection:
    """A consistent copy of the database, even while Luminar/digiKam is running (SQLite backup API)."""
    if not db.is_file():
        raise SyncError(f"database not found: {db}")
    url = pathname2url(str(db))
    dst = sqlite3.connect(os.path.join(tmpdir, db.name))
    # a WAL-mode database (digiKam) cannot be opened with mode=ro without its -shm file; then nobody is writing it
    for mode in ("mode=ro", "immutable=1"):
        try:
            src = sqlite3.connect(f"file:{url}?{mode}", uri=True)
            try:
                src.backup(dst)
            finally:
                src.close()
            return dst
        except sqlite3.OperationalError:
            continue
    raise SyncError(f"database cannot be read: {db}")


@dataclass
class LuminarEntry:
    path: str  # absolute, NFC
    pick: int
    rating: int
    live: bool  # lies under one of the Luminar folders (referenced folders)
    image_id: int = 0
    marker: int = 0  # the raw Luminar value (checked before writing back, in case it changed meanwhile)


@dataclass
class Catalog:
    entries: list[LuminarEntry]
    roots: list[str]  # absolute paths of the Luminar folders
    path: Path | None = None
    schema: int = 0  # PRAGMA user_version when the catalog was read

    def live_by_path(self) -> dict[str, LuminarEntry]:
        return {e.path: e for e in self.entries if e.live}

    def live_entries(self, path: str) -> list[LuminarEntry]:
        """The same file is occasionally listed twice in the catalog; writing back updates every copy."""
        return [e for e in self.entries if e.live and e.path == path]


def _mount_point(info: str) -> str | None:
    try:
        return json.loads(info)["kMountPointSerializationKey"]
    except (ValueError, KeyError, TypeError):
        return None


def load_luminar(catalog: Path, tmpdir: str) -> Catalog:
    con = open_snapshot(catalog, tmpdir)
    try:
        version = con.execute("PRAGMA user_version").fetchone()[0]
        if version not in SUPPORTED_SCHEMAS:
            supported = ", ".join(f"{v} ({name})" for v, name in sorted(SUPPORTED_SCHEMAS.items()))
            raise SyncError(
                f"unknown Luminar catalog version {version} (supported: {supported}); "
                "Luminar was probably updated, the tool has to be checked against the new version first"
            )
        mounts = {vid: _mount_point(info) for vid, info in con.execute("SELECT _id_int_64, info_wide_ch FROM volumes")}
        root_rows = con.execute(
            "SELECT p.volume_id_int_64, p.path_wide_ch FROM reffolders r "
            "JOIN paths p ON p._id_int_64 = r.id_path_int_64"
        ).fetchall()
        rows = con.execute(
            """
            SELECT p.volume_id_int_64, p.path_wide_ch, i.path_wide_ch, u.marker, u.rating, i._id_int_64
            FROM images i
            JOIN image_user_attributes u ON u._out_id_int_64 = i._id_int_64
            JOIN paths_images pi ON pi._val_id_int_64 = i._id_int_64
            JOIN paths p ON p._id_int_64 = pi._key_id_int_64
            WHERE i.marked_to_delete_bool = 0 AND u.trash_bool = 0 AND u.virtual_copy_bool = 0
            """
        ).fetchall()
    finally:
        con.close()

    roots = {(vol, folder.strip("/")) for vol, folder in root_rows}
    root_paths = sorted(
        {nfc(os.path.join(mounts[vol], folder)) for vol, folder in roots if mounts.get(vol)},
        key=len,
        reverse=True,
    )

    def under_root(vol: int, folder: str) -> bool:
        if not roots:
            return True
        parts = folder.split("/")
        return any((vol, "/".join(parts[:n])) in roots for n in range(len(parts), 0, -1))

    entries = []
    for vol, folder, name, marker, rating, image_id in rows:
        mount = mounts.get(vol)
        if not mount:
            continue
        folder = folder.strip("/")
        entries.append(
            LuminarEntry(
                path=nfc(os.path.join(mount, folder, name)),
                pick=MARKER_TO_PICK.get(marker, PICK_NONE),
                rating=clamp_rating(rating),
                live=under_root(vol, folder),
                image_id=image_id,
                marker=marker,
            )
        )
    return Catalog(entries, root_paths, catalog, version)


def load_digikam(db: Path | None, tmpdir: str) -> dict[str, tuple[int, int]]:
    """What digiKam currently shows: path -> (pick, rating). Empty if there is no database."""
    if db is None or not db.is_file():
        return {}
    con = open_snapshot(db, tmpdir)
    try:
        rows = con.execute(
            """
            SELECT r.specificPath, a.relativePath, i.name, ii.rating, pl.name
            FROM Images i
            JOIN Albums a ON a.id = i.album
            JOIN AlbumRoots r ON r.id = a.albumRoot
            LEFT JOIN ImageInformation ii ON ii.imageid = i.id
            LEFT JOIN (
                SELECT it.imageid, t.name FROM ImageTags it JOIN Tags t ON t.id = it.tagid
                WHERE t.name LIKE 'Pick Label %'
                  AND t.pid = (SELECT id FROM Tags WHERE name = '_Digikam_Internal_Tags_')
            ) pl ON pl.imageid = i.id
            WHERE i.status = 1
            """
        ).fetchall()
    finally:
        con.close()
    result = {}
    for root, rel, name, rating, pick in rows:
        rel = "" if rel == "/" else rel
        result[nfc(root.rstrip("/") + rel + "/" + name)] = (DIGIKAM_PICK_TAGS.get(pick, PICK_NONE), clamp_rating(rating))
    return result


# ---------------------------------------------------------------- exiftool


def run_exiftool(exiftool: str, args: list[str], common: list[str]) -> subprocess.CompletedProcess:
    """Passes the arguments in an argfile (long lists, non-ASCII paths)."""
    with tempfile.NamedTemporaryFile("w", suffix=".args", delete=False, encoding="utf-8") as f:
        f.write("\n".join(args) + "\n")
        argfile = f.name
    try:
        return subprocess.run(
            [exiftool, "-@", argfile, *common], capture_output=True, text=True, encoding="utf-8"
        )
    finally:
        os.unlink(argfile)


def read_metadata(exiftool: str, files: list[str], extra: tuple[str, ...] = ()) -> dict[str, dict]:
    if not files:
        return {}
    proc = run_exiftool(exiftool, files, ["-j", "-n", "-G1", "-q", "-q", "-m", *XMP_READ_TAGS, *extra])
    if not proc.stdout.strip():
        return {}
    return {nfc(rec["SourceFile"]): rec for rec in json.loads(proc.stdout)}


def parse_pick(rec: dict) -> int | None:
    """digiKam's reading order: digiKam:PickLabel, xmpDM:pick, xmpDM:good."""
    value = rec.get("XMP-digiKam:PickLabel")
    if value is not None:
        try:
            pick = int(value)
            return pick if pick in PICK_NAMES else PICK_NONE
        except (TypeError, ValueError):
            pass
    value = rec.get("XMP-xmpDM:Pick")
    if value is not None:
        try:
            return {1: PICK_ACCEPTED, -1: PICK_REJECTED}.get(int(value), PICK_NONE)
        except (TypeError, ValueError):
            pass
    value = rec.get("XMP-xmpDM:Good")
    if value is not None:
        return PICK_ACCEPTED if str(value).lower() in ("true", "1") else PICK_REJECTED
    return None


def parse_rating(rec: dict) -> int | None:
    """digiKam's reading order: xmp:Rating, acdsee:rating, MicrosoftPhoto:Rating, Exif Rating."""
    for key in ("XMP-xmp:Rating", "XMP-acdsee:Rating"):
        if rec.get(key) is not None:
            return clamp_rating(rec[key])
    if rec.get("XMP-microsoft:RatingPercent") is not None:
        return percent_to_stars(rec["XMP-microsoft:RatingPercent"])
    if rec.get("IFD0:Rating") is not None:
        return clamp_rating(rec["IFD0:Rating"])
    return None


def tag_args(pick: int, rating: int) -> list[str]:
    """Exactly the tags digiKam writes itself (see the write*Namespaces entries in digikamrc)."""
    if pick == PICK_ACCEPTED:
        dm = ["-XMP-xmpDM:Pick=1", "-XMP-xmpDM:Good=True"]
    elif pick == PICK_REJECTED:
        dm = ["-XMP-xmpDM:Pick=-1", "-XMP-xmpDM:Good=False"]
    else:
        dm = ["-XMP-xmpDM:Pick=0", "-XMP-xmpDM:Good="]
    return [
        f"-XMP-digiKam:PickLabel={pick}",
        *dm,
        f"-XMP-xmp:Rating={rating}",
        f"-XMP-acdsee:Rating={rating}",
        f"-XMP-microsoft:RatingPercent={RATING_PERCENT[rating]}",
    ]


# ---------------------------------------------------------------- decisions


def decide(
    lum: int, cur: int, base_lum: int | None, base_cur: int | None, prefer: str, stale: bool = False
) -> tuple[str, str]:
    """Three-way decision for one field. lum: Luminar, cur: digiKam, base_*: the values seen at the last --apply.

    A PULL (digiKam → Luminar) is only carried out with --to-luminar; without it the value is left alone.
    stale: the sidecar went back to the pre-sync digiKam value while digiKam's database did not change –
    digiKam probably overwrote it before the album was re-read; such a value is not written back.
    """
    if lum == cur:
        return NONE, "equal"
    if prefer == "luminar":
        return WRITE, "Luminar forced"
    if prefer == "digikam":
        return PULL, "digiKam forced"
    if base_lum is not None and base_cur is not None:
        lum_changed, cur_changed = lum != base_lum, cur != base_cur
        if lum_changed and not cur_changed:
            return WRITE, "changed in Luminar"
        if cur_changed and not lum_changed:
            if stale:
                return CONFLICT, "digiKam may have overwritten it before a re-read – please check"
            return PULL, "changed in digiKam"
        if lum_changed:
            return CONFLICT, "changed on both sides"
        return KEEP, "old difference, neither side changed since"
    if cur == 0:
        return WRITE, "filling a gap"
    if lum == 0:
        return KEEP, "value only in digiKam"
    return CONFLICT, "different values, no history"


@dataclass
class Item:
    image: str
    sidecar: str
    has_sidecar: bool
    lum: dict[str, int]
    cur: dict[str, int]
    decisions: dict[str, tuple[str, str]] = field(default_factory=dict)
    db: dict[str, int | None] = field(default_factory=dict)  # in digiKam's database (None: not in it)

    def actions(self) -> set[str]:
        return {action for action, _ in self.decisions.values()}

    @property
    def needs_write(self) -> bool:
        return WRITE in self.actions()

    @property
    def needs_pull(self) -> bool:
        return PULL in self.actions()

    @property
    def target(self) -> dict[str, int]:
        """What ends up in the sidecar after writing."""
        return {f: self.lum[f] if self.decisions[f][0] == WRITE else self.cur[f] for f in FIELDS}

    @property
    def luminar_target(self) -> dict[str, int]:
        """What Luminar holds after writing back (Pending does not exist in Luminar, it becomes empty)."""
        target = {f: self.cur[f] if self.decisions[f][0] == PULL else self.lum[f] for f in FIELDS}
        target["pick"] = MARKER_TO_PICK[PICK_TO_MARKER.get(target["pick"], 0)]
        return target


@dataclass
class Plan:
    items: list[Item]
    missing: list[str]  # known to Luminar, but the image file is gone
    not_in_catalog: list[str]  # a given file that is not in the Luminar catalog


def select_entries(
    live: dict[str, LuminarEntry], targets: list[str], recursive: bool
) -> tuple[list[LuminarEntry], list[str]]:
    selected: dict[str, LuminarEntry] = {}
    not_in_catalog = []
    for target in targets:
        if os.path.isdir(target):
            prefix = target.rstrip("/") + "/"
            for path, entry in live.items():
                if path.startswith(prefix) and (recursive or "/" not in path[len(prefix):]):
                    selected[path] = entry
        elif target in live:
            selected[target] = live[target]
        else:
            not_in_catalog.append(target)
    return sorted(selected.values(), key=lambda e: e.path), not_in_catalog


def build_plan(
    catalog: Catalog,
    targets: list[str],
    recursive: bool,
    digikam: dict[str, tuple[int, int]],
    state: dict,
    prefer: str,
    exiftool: str,
    compatible: bool,
) -> Plan:
    entries, not_in_catalog = select_entries(catalog.live_by_path(), targets, recursive)
    missing = [e.path for e in entries if not os.path.exists(e.path)]
    entries = [e for e in entries if os.path.exists(e.path)]

    sidecars = {e.path: sidecar_path(e.path, compatible) for e in entries}
    with_sidecar = {e.path for e in entries if os.path.isfile(sidecars[e.path])}
    sidecar_meta = read_metadata(exiftool, [sidecars[p] for p in with_sidecar])
    # without a sidecar and without digiKam data the image is only read if there is something to write
    # (reading a file that lives only in the cloud triggers a download)
    unknown = [
        e.path for e in entries if e.path not in digikam and e.path not in with_sidecar and (e.pick or e.rating)
    ]
    image_meta = read_metadata(exiftool, unknown, ("-IFD0:Rating",))

    items = []
    saved = state.get("items", {})
    for e in entries:
        side = sidecar_meta.get(nfc(sidecars[e.path]), {})
        image = image_meta.get(e.path, {})
        dk_pick, dk_rating = digikam.get(e.path, (None, None))
        cur = {
            "pick": next((v for v in (parse_pick(side), dk_pick, parse_pick(image)) if v is not None), PICK_NONE),
            "rating": next((v for v in (parse_rating(side), dk_rating, parse_rating(image)) if v is not None), 0),
        }
        lum = {"pick": e.pick, "rating": e.rating}
        db = {"pick": dk_pick, "rating": dk_rating}
        item = Item(e.path, sidecars[e.path], e.path in with_sidecar, lum, cur, db=db)
        base = saved.get(e.path, {})
        for f in FIELDS:
            # state: [Luminar, digiKam/sidecar, digiKam database] at the last --apply (older files have only 2 items)
            base_lum, base_cur, base_db = (list(base.get(f, ())) + [None, None, None])[:3]
            stale = (
                base_db is not None
                and db[f] is not None
                and base_cur != base_db  # back then the sidecar got a different value than the database had
                and db[f] == base_db  # the database has not changed since (no re-read)
                and cur[f] == db[f]  # the sidecar went back to the database's old value
            )
            item.decisions[f] = decide(lum[f], cur[f], base_lum, base_cur, prefer, stale)
        items.append(item)
    return Plan(items, missing, not_in_catalog)


# ---------------------------------------------------------------- output


def fmt(field_name: str, value: int) -> str:
    if field_name == "pick":
        return PICK_NAMES.get(value, str(value))
    return f"{value}★" if value else "–"


def describe(item: Item, verbose: bool) -> list[str]:
    parts = []
    for f in FIELDS:
        action, reason = item.decisions[f]
        if action == NONE or (action == KEEP and not verbose):
            continue
        if action == WRITE:
            change = f"{fmt(f, item.cur[f])} → {fmt(f, item.lum[f])}"
        elif action == PULL:
            change = f"Luminar {fmt(f, item.lum[f])} → {fmt(f, item.cur[f])}"
        else:
            change = f"digiKam {fmt(f, item.cur[f])}, Luminar {fmt(f, item.lum[f])}"
        parts.append(f"{FIELD_LABELS[f]}: {change} ({ACTION_LABELS[action]}: {reason})")
    if parts and item.needs_write and not item.has_sidecar:
        parts.append("[new sidecar]")
    return parts


def print_plan(plan: Plan, verbose: bool) -> None:
    last_dir = None
    for item in plan.items:
        parts = describe(item, verbose)
        if not parts:
            continue
        folder, name = os.path.split(item.image)
        if folder != last_dir:
            print(f"\n{folder}")
            last_dir = folder
        print(f"  {name}  " + "; ".join(parts))
    for path in plan.missing if verbose else []:
        print(f"  missing image file: {path}")
    for path in plan.not_in_catalog:
        print(f"  not in the Luminar catalog: {path}")


def summarize(plan: Plan) -> dict[str, int]:
    items = plan.items
    writes = [i for i in items if i.needs_write]
    return {
        "selected": len(items) + len(plan.missing),
        "write": len(writes),
        "new_sidecars": sum(not i.has_sidecar for i in writes),
        "pull": sum(i.needs_pull for i in items),
        "conflict": sum(CONFLICT in i.actions() for i in items),
        "keep": sum(KEEP in i.actions() for i in items),
        "equal": sum(i.actions() == {NONE} for i in items),
        "missing": len(plan.missing),
        "not_in_catalog": len(plan.not_in_catalog),
    }


def print_summary(summary: dict[str, int], applied: bool, pulled: bool) -> None:
    print("\nSummary" + ("" if applied else " (dry run, nothing changed)") + ":")
    print(f"  images selected:            {summary['selected']}")
    print(f"  sidecars to write:          {summary['write']} (new: {summary['new_sidecars']})")
    if summary["pull"]:
        hint = "" if pulled else " (with --to-luminar)"
        print(f"  to write back to Luminar:   {summary['pull']}{hint}")
    print(f"  conflicts (not written):    {summary['conflict']}")
    print(f"  digiKam values kept:        {summary['keep']}")
    print(f"  equal:                      {summary['equal']}")
    if summary["missing"]:
        print(f"  missing image files:        {summary['missing']}")
    if summary["not_in_catalog"]:
        print(f"  not in the Luminar catalog: {summary['not_in_catalog']}")


def plan_to_json(plan: Plan, applied: bool, pulled: bool, failed: list[str], backup: Path | None) -> dict:
    return {
        "applied": applied,
        "pulled": pulled,
        "backup": str(backup) if backup else None,
        "failed": failed,
        "summary": summarize(plan),
        "missing": plan.missing,
        "not_in_catalog": plan.not_in_catalog,
        "items": [
            {
                "image": i.image,
                "has_sidecar": i.has_sidecar,
                "luminar": i.lum,
                "digikam": i.cur,
                "decisions": {f: {"action": a, "reason": r} for f, (a, r) in i.decisions.items()},
            }
            for i in plan.items
        ],
    }


def top_roots(roots: list[str]) -> list[str]:
    """The Luminar folders that are not inside another one (the roots of the folder tree)."""
    ordered = sorted(roots)
    result: list[str] = []
    for root in ordered:
        if not result or not root.startswith(result[-1].rstrip("/") + "/"):
            result.append(root)
    return result


def overview(catalog: Catalog, digikam: dict[str, tuple[int, int]], state: dict) -> list[dict]:
    """Per-folder totals from the two databases and the state file only (no sidecars read, so an estimate).

    differs: Luminar and digiKam's database differ; reread: already synced, but digiKam has not re-read it yet.
    """
    saved = state.get("items", {})
    folders: dict[str, dict] = {}
    for e in catalog.entries:
        if not e.live:
            continue
        path = os.path.dirname(e.path)
        folder = folders.setdefault(
            path, {"path": path, "images": 0, "flagged": 0, "rated": 0, "differs": 0, "reread": 0}
        )
        folder["images"] += 1
        folder["flagged"] += e.pick != PICK_NONE
        folder["rated"] += e.rating > 0
        db = digikam.get(e.path, (PICK_NONE, 0))
        if db != (e.pick, e.rating):
            base = saved.get(e.path, {})
            fields = (("pick", e.pick, db[0]), ("rating", e.rating, db[1]))
            synced = all(list(base.get(f, ()))[:2] == [lum, lum] for f, lum, _ in fields)
            # if digiKam's database changed since the sync, it was edited there (not just waiting for a re-read)
            db_unchanged = all(len(base.get(f, ())) < 3 or base[f][2] in (None, value) for f, _, value in fields)
            folder["reread" if synced and db_unchanged else "differs"] += 1
    return sorted(folders.values(), key=lambda f: f["path"])


def print_overview(folders: list[dict]) -> None:
    print("differ  awaiting re-read / images  folder")
    for folder in folders:
        if folder["differs"] or folder["reread"]:
            print(f"{folder['differs']:6d}  {folder['reread']:6d} / {folder['images']:<6d}  {folder['path']}")


# ---------------------------------------------------------------- writing and state


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": 1, "items": {}}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def update_state(state: dict, items: list[Item], failed: set[str], pulled: bool) -> None:
    saved = state.setdefault("items", {})
    for item in items:
        if item.image in failed:
            continue
        entry = saved.setdefault(item.image, {})
        target, luminar_target = item.target, item.luminar_target
        for f in FIELDS:
            action = item.decisions[f][0]
            # conflicts and write-backs that were not carried out keep their history, so the next run reports them again
            if action == CONFLICT or (action == PULL and not pulled):
                continue
            entry[f] = [luminar_target[f], target[f], item.db.get(f)]


def new_backup_dir(state_path: Path) -> Path:
    base = state_path.parent / "backups" / time.strftime("%Y%m%d-%H%M%S")
    backup_dir, n = base, 1
    while backup_dir.exists():  # two writes within the same second (e.g. from the GUI)
        n += 1
        backup_dir = base.with_name(f"{base.name}-{n}")
    return backup_dir


def write_luminar(catalog: Catalog, pulls: list[Item], backup_dir: Path) -> list[dict]:
    """digiKam → Luminar: writes the image_user_attributes table in a single transaction.

    Only while Luminar is not running. A copy of the whole catalog is saved first. If any image's value changed in
    Luminar since it was read, nothing is written. Returns the old and new value per image, for undo.
    """
    if catalog.path is None:
        raise SyncError("unknown Luminar catalog")
    if process_running("Luminar"):
        raise SyncError("Luminar is running – the catalog can only be written while Luminar is closed (Cmd+Q)")
    changes = []
    for item in pulls:
        target = item.luminar_target
        for entry in catalog.live_entries(item.image):
            changes.append((entry, PICK_TO_MARKER[target["pick"]], target["rating"]))

    copy = backup_dir / "catalog" / catalog.path.name
    copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(catalog.path, copy)

    log = []
    con = sqlite3.connect(str(catalog.path), isolation_level=None, timeout=5)
    try:
        if con.execute("PRAGMA user_version").fetchone()[0] != catalog.schema:
            raise SyncError("the Luminar catalog version changed in the meantime")
        con.execute("BEGIN IMMEDIATE")
        try:
            for entry, marker, rating in changes:
                row = con.execute(
                    "SELECT marker, rating FROM image_user_attributes WHERE _out_id_int_64 = ?", (entry.image_id,)
                ).fetchone()
                if row is None or (row[0], clamp_rating(row[1])) != (entry.marker, entry.rating):
                    raise SyncError(f"changed in Luminar in the meantime: {entry.path} – run again")
                con.execute(
                    "UPDATE image_user_attributes SET marker = ?, rating = ? WHERE _out_id_int_64 = ?",
                    (marker, rating, entry.image_id),
                )
                log.append({"id": entry.image_id, "path": entry.path, "old": [row[0], row[1]], "new": [marker, rating]})
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        check = con.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        con.close()
    if check != "ok":
        raise SyncError(f"the Luminar catalog check reported an error ({check}); backup: {copy}")
    return log


def apply_plan(
    plan: Plan, exiftool: str, state: dict, state_path: Path, catalog: Catalog, to_luminar: bool
) -> tuple[list[str], Path | None]:
    """Returns the images that failed and the backup folder (if anything was written)."""
    writes = [i for i in plan.items if i.needs_write]
    pulls = [i for i in plan.items if i.needs_pull] if to_luminar else []
    if not writes and not pulls:
        update_state(state, plan.items, set(), pulled=False)
        save_state(state_path, state)
        return [], None

    backup_dir = new_backup_dir(state_path)
    backup_dir.mkdir(parents=True)
    if state_path.is_file():
        shutil.copy2(state_path, backup_dir / "state.json")
    manifest = {"modified": [], "created": [], "catalog": str(catalog.path) if pulls else None, "luminar": []}
    # writing Luminar is a single transaction: if it fails, the sidecars are not touched either
    if pulls:
        manifest["luminar"] = write_luminar(catalog, pulls, backup_dir)
    for item in writes:
        if item.has_sidecar:
            dst = backup_dir / "files" / item.sidecar.lstrip("/")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item.sidecar, dst)
            manifest["modified"].append(item.sidecar)
        else:
            manifest["created"].append(item.sidecar)
    (backup_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")

    proc = None
    if writes:
        args = []
        for item in writes:
            tags = tag_args(item.target["pick"], item.target["rating"])
            if item.has_sidecar:
                args += ["-overwrite_original_in_place", *tags, item.sidecar]
            else:
                # new sidecar: the image's own XMP goes in too, because digiKam ignores embedded XMP once a sidecar exists
                args += ["-tagsfromfile", item.image, "-xmp:all", *tags, "-o", item.sidecar, item.image]
            args.append("-execute")
        proc = run_exiftool(exiftool, args, ["-common_args", "-q", "-q", "-m"])

    # verification: read back what was written
    meta = read_metadata(exiftool, [i.sidecar for i in writes])
    failed = set()
    for item in writes:
        rec = meta.get(nfc(item.sidecar), {})
        if (parse_pick(rec), parse_rating(rec)) != (item.target["pick"], item.target["rating"]):
            failed.add(item.image)
    if failed and proc is not None and proc.stderr.strip():
        print(proc.stderr.strip(), file=sys.stderr)
    update_state(state, plan.items, failed, pulled=bool(pulls))
    save_state(state_path, state)
    return sorted(failed), backup_dir


def undo(backup_dir: Path, state_path: Path) -> dict[str, int]:
    """Undoes an --apply. In Luminar it only restores what nobody has changed since."""
    manifest_file = backup_dir / "manifest.json"
    if not manifest_file.is_file():
        raise SyncError(f"not a backup folder: {backup_dir}")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    counts = {"restored": 0, "deleted": 0, "luminar_restored": 0, "luminar_skipped": 0}

    if manifest.get("luminar"):
        if process_running("Luminar"):
            raise SyncError("Luminar is running – quit it before undoing (Cmd+Q)")
        con = sqlite3.connect(manifest["catalog"], isolation_level=None, timeout=5)
        try:
            con.execute("BEGIN IMMEDIATE")
            for change in manifest["luminar"]:
                row = con.execute(
                    "SELECT marker, rating FROM image_user_attributes WHERE _out_id_int_64 = ?", (change["id"],)
                ).fetchone()
                if row is not None and list(row) == change["new"]:
                    con.execute(
                        "UPDATE image_user_attributes SET marker = ?, rating = ? WHERE _out_id_int_64 = ?",
                        (*change["old"], change["id"]),
                    )
                    counts["luminar_restored"] += 1
                else:
                    counts["luminar_skipped"] += 1
            con.execute("COMMIT")
        finally:
            con.close()

    for path in manifest["modified"]:
        shutil.copy2(backup_dir / "files" / path.lstrip("/"), path)
        counts["restored"] += 1
    for path in manifest["created"]:
        if os.path.isfile(path):
            os.remove(path)
            counts["deleted"] += 1
    saved_state = backup_dir / "state.json"
    if saved_state.is_file():
        shutil.copy2(saved_state, state_path)
    elif state_path.is_file():
        state_path.unlink()
    return counts


# ---------------------------------------------------------------- audit


def find_twin(path: str, roots_by_name: dict[str, list[str]], live: set[str]) -> str | None:
    """The counterpart of an orphaned entry: the same relative path under a live Luminar folder."""
    candidates = []
    for part in path.split("/"):
        pos = path.find("/" + part + "/")
        if pos < 0:
            continue
        for root in roots_by_name.get(part, ()):
            candidates.append(root + path[pos + len(part) + 1:])
    for candidate in candidates:
        if candidate in live:
            return candidate
    return next((c for c in candidates if os.path.exists(c)), None)


def audit(catalog: Catalog, verbose: bool) -> None:
    live_entries = [e for e in catalog.entries if e.live]
    orphans = [e for e in catalog.entries if not e.live]
    live = {e.path: e for e in live_entries}
    live_paths = set(live)
    roots_by_name: dict[str, list[str]] = {}
    for root in catalog.roots:
        roots_by_name.setdefault(os.path.basename(root), []).append(root)

    missing_live = [e.path for e in live_entries if not os.path.exists(e.path)]
    twins, on_disk, lost, differing = [], [], [], []
    for e in orphans:
        twin = find_twin(e.path, roots_by_name, live_paths)
        if twin in live:
            twins.append(e.path)
            other = live[twin]
            if (e.pick or e.rating) and (e.pick, e.rating) != (other.pick, other.rating):
                differing.append(f"{e.path}  (orphan: {fmt('pick', e.pick)}/{fmt('rating', e.rating)}, "
                                 f"live: {fmt('pick', other.pick)}/{fmt('rating', other.rating)})")
        elif twin:
            on_disk.append(f"{e.path} -> {twin}")
        else:
            lost.append(e.path)

    def section(title: str, paths: list[str]) -> None:
        print(f"    {title}: {len(paths)}")
        for path in paths if verbose else paths[:10]:
            print(f"        {path}")
        if not verbose and len(paths) > 10:
            print("        … (full list: -v)")

    print(f"Entries in total: {len(catalog.entries)}")
    print(f"  live (in a Luminar folder): {len(live_entries)}, duplicate paths: {len(live_entries) - len(live)}")
    section("missing on disk", missing_live)
    print(f"  orphaned (in no Luminar folder): {len(orphans)}")
    print(f"    with a live counterpart in the catalog: {len(twins)}")
    section("of which the counterpart's flag/rating differs", differing)
    section("not in the catalog, but the file exists on disk", on_disk)
    section("found nowhere", lost)


# ---------------------------------------------------------------- digiKam check

# digiKam color labels (internal tags) and the names used in files
DIGIKAM_COLOR_TAGS = {
    "Color Label None": 0, "Color Label Red": 1, "Color Label Orange": 2, "Color Label Yellow": 3,
    "Color Label Green": 4, "Color Label Blue": 5, "Color Label Magenta": 6, "Color Label Gray": 7,
    "Color Label Black": 8, "Color Label White": 9,
}
COLOR_NAMES = {0: "–", 1: "Red", 2: "Orange", 3: "Yellow", 4: "Green", 5: "Blue", 6: "Magenta", 7: "Gray",
               8: "Black", 9: "White"}
XMP_LABEL_COLORS = {"red": 1, "orange": 2, "yellow": 3, "green": 4, "blue": 5, "magenta": 6, "purple": 6,
                    "gray": 7, "grey": 7, "black": 8, "white": 9}
CHECK_READ_TAGS = XMP_READ_TAGS + (
    "-XMP-digiKam:ColorLabel", "-XMP-xmp:Label",
    "-XMP-digiKam:TagsList", "-XMP-lr:HierarchicalSubject", "-XMP-microsoft:LastKeywordXMP", "-XMP-dc:Subject",
    "-XMP-dc:Description", "-XMP-dc:Title",
    "-IPTC:Keywords", "-IPTC:Caption-Abstract", "-IPTC:ObjectName",
    "-IFD0:Rating", "-IFD0:ImageDescription", "-ExifIFD:UserComment",
)
CHECK_FIELD_LABELS = {"pick": "flag", "color": "color label", "rating": "rating", "tag": "tag", "caption": "caption"}
OVERWRITTEN, DB_ONLY = "overwritten", "db_only"


@dataclass
class DigikamInfo:
    path: str
    pick: int = PICK_NONE
    color: int = 0
    rating: int = 0
    tags: list[str] = field(default_factory=list)  # full path, e.g. “Family/Alice”
    captions: list[str] = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        return bool(self.pick or self.color or self.rating or self.tags or self.captions)


def load_digikam_details(db: Path | None, tmpdir: str, targets: list[str], recursive: bool) -> list[DigikamInfo]:
    """Per-image data from digiKam's database (flag, color, rating, tags, captions) at the given locations."""
    if db is None or not db.is_file():
        raise SyncError("digiKam's database was not found")
    con = open_snapshot(db, tmpdir)
    try:
        tags = {tid: (pid, name) for tid, pid, name in con.execute("SELECT id, pid, name FROM Tags")}
        images = con.execute(
            "SELECT i.id, r.specificPath, a.relativePath, i.name, ii.rating FROM Images i "
            "JOIN Albums a ON a.id = i.album JOIN AlbumRoots r ON r.id = a.albumRoot "
            "LEFT JOIN ImageInformation ii ON ii.imageid = i.id WHERE i.status = 1"
        ).fetchall()
        image_tags = con.execute("SELECT imageid, tagid FROM ImageTags").fetchall()
        comments = con.execute("SELECT imageid, comment FROM ImageComments WHERE trim(comment) <> ''").fetchall()
    finally:
        con.close()

    internal = {tid for tid, (pid, name) in tags.items() if name == "_Digikam_Internal_Tags_"}

    def tag_path(tid: int) -> str | None:
        parts, seen = [], set()
        while tid in tags and tid not in seen:
            seen.add(tid)
            if tid in internal:
                return None
            pid, name = tags[tid]
            parts.append(name)
            tid = pid
        return "/".join(reversed(parts))

    def selected(path: str) -> bool:
        for target in targets:
            if path == target:
                return True
            prefix = target.rstrip("/") + "/"
            if path.startswith(prefix) and (recursive or "/" not in path[len(prefix):]):
                return True
        return False

    infos: dict[int, DigikamInfo] = {}
    for image_id, root, rel, name, rating in images:
        path = nfc(root.rstrip("/") + ("" if rel == "/" else rel) + "/" + name)
        if selected(path):
            infos[image_id] = DigikamInfo(path, rating=clamp_rating(rating))
    for image_id, tid in image_tags:
        info = infos.get(image_id)
        if info is None or tid not in tags:
            continue
        name = tags[tid][1]
        if name in DIGIKAM_PICK_TAGS:
            info.pick = DIGIKAM_PICK_TAGS[name]
        elif name in DIGIKAM_COLOR_TAGS:
            info.color = DIGIKAM_COLOR_TAGS[name]
        else:
            path = tag_path(tid)
            if path:
                info.tags.append(path)
    for image_id, comment in comments:
        if image_id in infos:
            infos[image_id].captions.append(comment)
    return sorted(infos.values(), key=lambda i: i.path)


def is_cloud_only(path: str) -> bool:
    """A file that lives in the cloud and is not downloaded locally (reading it would trigger a download)."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    return bool(getattr(st, "st_flags", 0) & 0x40000000) or (st.st_size > 0 and getattr(st, "st_blocks", 1) == 0)


def _values(rec: dict, key: str) -> list[str]:
    value = rec.get(key)
    if value is None:
        return []
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


def _norm(text: str) -> str:
    return " ".join(text.split())


def file_color(rec: dict) -> int | None:
    value = rec.get("XMP-digiKam:ColorLabel")
    if value is not None:
        try:
            return int(value) if int(value) in COLOR_NAMES else 0
        except (TypeError, ValueError):
            pass
    label = rec.get("XMP-xmp:Label")
    if label is not None:
        return XMP_LABEL_COLORS.get(str(label).strip().lower(), 0)
    return None


def compare_with_files(info: DigikamInfo, rec: dict) -> list[dict]:
    """What would change in digiKam's database on a re-read (rec: what digiKam would read from the files)."""
    issues = []

    def scalar(field_name: str, db_value: int, file_value: int | None, fmt_value) -> None:
        if file_value is not None and file_value != db_value:
            issues.append({"field": field_name, "kind": OVERWRITTEN, "db": fmt_value(db_value), "file": fmt_value(file_value)})
        elif file_value is None and db_value:
            issues.append({"field": field_name, "kind": DB_ONLY, "db": fmt_value(db_value), "file": None})

    scalar("pick", info.pick, parse_pick(rec), lambda v: PICK_NAMES.get(v, str(v)))
    scalar("color", info.color, file_color(rec), lambda v: COLOR_NAMES.get(v, str(v)))
    scalar("rating", info.rating, parse_rating(rec), lambda v: f"{v}★" if v else "–")

    # tags: whatever is missing from the files also disappears from digiKam's database on a re-read
    paths = {v.strip("/") for key in ("XMP-digiKam:TagsList", "XMP-microsoft:LastKeywordXMP") for v in _values(rec, key)}
    paths |= {v.replace("|", "/") for v in _values(rec, "XMP-lr:HierarchicalSubject")}
    flat = {v for key in ("XMP-dc:Subject", "IPTC:Keywords") for v in _values(rec, key)}
    for tag in info.tags:
        if tag not in paths and tag.rsplit("/", 1)[-1] not in flat:
            issues.append({"field": "tag", "kind": DB_ONLY, "db": tag, "file": None})

    file_captions = {
        _norm(v)
        for key in ("XMP-dc:Description", "XMP-dc:Title", "IPTC:Caption-Abstract", "IPTC:ObjectName",
                    "IFD0:ImageDescription", "ExifIFD:UserComment")
        for v in _values(rec, key)
        if v.strip()
    }
    for caption in info.captions:
        if _norm(caption) not in file_captions:
            kind = OVERWRITTEN if file_captions else DB_ONLY
            issues.append({"field": "caption", "kind": kind, "db": caption,
                           "file": next(iter(file_captions)) if file_captions else None})
    return issues


def check_digikam(
    infos: list[DigikamInfo], exiftool: str, compatible: bool, include_cloud: bool
) -> list[dict]:
    """Per image: what would change or could be lost in digiKam after “Reread Metadata From Files”."""
    infos = [i for i in infos if i.has_data and os.path.exists(i.path)]
    sidecars = {i.path: sidecar_path(i.path, compatible) for i in infos}
    with_sidecar = [i.path for i in infos if os.path.isfile(sidecars[i.path])]
    side_meta = read_metadata(exiftool, [sidecars[p] for p in with_sidecar], CHECK_READ_TAGS)

    # first pass from the sidecars only; the image file is read only if the sidecar does not settle everything
    need_image, results = [], {}
    for info in infos:
        side = side_meta.get(nfc(sidecars[info.path]), {})
        if compare_with_files(info, side):
            need_image.append(info.path)
        else:
            results[info.path] = []
    unchecked = [p for p in need_image if not include_cloud and is_cloud_only(p)]
    image_meta = read_metadata(exiftool, [p for p in need_image if p not in unchecked], CHECK_READ_TAGS)

    report = []
    for info in infos:
        if info.path in unchecked:
            report.append({"image": info.path, "unchecked": True, "issues": []})
            continue
        if info.path not in results:
            rec = dict(image_meta.get(info.path, {}))
            if info.path in with_sidecar:
                # with a sidecar digiKam ignores the image's embedded XMP and only reads its EXIF/IPTC
                rec = {k: v for k, v in rec.items() if not k.startswith("XMP-")}
                rec.update(side_meta.get(nfc(sidecars[info.path]), {}))
            results[info.path] = compare_with_files(info, rec)
        if results[info.path]:
            report.append({"image": info.path, "unchecked": False, "issues": results[info.path]})
    return report


def check_summary(infos_total: int, report: list[dict]) -> dict[str, int]:
    return {
        "images": infos_total,
        "overwritten": sum(any(i["kind"] == OVERWRITTEN for i in r["issues"]) for r in report),
        "db_only": sum(any(i["kind"] == DB_ONLY for i in r["issues"]) for r in report),
        "unchecked": sum(r["unchecked"] for r in report),
    }


def print_check(report: list[dict], summary: dict[str, int], verbose: bool) -> None:
    print("digiKam check – what would change in digiKam after “Reread Metadata From Files”:")
    last_dir = None
    for entry in report:
        if entry["unchecked"] and not verbose:
            continue
        folder, name = os.path.split(entry["image"])
        if folder != last_dir:
            print(f"\n{folder}")
            last_dir = folder
        if entry["unchecked"]:
            print(f"  {name}  not checked (cloud only)")
            continue
        parts = []
        for issue in entry["issues"]:
            label = CHECK_FIELD_LABELS[issue["field"]]
            if issue["kind"] == OVERWRITTEN:
                parts.append(f"{label}: database “{issue['db']}”, file “{issue['file']}” (would be overwritten)")
            else:
                parts.append(f"{label}: “{issue['db']}” (only in the database)")
        print(f"  {name}  " + "; ".join(parts))
    print("\nSummary:")
    print(f"  images in digiKam:          {summary['images']}")
    print(f"  would be overwritten:       {summary['overwritten']} images")
    print(f"  only in the database:       {summary['db_only']} images")
    if summary["unchecked"]:
        hint = "" if verbose else " (list: -v; check with download: --include-cloud)"
        print(f"  not checked (cloud only):   {summary['unchecked']} images{hint}")
    if summary["overwritten"] or summary["db_only"]:
        print("\nSuggestion: before re-reading, select the album in digiKam and write the database values to the "
              "files (Album → Write Metadata to Files). Do this BEFORE running LuminarSync.")


# ---------------------------------------------------------------- main


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Two-way sync of flags and star ratings between Luminar Neo and digiKam XMP sidecars. "
        "Dry run by default.",
    )
    p.add_argument("paths", nargs="*", help="image files and/or folders")
    p.add_argument("-r", "--recursive", action="store_true", help="walk folders recursively")
    p.add_argument("--files-from", metavar="FILE", help="more paths from a file, one per line")
    p.add_argument("--apply", action="store_true", help="really write (without it only the plan is printed)")
    p.add_argument(
        "--to-luminar",
        action="store_true",
        help="write values changed in digiKam back to the Luminar catalog (only while Luminar is closed)",
    )
    p.add_argument(
        "--prefer",
        choices=("auto", "luminar", "digikam"),
        default="auto",
        help="auto: the side that changed wins (default); luminar: Luminar's value overwrites; "
        "digikam: digiKam's value goes to Luminar (implies --to-luminar)",
    )
    p.add_argument("--overview", action="store_true", help="per-folder totals (estimate from the two databases)")
    p.add_argument("--audit", action="store_true", help="catalog report: orphaned and missing entries")
    p.add_argument(
        "--check-digikam",
        action="store_true",
        help="what would change or be lost in digiKam when re-reading metadata from the files",
    )
    p.add_argument(
        "--include-cloud",
        action="store_true",
        help="--check-digikam: also check cloud-only images (triggers downloads)",
    )
    p.add_argument("--undo", metavar="FOLDER", help="undo an earlier --apply from its backup folder")
    p.add_argument("--json", action="store_true", help="machine-readable output (used by the GUI)")
    p.add_argument("-v", "--verbose", action="store_true", help="detailed list (kept values too)")
    p.add_argument("--catalog", help="Luminar catalog (default: the active catalog)")
    p.add_argument("--exiftool", help="exiftool (default: the copy bundled with digiKam)")
    p.add_argument("--state", default=str(STATE_FILE), help="sync state file")
    p.add_argument("--digikamrc", default=str(DIGIKAMRC), help=argparse.SUPPRESS)
    p.add_argument("--digikam-db", help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if not (args.paths or args.files_from or args.audit or args.undo or args.overview):
        p.error("give at least one file or folder")
    if args.json and args.audit:
        p.error("--audit is not available with --json")
    return args


def collect_targets(args: argparse.Namespace) -> list[str]:
    raw_paths = list(args.paths)
    if args.files_from:
        try:
            lines = Path(args.files_from).expanduser().read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise SyncError(f"cannot read: {args.files_from} ({exc})")
        raw_paths += [line for line in lines if line.strip()]
    targets = []
    for raw in raw_paths:
        target = nfc(os.path.abspath(os.path.expanduser(raw)))
        if not os.path.exists(target):
            raise SyncError(f"does not exist: {raw}")
        targets.append(target)
    return targets


def emit_json(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False))


def run_check(args: argparse.Namespace, targets: list[str]) -> int:
    rc = read_digikamrc(Path(args.digikamrc).expanduser())
    compatible = rc.get("Metadata Settings", {}).get("Use Compatible File Name") == "true"
    dk_db = Path(args.digikam_db).expanduser() if args.digikam_db else digikam_db_from_rc(rc)
    exiftool = find_exiftool(args.exiftool)
    with tempfile.TemporaryDirectory() as tmpdir:
        infos = load_digikam_details(dk_db, tmpdir, targets, args.recursive)
    report = check_digikam(infos, exiftool, compatible, args.include_cloud)
    summary = check_summary(len(infos), report)
    if args.json:
        emit_json({"check": {"summary": summary, "items": report}})
    else:
        print_check(report, summary, args.verbose)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    state_path = Path(args.state).expanduser()
    try:
        if args.undo:
            counts = undo(Path(args.undo).expanduser(), state_path)
            if args.json:
                emit_json({"undo": counts})
            else:
                print(f"Restored: {counts['restored']} sidecars, deleted: {counts['deleted']} new sidecars.")
                if counts["luminar_restored"] or counts["luminar_skipped"]:
                    print(f"Luminar: {counts['luminar_restored']} images restored, "
                          f"{counts['luminar_skipped']} skipped (changed in Luminar since).")
            return 0
        targets = collect_targets(args)
        if args.check_digikam:
            return run_check(args, targets)
        catalog_path = Path(args.catalog).expanduser() if args.catalog else active_catalog()
        if not args.json:
            print(f"Luminar catalog: {catalog_path}")
        with tempfile.TemporaryDirectory() as tmpdir:
            catalog = load_luminar(catalog_path, tmpdir)
            if args.audit:
                audit(catalog, args.verbose)
                if not targets:
                    return 0
            rc = read_digikamrc(Path(args.digikamrc).expanduser())
            compatible = rc.get("Metadata Settings", {}).get("Use Compatible File Name") == "true"
            dk_db = Path(args.digikam_db).expanduser() if args.digikam_db else digikam_db_from_rc(rc)
            digikam = load_digikam(dk_db, tmpdir)
        state = load_state(state_path)
        if args.overview:
            folders = overview(catalog, digikam, state)
            if args.json:
                emit_json({"catalog": str(catalog_path), "roots": top_roots(catalog.roots), "folders": folders})
            else:
                print_overview(folders)
            return 0
        exiftool = find_exiftool(args.exiftool)
        plan = build_plan(catalog, targets, args.recursive, digikam, state, args.prefer, exiftool, compatible)
        to_luminar = args.to_luminar or args.prefer == "digikam"
        pulled = args.apply and to_luminar and any(i.needs_pull for i in plan.items)
        # writing sidecars requires quitting digiKam; writing Luminar does not
        if args.apply and any(i.needs_write for i in plan.items) and process_running("digikam"):
            raise SyncError("digiKam is running – quit it before writing, otherwise it may overwrite the sidecars "
                            "from its own database")
        if args.apply:
            failed, backup = apply_plan(plan, exiftool, state, state_path, catalog, to_luminar)
        else:
            failed, backup = [], None
        if args.json:
            emit_json(plan_to_json(plan, args.apply, pulled, failed, backup))
            return 1 if failed else 0
        print_plan(plan, args.verbose)
        if backup:
            print(f"\nBackup: {backup}  (undo: --undo \"{backup}\")")
        print_summary(summarize(plan), args.apply, pulled)
        if failed:
            print(f"\nERROR: {len(failed)} sidecars could not be written:", file=sys.stderr)
            for path in failed:
                print(f"  {path}", file=sys.stderr)
            return 1
        if not args.apply and any(i.needs_write for i in plan.items):
            print("\nRun again with --apply to write.")
        if not pulled and any(i.needs_pull for i in plan.items):
            print("To write digiKam changes back: --apply --to-luminar (with Luminar closed).")
        return 0
    except SyncError as exc:
        if args.json:
            emit_json({"error": str(exc)})
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
