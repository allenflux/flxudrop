from __future__ import annotations

import http.client
import io
import json
import stat
import tempfile
import threading
import unittest
import warnings
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode, urlsplit

from app import FluxDropConfig, make_handler


def make_zip(entries, compression=zipfile.ZIP_STORED):
    output = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(output, "w", compression=compression) as archive:
            for name, payload in entries:
                archive.writestr(name, payload)
    return output.getvalue()


def multipart(entries):
    body = bytearray()
    for name, payload in entries:
        body.extend(b'--directory-boundary\r\nContent-Disposition: form-data; name="file"; filename="')
        body.extend(name.encode("utf-8"))
        body.extend(b'"\r\nContent-Type: application/octet-stream\r\n\r\n')
        body.extend(payload)
        body.extend(b"\r\n")
    body.extend(b"--directory-boundary--\r\n")
    return bytes(body)


class DirectoryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.config = FluxDropConfig(Path(self.tempdir.name), None, None, 1024 * 1024)
        self.config.ensure_dirs()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.config))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.tempdir.cleanup()

    def request(self, method, path, body=b"", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        self.addCleanup(conn.close)
        parts = urlsplit(path)
        conn.request(method, parts.path + ("?" + parts.query if parts.query else ""), body, headers or {})
        response = conn.getresponse()
        payload = response.read()
        return response, payload

    def upload(self, entries=None, form=False, headers=None):
        entries = entries if entries is not None else [
            ("result_stats/", b""), ("result_stats/empty/", b""),
            ("result_stats/sub/results.json", b'{"ok": true}\n'),
            ("result_stats/readme.txt", b"hello folder\n"),
        ]
        headers = dict(headers or {})
        if form:
            headers["Content-Type"] = "multipart/form-data; boundary=directory-boundary"
        response, payload = self.request("POST" if form else "PUT", "/upload-directory", multipart(entries) if form else make_zip(entries), headers)
        self.assertEqual(response.status, 201, payload)
        return json.loads(payload)

    def browse(self, file_id, path="", headers=None):
        response, payload = self.request("GET", f"/api/directories/{file_id}?" + urlencode({"path": path}), headers=headers)
        self.assertEqual(response.status, 200, payload)
        return json.loads(payload)["entries"]

    def assert_no_uploads(self):
        self.assertEqual(list(self.config.files_dir.iterdir()), [])
        self.assertEqual(list(self.config.meta_dir.iterdir()), [])
        self.assertEqual(sorted(path.name for path in self.config.storage_dir.iterdir()), ["files", "meta"])

    def test_zip_and_multipart_preserve_tree_and_download_content(self):
        for form in (False, True):
            with self.subTest(form=form):
                stored = self.upload(form=form)
                self.assertEqual(stored["kind"], "directory")
                self.assertEqual(stored["filename"], "result_stats")
                self.assertEqual(stored["download_filename"], "result_stats.zip")
                self.assertEqual(stored["file_count"], 2)
                root = {entry["filename"]: entry for entry in self.browse(stored["file_id"])}
                self.assertEqual(set(root), {"empty", "sub", "readme.txt"})
                self.assertEqual(root["empty"]["kind"], "directory")
                self.assertEqual(self.browse(stored["file_id"], "empty"), [])
                child = self.browse(stored["file_id"], "sub")[0]
                self.assertEqual(child["filename"], "results.json")
                self.assertEqual(child["path"], "sub/results.json")
                self.assertEqual(child["preview_type"], "text")
                response, payload = self.request("GET", child["preview_url"])
                self.assertEqual(response.status, 200)
                self.assertEqual(payload, b'{"ok": true}\n')
                response, payload = self.request("GET", root["readme.txt"]["download_url"])
                self.assertEqual(response.status, 200)
                self.assertEqual(payload, b"hello folder\n")
                response, payload = self.request("GET", stored["download_url"])
                self.assertEqual(response.status, 200)
                self.assertIn("result_stats.zip", response.getheader("Content-Disposition"))
                with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                    self.assertIn("result_stats/empty/", archive.namelist())
                    self.assertEqual(archive.read("result_stats/sub/results.json"), b'{"ok": true}\n')
                response, payload = self.request("GET", "/api/files")
                listed = next(item for item in json.loads(payload)["files"] if item["file_id"] == stored["file_id"])
                self.assertEqual(listed["kind"], "directory")

    def test_unicode_and_url_metacharacters_survive_folder_browsing(self):
        name = "报告/子目录/100% #?+.txt"
        for form in (False, True):
            stored = self.upload([(name, "你好".encode())], form=form)
            self.assertEqual(stored["filename"], "报告")
            child = self.browse(stored["file_id"], "子目录")[0]
            self.assertEqual(child["filename"], "100% #?+.txt")
            response, payload = self.request("GET", child["download_url"])
            self.assertEqual(response.status, 200)
            self.assertEqual(payload.decode(), "你好")

    def test_empty_root_directory(self):
        for form in (False, True):
            stored = self.upload([("empty/", b"")], form=form)
            self.assertEqual(stored["file_count"], 0)
            self.assertEqual(self.browse(stored["file_id"]), [])

    def test_child_preview_limits_and_media_ranges(self):
        stored = self.upload([
            ("root/long.txt", b"x" * (300 * 1024)),
            ("root/test.mp4", b"0123456789"),
            ("root/unsafe.html", b"<script>alert(1)</script>"),
        ])
        entries = {entry["filename"]: entry for entry in self.browse(stored["file_id"])}
        response, payload = self.request("GET", entries["long.txt"]["preview_url"])
        self.assertEqual(response.status, 200)
        self.assertEqual(len(payload), 256 * 1024)
        self.assertEqual(response.getheader("X-Preview-Truncated"), "true")
        response, payload = self.request("GET", entries["test.mp4"]["preview_url"], headers={"Range": "bytes=2-5"})
        self.assertEqual(response.status, 206)
        self.assertEqual(payload, b"2345")
        response, payload = self.request("HEAD", entries["test.mp4"]["preview_url"])
        self.assertEqual(response.status, 200)
        self.assertEqual(payload, b"")
        self.assertEqual(response.getheader("Content-Length"), "10")
        response, payload = self.request("GET", entries["unsafe.html"]["preview_url"])
        self.assertIn("text/plain", response.getheader("Content-Type"))
        self.assertIn("sandbox", response.getheader("Content-Security-Policy"))

    def test_auth_delete_and_legacy_uploads(self):
        self.config.upload_token = "secret"
        headers = {"Authorization": "Bearer secret"}
        response, _ = self.request("PUT", "/upload-directory", make_zip([("root/", b"")]))
        self.assertEqual(response.status, 401)
        stored = self.upload(headers=headers)
        response, _ = self.request("GET", f'/api/directories/{stored["file_id"]}')
        self.assertEqual(response.status, 401)
        files = [entry for entry in self.browse(stored["file_id"], headers=headers) if entry["kind"] == "file"]
        child_url = files[0]["download_url"]
        self.assertEqual(self.request("GET", child_url)[0].status, 200)
        self.assertEqual(self.request("DELETE", f'/api/files/{stored["file_id"]}', headers=headers)[0].status, 200)
        self.assertEqual(self.request("GET", child_url)[0].status, 404)
        self.assertEqual(self.request("GET", stored["download_url"])[0].status, 404)
        self.assert_no_uploads()
        response, raw = self.request("PUT", "/upload/legacy.txt", b"still works", headers)
        self.assertEqual(response.status, 201)
        legacy = json.loads(raw)
        metadata = self.config.meta_dir / f'{legacy["file_id"]}.json'
        old = json.loads(metadata.read_text())
        metadata.write_text(json.dumps({key: old[key] for key in ("file_id", "filename", "size", "created_at")}))
        response, raw = self.request("GET", "/api/files", headers=headers)
        self.assertEqual(json.loads(raw)["files"][0]["filename"], "legacy.txt")
        self.assertEqual(self.request("GET", legacy["download_url"])[1], b"still works")

    def test_invalid_archive_paths_duplicates_and_special_files_are_atomic(self):
        invalid = [
            [("../outside.txt", b"x")], [("/root/file.txt", b"x")],
            [("root/../escape", b"x")], [("root\\escape", b"x")],
            [("C:/file.txt", b"x")], [("root/a", b"x"), ("other/a", b"x")],
            [("root/a", b"x"), ("root/a", b"y")],
            [("root/a", b"x"), ("root/a/child", b"y")],
            [("root/a/child", b"x"), ("root/a", b"y")],
            [("root/empty/", b"nonempty")], [],
        ]
        link = zipfile.ZipInfo("root/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        invalid.append([(link, b"../../secret")])
        for entries in invalid:
            with self.subTest(entries=str(entries)[:100]):
                response, payload = self.request("PUT", "/upload-directory", make_zip(entries))
                self.assertEqual(response.status, 400, payload)
                self.assert_no_uploads()

    def test_multipart_bad_paths_and_truncation_are_atomic(self):
        for entries in ([('root/../escape', b"x")], [("root/file", b"a"), ("root/file", b"b")]):
            response, payload = self.request("POST", "/upload-directory", multipart(entries), {"Content-Type": "multipart/form-data; boundary=directory-boundary"})
            self.assertEqual(response.status, 400, payload)
            self.assert_no_uploads()
        response, _ = self.request("POST", "/upload-directory", multipart([("root/a", b"abc")])[:-30], {"Content-Type": "multipart/form-data; boundary=directory-boundary"})
        self.assertEqual(response.status, 400)
        self.assert_no_uploads()

    def test_expanded_size_limit_and_corrupt_zip(self):
        self.config.max_upload_bytes = 1024
        response, _ = self.request("PUT", "/upload-directory", make_zip([("root/big", b"x" * 2048)], zipfile.ZIP_DEFLATED))
        self.assertEqual(response.status, 413)
        self.assert_no_uploads()
        for raw in (b"not a ZIP", make_zip([("root/a", b"abc")])[:-30]):
            response, _ = self.request("PUT", "/upload-directory", raw)
            self.assertEqual(response.status, 400)
            self.assert_no_uploads()

    def test_failed_storage_commit_removes_entire_folder(self):
        with mock.patch("app.save_metadata", side_effect=OSError("Disk full")):
            response, _ = self.request("PUT", "/upload-directory", make_zip([("root/a", b"abc")]))
        self.assertEqual(response.status, 500)
        self.assert_no_uploads()

    def test_browse_and_child_paths_cannot_escape_folder(self):
        stored = self.upload()
        for path in ("../", "/readme.txt", "sub/../../readme.txt", "sub\\results.json", "missing"):
            for prefix in (f'/api/directories/{stored["file_id"]}', f'/f/{stored["file_id"]}/child', f'/p/{stored["file_id"]}/child'):
                response, _ = self.request("GET", prefix + "?" + urlencode({"path": path}))
                self.assertIn(response.status, (400, 404), path)

    def test_bulk_download_includes_named_folder_zip(self):
        stored = self.upload()
        response, payload = self.request("GET", "/api/files/download?" + urlencode({"file_id": stored["file_id"]}))
        self.assertEqual(response.status, 200)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertEqual(archive.namelist(), ["result_stats.zip"])
            with zipfile.ZipFile(io.BytesIO(archive.read("result_stats.zip"))) as folder:
                self.assertEqual(folder.read("result_stats/readme.txt"), b"hello folder\n")


if __name__ == "__main__":
    unittest.main()
