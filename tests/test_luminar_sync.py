import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import luminar_sync as ls  # noqa: E402
import make_fixture as fx  # noqa: E402


def exif_values(path: Path, *tags: str) -> dict:
    out = subprocess.run(["exiftool", "-j", "-n", "-G1", *tags, str(path)], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)[0]


def sidecar_state(path: Path) -> tuple:
    rec = exif_values(path, *ls.XMP_READ_TAGS)
    return ls.parse_pick(rec), ls.parse_rating(rec)


class DecideTest(unittest.TestCase):
    def test_table(self):
        cases = [
            # (lum, cur, base_lum, base_cur, prefer) -> action
            ((1, 1, None, None, "auto"), ls.NONE),
            ((1, 0, None, None, "auto"), ls.WRITE),  # filling a gap
            ((0, 1, None, None, "auto"), ls.KEEP),  # e.g. a rating imported from Lightroom
            ((3, 1, None, None, "auto"), ls.CONFLICT),
            ((0, 1, 1, 1, "auto"), ls.WRITE),  # removed in Luminar
            ((1, 2, 1, 1, "auto"), ls.PULL),  # changed in digiKam
            ((3, 2, 1, 1, "auto"), ls.CONFLICT),  # changed on both sides
            ((0, 1, 0, 1, "auto"), ls.KEEP),  # old difference
            ((0, 1, None, None, "luminar"), ls.WRITE),
            ((3, 2, 1, 1, "luminar"), ls.WRITE),
            ((0, 1, None, None, "digikam"), ls.PULL),
            ((3, 2, 1, 1, "digikam"), ls.PULL),
            ((2, 2, None, None, "digikam"), ls.NONE),
        ]
        for args, expected in cases:
            with self.subTest(args=args):
                self.assertEqual(ls.decide(*args)[0], expected)

    def test_percent(self):
        self.assertEqual([ls.percent_to_stars(ls.RATING_PERCENT[r]) for r in range(6)], list(range(6)))


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fx = fx.make_all(self.root)
        self.folder = self.fx["folder"]

    def tearDown(self):
        self.tmp.cleanup()

    def run_tool(self, *args, paths=None):
        base = [
            "--catalog", str(self.fx["catalog"]),
            "--digikam-db", str(self.fx["digikam_db"]),
            "--digikamrc", str(self.root / "missing"),
            "--state", str(self.fx["state"]),
            "--exiftool", "exiftool",
        ]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = ls.main([*base, *args, *(paths if paths is not None else [str(self.folder)])])
        return code, out.getvalue() + err.getvalue()

    def snapshot_files(self):
        return {p: p.read_bytes() for p in sorted(self.root.rglob("*")) if p.is_file() and p.suffix != ".db"}

    IMAGE_ID_SQL = (
        "(SELECT i._id_int_64 FROM images i JOIN paths_images pi ON pi._val_id_int_64 = i._id_int_64 "
        " WHERE pi._key_id_int_64 = 11 AND i.path_wide_ch = ?)"
    )

    def set_marker(self, name: str, marker: int):
        con = sqlite3.connect(self.fx["catalog"])
        con.execute(f"UPDATE image_user_attributes SET marker = ? WHERE _out_id_int_64 = {self.IMAGE_ID_SQL}", (marker, name))
        con.commit()
        con.close()

    def luminar_values(self, name: str) -> tuple:
        con = sqlite3.connect(self.fx["catalog"])
        row = con.execute(
            f"SELECT marker, rating FROM image_user_attributes WHERE _out_id_int_64 = {self.IMAGE_ID_SQL}", (name,)
        ).fetchone()
        con.close()
        return row

    def test_load_luminar_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = ls.load_luminar(self.fx["catalog"], tmp)
        live = catalog.live_by_path()
        expected = ls.nfc(str(self.folder / "a_rejected.jpg"))
        self.assertIn(expected, live)  # NFD catalog path -> NFC
        self.assertEqual((live[expected].pick, live[expected].rating), (ls.PICK_REJECTED, 0))  # virtual copy skipped
        self.assertEqual(sum(not e.live for e in catalog.entries), 3)

    def test_dry_run_changes_nothing(self):
        before = self.snapshot_files()
        code, out = self.run_tool()
        self.assertEqual(code, 0, out)
        self.assertEqual(before, self.snapshot_files())
        self.assertFalse(self.fx["state"].exists())
        self.assertIn("a_rejected.jpg  flag: – → Rejected (write: filling a gap); [new sidecar]", out)
        self.assertIn("d_rated.jpg  rating: – → 4★ (write: filling a gap)", out)
        self.assertIn("e_conflict.jpg  flag: digiKam Rejected, Luminar Accepted (CONFLICT", out)
        self.assertNotIn("c_dkstar.jpg", out)  # kept values only with -v
        self.assertNotIn("h_sub.jpg", out)  # not recursive
        self.assertRegex(out, r"sidecars to write:\s+2 \(new: 1\)")
        self.assertRegex(out, r"missing image files:\s+1")

    def test_verbose_and_recursive(self):
        code, out = self.run_tool("-v", "-r")
        self.assertEqual(code, 0, out)
        self.assertIn("c_dkstar.jpg  rating: digiKam 1★, Luminar – (kept: value only in digiKam)", out)
        self.assertIn("h_sub.jpg  flag: – → Rejected", out)

    def test_apply_and_idempotent(self):
        d_sidecar = self.folder / "d_rated.jpg.xmp"
        code, out = self.run_tool("--apply")
        self.assertEqual(code, 0, out)

        new_sidecar = self.folder / "a_rejected.jpg.xmp"
        self.assertEqual(sidecar_state(new_sidecar), (ls.PICK_REJECTED, 0))
        rec = exif_values(new_sidecar, "-XMP-dc:Subject", "-XMP-xmpDM:Pick", "-XMP-xmpDM:Good")
        self.assertEqual(rec["XMP-dc:Subject"], "Keyword")  # the embedded XMP was carried over
        self.assertEqual((rec["XMP-xmpDM:Pick"], str(rec["XMP-xmpDM:Good"])), (-1, "False"))

        self.assertEqual(sidecar_state(d_sidecar), (ls.PICK_NONE, 4))
        rec = exif_values(d_sidecar, "-XMP-digiKam:TagsList", "-XMP-acdsee:Rating", "-XMP-microsoft:RatingPercent")
        self.assertEqual(rec["XMP-digiKam:TagsList"], "Family/Alice")  # other data untouched
        self.assertEqual((rec["XMP-acdsee:Rating"], rec["XMP-microsoft:RatingPercent"]), (4, 75))

        self.assertEqual(sidecar_state(self.folder / "e_conflict.jpg.xmp"), (ls.PICK_REJECTED, 0))  # conflict: not written
        self.assertFalse((self.folder / "c_dkstar.jpg.xmp").exists())
        self.assertTrue(self.fx["state"].exists())

        code, out = self.run_tool()
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"sidecars to write:\s+0 ")
        self.assertRegex(out, r"conflicts \(not written\):\s+1")

    def test_three_way_after_apply(self):
        self.run_tool("--apply")
        self.set_marker("a_rejected.jpg", 0)  # the reject was removed in Luminar
        fx.write_sidecar(self.folder / "d_rated.jpg.xmp", 0, 2)  # re-rated in digiKam
        self.set_marker("b_fav.jpg", 2)  # changed on both sides
        fx.write_sidecar(self.folder / "b_fav.jpg.xmp", 0, 0)

        code, out = self.run_tool("-v")
        self.assertEqual(code, 0, out)
        self.assertIn("a_rejected.jpg  flag: Rejected → – (write: changed in Luminar)", out)
        self.assertIn("d_rated.jpg  rating: Luminar 4★ → 2★ (→ Luminar: changed in digiKam)", out)
        self.assertIn("b_fav.jpg  flag: digiKam –, Luminar Rejected (CONFLICT: changed on both sides)", out)
        self.assertIn("c_dkstar.jpg  rating: digiKam 1★, Luminar – (kept: old difference", out)

        self.run_tool("--apply")  # without --to-luminar Luminar is not touched
        self.assertEqual(sidecar_state(self.folder / "a_rejected.jpg.xmp"), (ls.PICK_NONE, 0))
        self.assertEqual(sidecar_state(self.folder / "d_rated.jpg.xmp"), (ls.PICK_NONE, 2))
        self.assertEqual(self.luminar_values("d_rated.jpg"), (0, 4))

        code, out = self.run_tool("--apply", "--to-luminar")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.luminar_values("d_rated.jpg"), (0, 2))
        code, out = self.run_tool()
        self.assertNotIn("d_rated.jpg", out)  # equal now

    def test_prefer_luminar(self):
        code, out = self.run_tool("--apply", "--prefer", "luminar")
        self.assertEqual(code, 0, out)
        self.assertEqual(sidecar_state(self.folder / "e_conflict.jpg.xmp"), (ls.PICK_ACCEPTED, 0))
        self.assertEqual(sidecar_state(self.folder / "c_dkstar.jpg.xmp"), (ls.PICK_NONE, 0))

    def test_prefer_digikam_writes_catalog_and_undo(self):
        code, out = self.run_tool("--json", "--apply", "--prefer", "digikam")
        self.assertEqual(code, 0, out)
        data = json.loads(out)
        self.assertTrue(data["pulled"])
        self.assertEqual(data["summary"]["write"], 0)
        # digiKam's values went to Luminar (marker: 0 = none, 1 = Favorite, 2 = Rejected)
        self.assertEqual(self.luminar_values("a_rejected.jpg"), (0, 0))
        self.assertEqual(self.luminar_values("c_dkstar.jpg"), (0, 1))
        self.assertEqual(self.luminar_values("d_rated.jpg"), (0, 0))
        self.assertEqual(self.luminar_values("e_conflict.jpg"), (2, 0))
        self.assertEqual(self.luminar_values("b_fav.jpg"), (1, 0))  # was equal, unchanged
        backup = Path(data["backup"])
        self.assertTrue((backup / "catalog" / "catalog.luminarneo").is_file())
        con = sqlite3.connect(self.fx["catalog"])
        self.assertEqual(con.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        con.close()

        code, out = self.run_tool()
        self.assertRegex(out, r"equal:\s+5")

        # undo only restores what has not been changed in Luminar since
        self.set_marker("e_conflict.jpg", 1)
        code, out = self.run_tool("--json", "--undo", str(backup), paths=[])
        self.assertEqual(json.loads(out)["undo"]["luminar_restored"], 3)
        self.assertEqual(json.loads(out)["undo"]["luminar_skipped"], 1)
        self.assertEqual(self.luminar_values("a_rejected.jpg"), (2, 0))
        self.assertEqual(self.luminar_values("d_rated.jpg"), (0, 4))
        self.assertEqual(self.luminar_values("e_conflict.jpg"), (1, 0))

    def test_luminar_running_blocks_write(self):
        before = self.fx["catalog"].read_bytes()
        with mock.patch.object(ls, "process_running", lambda name: name == "Luminar"):
            code, out = self.run_tool("--apply", "--prefer", "digikam")
        self.assertEqual(code, 1)
        self.assertIn("Luminar is running", out)
        self.assertEqual(before, self.fx["catalog"].read_bytes())
        self.assertFalse((self.folder / "a_rejected.jpg.xmp").exists())  # the sidecars were not touched either

    def test_stale_digikam_overwrite_is_not_pulled(self):
        self.run_tool("--apply")  # a_rejected: Rejected goes into the sidecar, digiKam's database has no flag yet
        sidecar = self.folder / "a_rejected.jpg.xmp"
        # edited in digiKam without a re-read: the sidecar goes back to the database's old value
        fx.write_sidecar(sidecar, 0, 0)
        code, out = self.run_tool()
        self.assertIn("a_rejected.jpg  flag: digiKam –, Luminar Rejected (CONFLICT: digiKam may have overwritten it", out)
        code, out = self.run_tool("--apply", "--to-luminar")
        self.assertEqual(self.luminar_values("a_rejected.jpg"), (2, 0))  # Luminar's value was kept

        # a real digiKam change: after a re-read the user set it to Accepted
        con = sqlite3.connect(self.fx["digikam_db"])
        con.execute("INSERT INTO ImageTags VALUES (1, 21)")
        con.commit()
        con.close()
        fx.write_sidecar(sidecar, 3, 0)
        code, out = self.run_tool()
        self.assertIn("a_rejected.jpg  flag: Luminar Rejected → Accepted (→ Luminar: changed in digiKam)", out)

    def add_digikam_data(self, sql: str, params: tuple = ()):
        con = sqlite3.connect(self.fx["digikam_db"])
        con.execute(sql, params)
        con.commit()
        con.close()

    def test_check_digikam(self):
        # digiKam ids in the fixture: a=1, b=2, c=3, d=4, e=5
        self.add_digikam_data("INSERT INTO ImageTags VALUES (2, 101)")  # b: Family/Alice – also in the sidecar
        self.add_digikam_data("INSERT INTO ImageTags VALUES (5, 102)")  # e: Sports – only in the database
        self.add_digikam_data("INSERT INTO ImageComments (imageid, type, comment) VALUES (1, 1, 'Nice picture')")
        self.add_digikam_data("INSERT INTO ImageComments (imageid, type, comment) VALUES (4, 1, 'Old caption')")
        self.add_digikam_data("INSERT INTO ImageTags VALUES (4, 9)")  # d: red color label
        subprocess.run(["exiftool", "-q", "-m", "-overwrite_original", "-XMP-dc:Description=New caption",
                        "-XMP-xmp:Label=Green", str(self.folder / "d_rated.jpg.xmp")], check=True)
        before = self.snapshot_files()

        code, out = self.run_tool("--check-digikam")
        self.assertEqual(code, 0, out)
        self.assertEqual(before, self.snapshot_files())  # read-only
        self.assertIn("d_rated.jpg  color label: database “Red”, file “Green” (would be overwritten); "
                      "caption: database “Old caption”, file “New caption” (would be overwritten)", out)
        self.assertIn("a_rejected.jpg  caption: “Nice picture” (only in the database)", out)
        self.assertIn("c_dkstar.jpg  rating: “1★” (only in the database)", out)
        self.assertIn("e_conflict.jpg  tag: “Sports” (only in the database)", out)
        self.assertNotIn("b_fav.jpg", out)
        self.assertRegex(out, r"would be overwritten:\s+1 images")
        self.assertRegex(out, r"only in the database:\s+3 images")
        self.assertIn("Write Metadata to Files", out)

        code, out = self.run_tool("--check-digikam", "--json")
        data = json.loads(out)["check"]
        self.assertEqual(data["summary"], {"images": 5, "overwritten": 1, "db_only": 3, "unchecked": 0})

    def test_check_digikam_skips_cloud_only(self):
        with mock.patch.object(ls, "is_cloud_only", lambda path: path.endswith("c_dkstar.jpg")):
            code, out = self.run_tool("--check-digikam", "-v")
        self.assertEqual(code, 0, out)
        self.assertIn("c_dkstar.jpg  not checked (cloud only)", out)
        self.assertRegex(out, r"not checked \(cloud only\):\s+1 images")
        self.assertRegex(out, r"only in the database:\s+0 images")

    def test_running_digikam_blocks_only_sidecar_writes(self):
        with mock.patch.object(ls, "process_running", lambda name: name == "digikam"):
            code, out = self.run_tool("--apply")
            self.assertEqual(code, 1)
            self.assertIn("digiKam is running", out)
            code, out = self.run_tool("--apply", "--prefer", "digikam")  # writes Luminar only
            self.assertEqual(code, 0, out)
        self.assertEqual(self.luminar_values("e_conflict.jpg"), (2, 0))

    def test_stale_catalog_aborts(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog = ls.load_luminar(self.fx["catalog"], tmp)
        path = ls.nfc(str(self.folder / "d_rated.jpg"))
        item = ls.Item(path, path + ".xmp", True, {"pick": 0, "rating": 4}, {"pick": 0, "rating": 2},
                       {"pick": (ls.NONE, ""), "rating": (ls.PULL, "")})
        self.set_marker("d_rated.jpg", 1)  # changed in Luminar after it was read
        with self.assertRaises(ls.SyncError):
            ls.write_luminar(catalog, [item], self.root / "backup")
        self.assertEqual(self.luminar_values("d_rated.jpg"), (1, 4))  # nothing was written

    def test_undo(self):
        d_sidecar = self.folder / "d_rated.jpg.xmp"
        original = d_sidecar.read_bytes()
        self.run_tool("--apply")
        backups = list((self.fx["state"].parent / "backups").iterdir())
        self.assertEqual(len(backups), 1)
        code, out = self.run_tool("--undo", str(backups[0]), paths=[])
        self.assertEqual(code, 0, out)
        self.assertEqual(d_sidecar.read_bytes(), original)
        self.assertFalse((self.folder / "a_rejected.jpg.xmp").exists())
        self.assertFalse(self.fx["state"].exists())

    def test_single_file_and_unknown_file(self):
        code, out = self.run_tool(paths=[str(self.folder / "d_rated.jpg"), str(self.folder / "d_rated.jpg.xmp")])
        self.assertEqual(code, 0, out)
        self.assertIn("d_rated.jpg  rating", out)
        self.assertNotIn("a_rejected.jpg", out)
        self.assertIn("not in the Luminar catalog:", out)

    def test_json_dry_run(self):
        code, out = self.run_tool("--json")
        self.assertEqual(code, 0, out)
        data = json.loads(out)
        self.assertFalse(data["applied"])
        self.assertEqual(data["summary"]["write"], 2)
        items = {os.path.basename(i["image"]): i for i in data["items"]}
        self.assertEqual(items["a_rejected.jpg"]["luminar"], {"pick": ls.PICK_REJECTED, "rating": 0})
        self.assertEqual(items["a_rejected.jpg"]["decisions"]["pick"]["action"], ls.WRITE)
        self.assertEqual(items["e_conflict.jpg"]["decisions"]["pick"]["action"], ls.CONFLICT)
        self.assertFalse(items["a_rejected.jpg"]["has_sidecar"])

    def test_files_from_apply_json(self):
        listing = self.root / "list.txt"
        listing.write_text(f"{self.folder / 'e_conflict.jpg'}\n\n{self.folder / 'c_dkstar.jpg'}\n", encoding="utf-8")
        code, out = self.run_tool("--json", "--apply", "--prefer", "luminar", "--files-from", str(listing), paths=[])
        self.assertEqual(code, 0, out)
        data = json.loads(out)
        self.assertTrue(data["applied"])
        self.assertEqual(data["summary"]["selected"], 2)
        self.assertEqual(data["failed"], [])
        self.assertTrue(Path(data["backup"], "manifest.json").is_file())
        self.assertEqual(sidecar_state(self.folder / "e_conflict.jpg.xmp"), (ls.PICK_ACCEPTED, 0))
        self.assertFalse((self.folder / "a_rejected.jpg.xmp").exists())  # was not on the list

        code, out = self.run_tool("--json", "--undo", data["backup"], paths=[])
        self.assertEqual(
            json.loads(out), {"undo": {"restored": 1, "deleted": 1, "luminar_restored": 0, "luminar_skipped": 0}}
        )

    def test_json_error(self):
        code, out = self.run_tool("--json", paths=[str(self.root / "no-such-file")])
        self.assertEqual(code, 1)
        self.assertIn("does not exist", json.loads(out.splitlines()[0])["error"])

    def test_overview(self):
        code, out = self.run_tool("--overview", "--json", paths=[])
        self.assertEqual(code, 0, out)
        data = json.loads(out)
        self.assertEqual(data["roots"], [ls.nfc(str(self.root / "Pictures/Photos"))])
        folders = {f["path"]: f for f in data["folders"]}
        main = folders[ls.nfc(str(self.folder))]
        # differing from digiKam's database: a, c, d, e and the missing f; b is equal
        self.assertEqual((main["images"], main["flagged"], main["rated"], main["differs"], main["reread"]), (6, 4, 1, 5, 0))
        self.assertEqual(folders[ls.nfc(str(self.folder / "Sub"))]["differs"], 1)

        # after writing, digiKam's database still shows the old values: a and d are awaiting a re-read,
        # c and e remain differences
        self.run_tool("--apply")
        code, out = self.run_tool("--overview", "--json", paths=[])
        main = {f["path"]: f for f in json.loads(out)["folders"]}[ls.nfc(str(self.folder))]
        self.assertEqual((main["differs"], main["reread"]), (3, 2))

        # d's rating was changed in digiKam: that is a difference now, not an image awaiting a re-read
        con = sqlite3.connect(self.fx["digikam_db"])
        con.execute("UPDATE ImageInformation SET rating = 2 WHERE imageid = 4")
        con.commit()
        con.close()
        code, out = self.run_tool("--overview", "--json", paths=[])
        main = {f["path"]: f for f in json.loads(out)["folders"]}[ls.nfc(str(self.folder))]
        self.assertEqual((main["differs"], main["reread"]), (4, 1))

    def test_top_roots(self):
        self.assertEqual(ls.top_roots(["/a/b", "/a", "/ab", "/a/b/c", "/x"]), ["/a", "/ab", "/x"])

    def test_backup_dirs_unique(self):
        state = self.fx["state"]
        first = ls.new_backup_dir(state)
        first.mkdir(parents=True)
        self.assertNotEqual(first, ls.new_backup_dir(state))

    def test_schema_guard(self):
        con = sqlite3.connect(self.fx["catalog"])
        con.execute("PRAGMA user_version = 154")
        con.commit()
        con.close()
        code, out = self.run_tool()
        self.assertEqual(code, 1)
        self.assertIn("unknown Luminar catalog version", out)

    def test_audit(self):
        code, out = self.run_tool("--audit", paths=[])
        self.assertEqual(code, 0, out)
        self.assertIn("live (in a Luminar folder): 7, duplicate paths: 0", out)
        self.assertIn("missing on disk: 1", out)
        self.assertIn("orphaned (in no Luminar folder): 3", out)
        self.assertIn("with a live counterpart in the catalog: 2", out)
        self.assertIn("of which the counterpart's flag/rating differs: 1", out)
        self.assertIn("found nowhere: 1", out)


if __name__ == "__main__":
    unittest.main()
