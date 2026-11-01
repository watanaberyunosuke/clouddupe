import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import clouddupe as cd  # noqa: E402

DROPBOX_ITEMS = [
    {"Path": "Photos/beach.jpg", "Name": "beach.jpg", "Size": 2048, "ModTime": "2024-01-01T00:00:00Z",
     "IsDir": False, "Hashes": {"dropbox": "aaa111"}},
    {"Path": "Backup/beach (1).jpg", "Name": "beach (1).jpg", "Size": 2048, "ModTime": "2024-02-01T00:00:00Z",
     "IsDir": False, "Hashes": {"dropbox": "AAA111"}},
    {"Path": "Docs/report.pdf", "Name": "report.pdf", "Size": 5000, "ModTime": "2024-01-01T00:00:00Z",
     "IsDir": False, "Hashes": {"dropbox": "bbb222"}},
    {"Path": "Docs/notes.txt", "Name": "notes.txt", "Size": 300, "ModTime": "2024-01-01T00:00:00Z",
     "IsDir": False, "Hashes": {"dropbox": "ccc333"}},
    {"Path": "Docs", "Name": "Docs", "Size": -1, "IsDir": True},
    {"Path": "empty.txt", "Name": "empty.txt", "Size": 0, "IsDir": False, "Hashes": {"dropbox": "e3b0"}},
    {"Path": "empty2.txt", "Name": "empty2.txt", "Size": 0, "IsDir": False, "Hashes": {"dropbox": "e3b0"}},
]

PIKPAK_ITEMS = [
    # Same name and size as Dropbox report.pdf; PikPak has no dropbox hash -> probable
    {"Path": "Inbox/Report.pdf", "Name": "Report.pdf", "Size": 5000, "ModTime": "2024-03-01T00:00:00Z",
     "IsDir": False, "Hashes": {"md5": "m1"}},
    # Empty MD5 (documented PikPak limitation) -> must be treated as no hash
    {"Path": "Inbox/notes.txt", "Name": "notes.txt", "Size": 300, "ModTime": "2024-03-01T00:00:00Z",
     "IsDir": False, "Hashes": {"md5": ""}},
    {"Path": "Movies/film.mkv", "Name": "film.mkv", "Size": 900_000_000, "ModTime": "2024-03-01T00:00:00Z",
     "IsDir": False, "Hashes": {"md5": "f00d"}},
    # Same name and size as film.mkv but different MD5 -> proven different
    {"Path": "Movies/old/film.mkv", "Name": "film.mkv", "Size": 900_000_000,
     "ModTime": "2024-03-01T00:00:00Z", "IsDir": False, "Hashes": {"md5": "beef"}},
]

GDRIVE_ITEMS = [
    # MD5 matches PikPak film.mkv -> cross-provider confirmed
    {"Path": "Media/film-final.mkv", "Name": "film-final.mkv", "Size": 900_000_000,
     "ModTime": "2024-04-01T00:00:00Z", "IsDir": False,
     "Hashes": {"MD5": "F00D", "SHA-1": "s1", "SHA-256": "s256"}},
]


def build(sources):
    files = []
    for remote, items in sources:
        for item in items:
            entry = cd.parse_item(item, remote, len(files))
            if entry is not None:
                files.append(entry)
    return files


def paths(group):
    return sorted(m.full_path for m in group.members)


class NormalisationTests(unittest.TestCase):
    def test_hash_type_names_across_rclone_versions(self):
        self.assertEqual(cd.normalise_hash_type("SHA-1"), "sha1")
        self.assertEqual(cd.normalise_hash_type("DropboxHash"), "dropbox")
        self.assertEqual(cd.normalise_hash_type("md5"), "md5")
        self.assertEqual(cd.normalise_hash_type("quickxor"), "quickxor")

    def test_copy_suffixes_stripped(self):
        self.assertEqual(cd.normalise_name("Beach (1).JPG"), "beach.jpg")
        self.assertEqual(cd.normalise_name("report - Copy.pdf"), "report.pdf")
        self.assertEqual(cd.normalise_name("Copy of notes.txt"), "notes.txt")
        self.assertEqual(cd.normalise_name("README"), "readme")
        self.assertEqual(cd.normalise_name(".bashrc"), ".bashrc")

    def test_parse_size(self):
        self.assertEqual(cd.parse_size("0"), 0)
        self.assertEqual(cd.parse_size("50M"), 50 * 1024**2)
        self.assertEqual(cd.parse_size("1.5G"), int(1.5 * 1024**3))


class DetectionTests(unittest.TestCase):
    def setUp(self):
        self.files = build([("dropbox:", DROPBOX_ITEMS), ("pikpak:", PIKPAK_ITEMS),
                            ("gdrive:", GDRIVE_ITEMS)])
        self.groups = cd.detect(self.files, min_size=1, size_only=False, size_only_min=0)

    def by_conf(self, conf):
        return [g for g in self.groups if g.confidence == conf]

    def test_directories_and_empty_hashes_skipped(self):
        self.assertEqual(len(self.files), 11)
        notes = next(f for f in self.files if f.full_path == "pikpak:Inbox/notes.txt")
        self.assertEqual(notes.hashes, {})

    def test_same_provider_hash_match_ignores_name(self):
        self.assertIn(["dropbox:Backup/beach (1).jpg", "dropbox:Photos/beach.jpg"],
                      [paths(g) for g in self.by_conf("confirmed")])

    def test_cross_provider_md5_match(self):
        confirmed = [paths(g) for g in self.by_conf("confirmed")]
        self.assertIn(["gdrive:Media/film-final.mkv", "pikpak:Movies/film.mkv"], confirmed)

    def test_same_name_different_hash_not_grouped(self):
        for g in self.groups:
            self.assertFalse({"pikpak:Movies/film.mkv", "pikpak:Movies/old/film.mkv"}
                             <= set(paths(g)), g)

    def test_no_common_hash_is_probable_only(self):
        probable = [paths(g) for g in self.by_conf("probable")]
        self.assertIn(["dropbox:Docs/report.pdf", "pikpak:Inbox/Report.pdf"], probable)
        self.assertIn(["dropbox:Docs/notes.txt", "pikpak:Inbox/notes.txt"], probable)

    def test_empty_files_ignored_by_default(self):
        for g in self.groups:
            self.assertGreater(g.size, 0)

    def test_size_only_mode(self):
        files = build([("a:", [{"Path": "x.bin", "Size": 60 * 1024**2, "Hashes": {"dropbox": "1"}}]),
                       ("b:", [{"Path": "y.bin", "Size": 60 * 1024**2, "Hashes": {"md5": "2"}}])])
        self.assertEqual(cd.detect(files, 1, False, 0), [])
        groups = cd.detect(files, 1, True, cd.parse_size("50M"))
        self.assertEqual([g.confidence for g in groups], ["possible"])

    def test_wasted_bytes(self):
        g = next(g for g in self.groups if "dropbox:Photos/beach.jpg" in paths(g))
        self.assertEqual(g.wasted_bytes, 2048)

    def test_comparability_summary(self):
        summary = cd.summarise(self.files, self.groups, [])
        pairs = {(p["a"], p["b"]): p["common_hash_types"] for p in summary["comparability"]}
        self.assertEqual(pairs[("dropbox:", "pikpak:")], [])
        self.assertEqual(pairs[("gdrive:", "pikpak:")], ["md5"])


FAKE_RCLONE = textwrap.dedent("""\
    #!{python}
    import json, os, sys
    remote = sys.argv[-1]
    if "lsjson" not in sys.argv or "--hash" not in sys.argv:
        sys.exit(9)
    fixtures = json.load(open(os.environ["FAKE_RCLONE_FIXTURES"]))
    if remote not in fixtures:
        sys.stderr.write("Failed to lsjson: directory not found\\n")
        sys.exit(3)
    items = fixtures[remote]
    print("[")
    for i, item in enumerate(items):
        print(json.dumps(item) + ("," if i < len(items) - 1 else ""))
    print("]")
""")


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        fixtures = self.tmp / "fixtures.json"
        fixtures.write_text(json.dumps({"dropbox:": DROPBOX_ITEMS, "pikpak:": PIKPAK_ITEMS,
                                        "gdrive:": GDRIVE_ITEMS}))
        os.environ["FAKE_RCLONE_FIXTURES"] = str(fixtures)
        self.rclone = self.tmp / "rclone"
        self.rclone.write_text(FAKE_RCLONE.format(python=sys.executable))
        self.rclone.chmod(self.rclone.stat().st_mode | stat.S_IEXEC)

    def test_scan_writes_reports_and_listings(self):
        out = self.tmp / "out"
        code = cd.main(["scan", "dropbox:", "pikpak:", "gdrive:", "--rclone", str(self.rclone),
                        "--out", str(out)])
        self.assertEqual(code, 0)
        report = json.loads((out / "report.json").read_text())
        self.assertEqual(report["summary"]["total_files"], 11)
        self.assertIn("confirmed", report["summary"]["by_confidence"])
        self.assertTrue((out / "duplicates.csv").exists())
        self.assertIn("Cloud Duplicate Report", (out / "report.html").read_text())
        self.assertEqual(len(list(out.glob("listing-*.json"))), 3)

        out2 = self.tmp / "out2"
        code = cd.main(["analyse", *map(str, sorted(out.glob("listing-*.json"))), "--out", str(out2)])
        self.assertEqual(code, 0)
        report2 = json.loads((out2 / "report.json").read_text())
        self.assertEqual(len(report2["groups"]), len(report["groups"]))

    def test_failed_remote_reported_and_others_continue(self):
        out = self.tmp / "out"
        code = cd.main(["scan", "dropbox:", "missing:", "--rclone", str(self.rclone),
                        "--out", str(out)])
        self.assertEqual(code, 1)
        report = json.loads((out / "report.json").read_text())
        self.assertEqual(report["summary"]["total_files"], 6)
        self.assertTrue(any("missing:" in e for e in report["summary"]["errors"]))


if __name__ == "__main__":
    unittest.main()
