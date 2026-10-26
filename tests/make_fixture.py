"""Synthetic Luminar catalog, digiKam database and images for the tests.

The Luminar table schema is taken from a real catalog (Luminar Neo 1.28.1, user_version 155).
"""

from __future__ import annotations

import base64
import json
import sqlite3
import subprocess
import unicodedata
from pathlib import Path

LUMINAR_SCHEMA = """
CREATE TABLE images (_id_int_64 INTEGER NOT NULL, marked_to_delete_bool INTEGER NOT NULL DEFAULT 0, guid_wide_ch TEXT NOT NULL UNIQUE, path_wide_ch TEXT NOT NULL, width INTEGER NOT NULL DEFAULT 0, height INTEGER NOT NULL DEFAULT 0, file_size_int_64 INTEGER NOT NULL DEFAULT 0, modification_date_int_64 INTEGER NOT NULL DEFAULT 0, import_date_int_64 INTEGER NOT NULL DEFAULT 0, import_index INTEGER NOT NULL DEFAULT 0, creation_date_int_64 INTEGER NOT NULL DEFAULT 0, file_type INTEGER NOT NULL DEFAULT 0, cloud_id_wide_ch TEXT NOT NULL DEFAULT '', updated_at_int_64 INTEGER NOT NULL DEFAULT 0, deleted_at_int_64 INTEGER NOT NULL DEFAULT 0, sync_status INTEGER NOT NULL DEFAULT 0 , PRIMARY KEY (_id_int_64));
CREATE TABLE image_user_attributes (_id_int_64 INTEGER NOT NULL, marked_to_delete_bool INTEGER NOT NULL DEFAULT 0, _out_id_int_64 INTEGER NOT NULL UNIQUE, trash_bool INTEGER NOT NULL DEFAULT 0, marker INTEGER NOT NULL DEFAULT 0, color INTEGER NOT NULL DEFAULT 0, rating INTEGER NOT NULL DEFAULT 0, edit_date_int_64 INTEGER NOT NULL DEFAULT 0, opened_bool INTEGER NOT NULL DEFAULT 0, origin_path_wide_ch TEXT NOT NULL DEFAULT '', _trashed_date_int_64 INTEGER NOT NULL DEFAULT 0, lost_bool INTEGER NOT NULL DEFAULT 0, tutorial INTEGER NOT NULL DEFAULT 0, smart_search_indexed INTEGER NOT NULL DEFAULT 0, virtual_copy_bool INTEGER NOT NULL DEFAULT 0, virtual_copy_date_int_64 INTEGER NOT NULL DEFAULT 0, virtual_copy_number INTEGER NOT NULL DEFAULT 0 , PRIMARY KEY (_id_int_64), FOREIGN KEY (_out_id_int_64) REFERENCES images (_id_int_64));
CREATE TABLE paths (_id_int_64 INTEGER NOT NULL, marked_to_delete_bool INTEGER NOT NULL DEFAULT 0, volume_id_int_64 INTEGER NOT NULL, path_wide_ch TEXT NOT NULL , PRIMARY KEY (_id_int_64), FOREIGN KEY (volume_id_int_64) REFERENCES volumes (_id_int_64) unique(volume_id_int_64,path_wide_ch));
CREATE TABLE paths_images (_key_id_int_64 INTEGER NOT NULL, _val_id_int_64 INTEGER NOT NULL , PRIMARY KEY (_key_id_int_64,_val_id_int_64));
CREATE TABLE volumes (_id_int_64 INTEGER NOT NULL, marked_to_delete_bool INTEGER NOT NULL DEFAULT 0, guid_wide_ch TEXT NOT NULL, info_wide_ch TEXT NOT NULL , PRIMARY KEY (_id_int_64) unique(guid_wide_ch,info_wide_ch));
CREATE TABLE reffolders (_id_int_64 INTEGER NOT NULL, marked_to_delete_bool INTEGER NOT NULL DEFAULT 0, id_path_int_64 INTEGER, guid_wide_ch TEXT NOT NULL, library_order REAL NOT NULL DEFAULT 0 , PRIMARY KEY (_id_int_64), FOREIGN KEY (id_path_int_64) REFERENCES paths (_id_int_64));
"""

DIGIKAM_SCHEMA = """
CREATE TABLE AlbumRoots (id INTEGER PRIMARY KEY, label TEXT, status INTEGER, type INTEGER, identifier TEXT, specificPath TEXT);
CREATE TABLE Albums (id INTEGER PRIMARY KEY, albumRoot INTEGER, relativePath TEXT);
CREATE TABLE Images (id INTEGER PRIMARY KEY, album INTEGER, name TEXT, status INTEGER);
CREATE TABLE ImageInformation (imageid INTEGER PRIMARY KEY, rating INTEGER);
CREATE TABLE Tags (id INTEGER PRIMARY KEY, pid INTEGER, name TEXT);
CREATE TABLE ImageTags (imageid INTEGER, tagid INTEGER);
CREATE TABLE ImageComments (id INTEGER PRIMARY KEY, imageid INTEGER, type INTEGER, language TEXT, author TEXT, date DATETIME, comment TEXT);
"""

# 1x1 pixel JPEG
TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////"
    "////////////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA="
)

FOLDER = "Pictures/Photos/2026/Café Crème"  # Luminar stores it in NFD form

DIGIKAM_SIDECAR = """<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="XMP Core 4.4.0-Exiv2">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    xmlns:acdsee="http://ns.acdsee.com/iptc/1.0/"
    xmlns:MicrosoftPhoto="http://ns.microsoft.com/photo/1.0/"
    xmlns:digiKam="http://www.digikam.org/ns/1.0/"
    xmlns:xmpDM="http://ns.adobe.com/xmp/1.0/DynamicMedia/"
   acdsee:rating="{rating}"
   MicrosoftPhoto:Rating="{rating}"
   xmp:Rating="{rating}"
   digiKam:PickLabel="{pick}">
   <digiKam:TagsList><rdf:Seq><rdf:li>Family/Alice</rdf:li></rdf:Seq></digiKam:TagsList>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>
"""

# name: (Luminar marker, Luminar rating, file exists, sidecar (pick, rating) or None, digiKam DB (pick tag, rating) or None)
IMAGES = {
    "a_rejected.jpg": (2, 0, True, None, (None, -1)),
    "b_fav.jpg": (1, 0, True, (3, 0), ("Pick Label Accepted", 0)),
    "c_dkstar.jpg": (0, 0, True, None, (None, 1)),  # a rating only digiKam has (e.g. imported from Lightroom)
    "d_rated.jpg": (0, 4, True, (0, 0), (None, 0)),
    "e_conflict.jpg": (1, 0, True, (1, 0), ("Pick Label Rejected", 0)),
    "f_missing.jpg": (2, 0, False, None, None),
}
SUB_IMAGE = "Sub/h_sub.jpg"


def nfd(text: str) -> str:
    return unicodedata.normalize("NFD", text)


def write_sidecar(path: Path, pick: int, rating: int) -> None:
    path.write_text(DIGIKAM_SIDECAR.format(pick=pick, rating=rating), encoding="utf-8")


def make_images(root: Path) -> Path:
    folder = root / FOLDER
    (folder / "Sub").mkdir(parents=True)
    for name, (_, _, exists, sidecar, _) in IMAGES.items():
        if exists:
            (folder / name).write_bytes(TINY_JPEG)
        if sidecar:
            write_sidecar(folder / (name + ".xmp"), *sidecar)
    (folder / SUB_IMAGE).write_bytes(TINY_JPEG)
    # the embedded XMP has to end up in the new sidecar as well
    subprocess.run(
        ["exiftool", "-q", "-overwrite_original", "-XMP-dc:Subject=Keyword", str(folder / "a_rejected.jpg")],
        check=True,
    )
    return folder


def make_catalog(path: Path, root: Path, user_version: int = 155) -> None:
    con = sqlite3.connect(path)
    con.executescript(LUMINAR_SCHEMA)
    con.execute(f"PRAGMA user_version = {user_version}")
    info = json.dumps({"kMountPointSerializationKey": str(root)})
    old_disk = json.dumps({"kMountPointSerializationKey": "/Volumes/OldDisk"})
    con.execute("INSERT INTO volumes VALUES (2, 0, 'VOL-LIVE', ?)", (info,))
    con.execute("INSERT INTO volumes VALUES (5, 0, 'VOL-OLD', ?)", (old_disk,))
    folders = {
        10: (2, "Pictures/Photos"),
        11: (2, nfd(FOLDER)),
        12: (2, nfd(FOLDER) + "/Sub"),
        20: (5, "/Pictures/Photos/2026/Café Crème"),  # left behind after the folder was moved
        21: (5, "/Pictures/Photos/2019/Lost"),
    }
    for pid, (vol, folder) in folders.items():
        con.execute("INSERT INTO paths VALUES (?, 0, ?, ?)", (pid, vol, folder))
    con.execute("INSERT INTO reffolders VALUES (1, 0, -1, 'Root', 0)")
    con.execute("INSERT INTO reffolders VALUES (2, 0, 10, 'REF-1', 1)")

    image_id = 0

    def add(path_id: int, name: str, marker: int, rating: int, virtual_copy: int = 0) -> None:
        nonlocal image_id
        image_id += 1
        con.execute("INSERT INTO images (_id_int_64, guid_wide_ch, path_wide_ch) VALUES (?, ?, ?)",
                    (image_id, f"GUID-{image_id}", name))
        con.execute(
            "INSERT INTO image_user_attributes (_id_int_64, _out_id_int_64, marker, rating, virtual_copy_bool) "
            "VALUES (?, ?, ?, ?, ?)",
            (image_id, image_id, marker, rating, virtual_copy),
        )
        con.execute("INSERT INTO paths_images VALUES (?, ?)", (path_id, image_id))

    for name, (marker, rating, *_rest) in IMAGES.items():
        add(11, name, marker, rating)
    add(11, "a_rejected.jpg", 1, 5, virtual_copy=1)  # virtual copy: must be skipped
    add(12, "h_sub.jpg", 2, 0)
    add(20, "a_rejected.jpg", 0, 3)  # orphan with a live counterpart, different rating
    add(20, "b_fav.jpg", 1, 0)  # orphan with a live counterpart
    add(21, "lost.jpg", 0, 0)  # orphan, found nowhere
    con.commit()
    con.close()


def make_digikam_db(path: Path, root: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(DIGIKAM_SCHEMA)
    con.execute("INSERT INTO AlbumRoots VALUES (1, 'Pictures', 0, 1, 'volumeid:?uuid=x', ?)", (str(root / "Pictures"),))
    con.execute("INSERT INTO Albums VALUES (1, 1, ?)", ("/" + FOLDER.split("/", 1)[1],))
    con.execute("INSERT INTO Tags VALUES (1, 0, '_Digikam_Internal_Tags_')")
    tag_ids = {"Pick Label None": 18, "Pick Label Rejected": 19, "Pick Label Pending": 20, "Pick Label Accepted": 21}
    for name, tag_id in tag_ids.items():
        con.execute("INSERT INTO Tags VALUES (?, 1, ?)", (tag_id, name))
    colors = ["None", "Red", "Orange", "Yellow", "Green", "Blue", "Magenta", "Gray", "Black", "White"]
    for offset, color in enumerate(colors):
        con.execute("INSERT INTO Tags VALUES (?, 1, ?)", (8 + offset, f"Color Label {color}"))
    # user tags: Family/Alice and Sports
    con.execute("INSERT INTO Tags VALUES (100, 0, 'Family')")
    con.execute("INSERT INTO Tags VALUES (101, 100, 'Alice')")
    con.execute("INSERT INTO Tags VALUES (102, 0, 'Sports')")
    for image_id, (name, (*_rest, dk)) in enumerate(IMAGES.items(), start=1):
        if dk is None:
            continue
        pick, rating = dk
        con.execute("INSERT INTO Images VALUES (?, 1, ?, 1)", (image_id, name))
        con.execute("INSERT INTO ImageInformation VALUES (?, ?)", (image_id, rating))
        if pick:
            con.execute("INSERT INTO ImageTags VALUES (?, ?)", (image_id, tag_ids[pick]))
    con.commit()
    con.close()


def make_all(root: Path) -> dict[str, Path]:
    folder = make_images(root)
    catalog = root / "catalog.luminarneo"
    digikam_db = root / "digikam4.db"
    make_catalog(catalog, root)
    make_digikam_db(digikam_db, root)
    return {"folder": folder, "catalog": catalog, "digikam_db": digikam_db, "state": root / "app" / "state.json"}


if __name__ == "__main__":
    import sys

    print(make_all(Path(sys.argv[1])))
