from __future__ import annotations

import http.client
import json
import secrets
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import app
from app import FluxDropConfig, make_handler


class ManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.config = FluxDropConfig(Path(self.tempdir.name), "https://public.example", "test-secret", 1024 * 1024)
        self.config.ensure_dirs()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.config))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.headers = {"Authorization": "Bearer test-secret"}

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.tempdir.cleanup()

    def request(self, method, path, body=b"", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        self.addCleanup(conn.close)
        conn.request(method, path, body=body, headers=headers or {})
        return conn.getresponse()

    def upload(self, filename="example.txt", payload=b"file content"):
        response = self.request("PUT", f"/upload/{filename}", payload, self.headers)
        self.assertEqual(response.status, 201)
        return json.loads(response.read())

    def listing(self):
        response = self.request("GET", "/api/files", headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        return json.loads(response.read())["files"]

    def test_homepage_and_assets_are_served_without_exposing_token(self) -> None:
        for path, content_type in (("/", "text/html"), ("/static/app.css", "text/css"), ("/static/app.js", "text/javascript"), ("/static/favicon.svg", "image/svg+xml")):
            with self.subTest(path=path):
                response = self.request("GET", path)
                self.assertEqual(response.status, 200)
                self.assertIn(content_type, response.getheader("Content-Type"))
                self.assertIn("frame-ancestors 'none'", response.getheader("Content-Security-Policy"))
                body = response.read()
                self.assertNotIn(b"test-secret", body)
                head = self.request("HEAD", path)
                self.assertEqual(head.status, 200)
                self.assertEqual(int(head.getheader("Content-Length")), len(body))
                self.assertEqual(head.read(), b"")
        for path in ("/static/../app.py", "/static/%2e%2e/app.py", "/static/index.html", "/app.py"):
            response = self.request("GET", path)
            self.assertEqual(response.status, 404)
            response.read()

    def test_upload_list_download_and_delete_work_without_token(self) -> None:
        self.config.upload_token = None
        upload = self.request("PUT", "/upload/file.log", b"example log\n")
        self.assertEqual(upload.status, 201)
        stored = json.loads(upload.read())
        for _ in range(2):
            response = self.request("GET", "/api/files")
            self.assertEqual(response.status, 200)
            files = json.loads(response.read())["files"]
            self.assertEqual([file["file_id"] for file in files], [stored["file_id"]])
        download = self.request("GET", files[0]["download_url"])
        self.assertEqual(download.status, 200)
        self.assertEqual(download.read(), b"example log\n")
        response = self.request("DELETE", f'/api/files/{stored["file_id"]}')
        self.assertEqual(response.status, 200)
        self.assertTrue(json.loads(response.read())["ok"])
        self.assertFalse((self.config.files_dir / stored["file_id"]).exists())
        self.assertFalse((self.config.meta_dir / f'{stored["file_id"]}.json').exists())
        listing = self.request("GET", "/api/files")
        self.assertEqual(json.loads(listing.read())["files"], [])

    def test_empty_token_also_allows_management(self) -> None:
        self.config.upload_token = ""
        response = self.request("GET", "/api/files")
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.read())["files"], [])

    def test_wrong_or_missing_token_cannot_list_or_delete(self) -> None:
        stored = self.upload()
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Cookie": "token=test-secret"}):
            for method, path in (("GET", "/api/files"), ("DELETE", f'/api/files/{stored["file_id"]}')):
                with self.subTest(headers=headers, method=method):
                    response = self.request(method, path, headers=headers)
                    self.assertEqual(response.status, 401)
                    self.assertEqual(response.getheader("Cache-Control"), "no-store")
                    self.assertFalse(json.loads(response.read())["ok"])
        self.assertEqual(len(self.listing()), 1)

    def test_both_token_headers_are_accepted(self) -> None:
        for headers in (self.headers, {"X-Upload-Token": "test-secret"}):
            response = self.request("GET", "/api/files", headers=headers)
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), {"ok": True, "files": []})

    def test_listing_preserves_duplicate_names_and_sorts_newest_first(self) -> None:
        first = self.upload("same.txt", b"one")
        second = self.upload("same.txt", b"second")
        path = self.config.meta_dir / f'{first["file_id"]}.json'
        metadata = json.loads(path.read_text())
        metadata["created_at"] = 1
        path.write_text(json.dumps(metadata))
        files = self.listing()
        self.assertEqual([item["file_id"] for item in files], [second["file_id"], first["file_id"]])
        self.assertEqual([item["size"] for item in files], [6, 3])
        self.assertTrue(all(item["filename"] == "same.txt" for item in files))
        self.assertTrue(all(item["download_url"].startswith("/f/") for item in files))
        download = self.request("GET", files[0]["download_url"])
        self.assertEqual(download.read(), b"second")

    def test_listing_skips_corrupt_incomplete_and_stale_records(self) -> None:
        good = self.upload()
        invalid_records = [
            b"{", b"\xff", b"[]", b'{"file_id": "wrong"}',
            {"filename": []}, {"filename": "\ud800"}, {"size": "12"}, {"size": -1}, {"size": True},
            {"created_at": None}, {"created_at": 10**50}, {"file_id": "mismatch"},
        ]
        for invalid in invalid_records:
            file_id = secrets.token_urlsafe(16)
            metadata = {"file_id": file_id, "filename": "invalid.txt", "size": 0, "created_at": 1}
            if isinstance(invalid, dict):
                metadata.update(invalid)
                invalid = json.dumps(metadata).encode()
            (self.config.meta_dir / f"{file_id}.json").write_bytes(invalid)
            (self.config.files_dir / file_id).write_bytes(b"")
        stale = self.upload("stale.txt")
        (self.config.files_dir / stale["file_id"]).unlink()
        (self.config.meta_dir / ".partial.tmp").write_text("{")
        self.assertEqual([item["file_id"] for item in self.listing()], [good["file_id"]])

    def test_delete_removes_data_and_metadata_and_invalidates_download(self) -> None:
        stored = self.upload()
        file_id = stored["file_id"]
        response = self.request("DELETE", f"/api/files/{file_id}", headers=self.headers)
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.read()), {"ok": True, "file_id": file_id})
        self.assertFalse((self.config.files_dir / file_id).exists())
        self.assertFalse((self.config.meta_dir / f"{file_id}.json").exists())
        self.assertEqual(self.listing(), [])
        for method, path in (("GET", f"/f/{file_id}/example.txt"), ("DELETE", f"/api/files/{file_id}")):
            response = self.request(method, path, headers=self.headers)
            self.assertEqual(response.status, 404)
            response.read()
        head = self.request("HEAD", f"/f/{file_id}/example.txt")
        self.assertEqual(head.status, 404)
        self.assertEqual(head.read(), b"")

    def test_delete_rejects_invalid_paths_and_unknown_ids(self) -> None:
        stored = self.upload()
        sentinel = self.config.storage_dir / "sentinel.txt"
        sentinel.write_text("keep")
        for suffix in ("", "..", "../../sentinel.txt", "%2e%2e%2fsentinel.txt", stored["file_id"] + "/extra", "x" * 15, "x" * 65, "x" * 22):
            with self.subTest(suffix=suffix):
                response = self.request("DELETE", f"/api/files/{suffix}", headers=self.headers)
                self.assertEqual(response.status, 404)
                response.read()
        self.assertEqual(sentinel.read_text(), "keep")
        self.assertEqual(len(self.listing()), 1)

    def test_failed_data_delete_keeps_file_and_metadata(self) -> None:
        stored = self.upload()
        blob = self.config.files_dir / stored["file_id"]
        unlink = Path.unlink

        def fail_blob(path, *args, **kwargs):
            if path == blob:
                raise PermissionError("cannot unlink")
            return unlink(path, *args, **kwargs)

        with mock.patch("app.Path.unlink", autospec=True, side_effect=fail_blob):
            response = self.request("DELETE", f'/api/files/{stored["file_id"]}', headers=self.headers)
            self.assertEqual(response.status, 500)
            response.read()
        self.assertTrue(blob.is_file())
        self.assertEqual(len(self.listing()), 1)

    def test_partial_delete_can_be_retried(self) -> None:
        stored = self.upload()
        file_id = stored["file_id"]
        metadata = self.config.meta_dir / f"{file_id}.json"
        unlink = Path.unlink

        def fail_metadata(path, *args, **kwargs):
            if path == metadata:
                raise PermissionError("cannot unlink metadata")
            return unlink(path, *args, **kwargs)

        with mock.patch("app.Path.unlink", autospec=True, side_effect=fail_metadata):
            response = self.request("DELETE", f"/api/files/{file_id}", headers=self.headers)
            self.assertEqual(response.status, 500)
            response.read()
        self.assertFalse((self.config.files_dir / file_id).exists())
        self.assertTrue(metadata.exists())
        response = self.request("DELETE", f"/api/files/{file_id}", headers=self.headers)
        self.assertEqual(response.status, 200)
        response.read()
        self.assertFalse(metadata.exists())

    def test_download_deleted_before_open_returns_404(self) -> None:
        stored = self.upload()
        load = app.load_metadata

        def remove_after_metadata(config, file_id):
            result = load(config, file_id)
            (config.files_dir / file_id).unlink()
            return result

        with mock.patch("app.load_metadata", side_effect=remove_after_metadata):
            response = self.request("GET", f'/f/{stored["file_id"]}/example.txt')
            self.assertEqual(response.status, 404)
            response.read()

    def test_open_download_completes_when_file_is_deleted(self) -> None:
        payload = bytes(range(256)) * 1024
        stored = self.upload(payload=payload)
        opened = threading.Event()
        resume = threading.Event()
        copy = app.shutil.copyfileobj

        def wait_then_copy(source, target, **kwargs):
            opened.set()
            if not resume.wait(3):
                raise TimeoutError("test deletion did not complete")
            return copy(source, target, **kwargs)

        with mock.patch("app.shutil.copyfileobj", side_effect=wait_then_copy):
            try:
                download = self.request("GET", f'/f/{stored["file_id"]}/example.txt')
                self.assertTrue(opened.wait(2))
                deletion = self.request("DELETE", f'/api/files/{stored["file_id"]}', headers=self.headers)
                self.assertEqual(deletion.status, 200)
                deletion.read()
            finally:
                resume.set()
            self.assertEqual(download.status, 200)
            self.assertEqual(download.read(), payload)


if __name__ == "__main__":
    unittest.main()
