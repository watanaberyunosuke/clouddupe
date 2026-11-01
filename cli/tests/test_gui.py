"""End-to-end tests for the web GUI against a real rclone binary.

Skipped unless rclone is on PATH or CLOUDDUPE_TEST_RCLONE points to one.
Uses a throwaway rclone config, so the user's real drives are never touched.
"""
import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gui  # noqa: E402

RCLONE = os.environ.get("CLOUDDUPE_TEST_RCLONE") or shutil.which("rclone")


@unittest.skipIf(RCLONE is None, "rclone not available")
class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        os.environ["RCLONE_CONFIG"] = str(cls.tmp / "rclone.conf")
        data = cls.tmp / "data"
        (data / "a").mkdir(parents=True)
        (data / "b").mkdir()
        (data / "a" / "holiday.jpg").write_bytes(b"same bytes" * 100)
        (data / "b" / "renamed copy.jpg").write_bytes(b"same bytes" * 100)
        (data / "b" / "other.txt").write_bytes(b"different")
        cls.data = data

        cls.token = "test-token"
        cls.port_ref = [0]
        cls.api = gui.Api(gui.Rclone(RCLONE))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), gui.make_handler(cls.api, cls.token, cls.port_ref))
        cls.port_ref[0] = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        os.environ.pop("RCLONE_CONFIG", None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def request(self, method, path, body=None, token=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port_ref[0], timeout=60)
        headers = {"Host": host or f"127.0.0.1:{self.port_ref[0]}",
                   "X-Clouddupe-Token": self.token if token is None else token}
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=headers)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        return res.status, data

    def call(self, name, body=None):
        status, data = self.request("POST", f"/api/{name}", body or {})
        payload = json.loads(data)
        if status != 200:
            raise AssertionError(f"{name}: {payload.get('error')}")
        return payload["result"]

    def test_1_security(self):
        self.assertEqual(self.request("POST", "/api/list_remotes", {}, token="wrong")[0], 403)
        self.assertEqual(self.request("POST", "/api/list_remotes", {}, host="evil.example:80")[0], 403)
        self.assertEqual(self.request("GET", "/", host="evil.example")[0], 403)
        self.assertEqual(self.request("GET", "/../clouddupe.py")[0], 404)
        status, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"<div id=\"app\">", body)

    def test_2_providers_and_version(self):
        self.assertTrue(self.call("rclone_version").startswith("rclone v"))
        names = {p["name"] for p in self.call("list_providers")}
        self.assertTrue({"dropbox", "pikpak", "drive"} <= names)
        self.assertNotIn("crypt", names)

    def test_3_password_is_obscured_and_failed_create_is_cleaned(self):
        step = self.call("config_start", {"name": "pp", "provider": "pikpak"})
        self.assertEqual(step["question"]["name"], "user")
        step = self.call("config_answer", {"name": "pp", "stepState": step["state"],
                                           "result": "nobody", "isPassword": False})
        self.assertTrue(step["question"]["isPassword"])
        # The final step logs in to PikPak; the fake account must be rejected
        # with rclone's message passed through.
        status, data = self.request("POST", "/api/config_answer", {
            "name": "pp", "stepState": step["state"], "result": "plain-secret", "isPassword": True})
        self.assertEqual(status, 400)
        self.assertIn("invalid_account_or_password", json.loads(data)["error"])
        conf = Path(os.environ["RCLONE_CONFIG"]).read_text()
        self.assertIn("pass = ", conf)
        self.assertNotIn("plain-secret", conf)
        self.call("delete_remote", {"name": "pp"})
        self.assertNotIn("pp", [r["name"] for r in self.call("list_remotes")])

        status, data = self.request("POST", "/api/config_start", {"name": "-bad", "provider": "local"})
        self.assertEqual(status, 400)

    def test_4_wizard_scan_export(self):
        step = self.call("config_start", {"name": "disk", "provider": "local"})
        guard = 0
        while not step["done"]:
            q = step["question"]
            step = self.call("config_answer", {"name": "disk", "stepState": step["state"],
                                               "result": q["default"], "isPassword": False})
            guard += 1
            self.assertLess(guard, 20)
        self.assertIn("disk", [r["name"] for r in self.call("list_remotes")])

        self.call("start_scan", {"request": {"targets": [f"disk:{self.data}"], "minSize": 1,
                                             "sizeOnly": False, "sizeOnlyMin": 0}})
        for _ in range(100):
            poll = self.call("scan_poll", {"since": 0})
            if poll["done"]:
                break
            time.sleep(0.2)
        self.assertIsNone(poll["error"])
        groups = poll["result"]["groups"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["confidence"], "confirmed")
        self.assertEqual(sorted(m["path"] for m in groups[0]["members"]),
                         ["a/holiday.jpg", "b/renamed copy.jpg"])
        self.assertTrue(poll["events"][-1]["done"])

        for fmt, marker in (("html", b"Cloud Duplicate Report"), ("csv", b"group_id"), ("json", b"\"groups\"")):
            status, body = self.request("GET", f"/api/export?format={fmt}")
            self.assertEqual(status, 200, fmt)
            self.assertIn(marker, body)
        self.assertEqual(self.request("GET", "/api/export?format=html", token="x")[0], 403)


if __name__ == "__main__":
    unittest.main()
