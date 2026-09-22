from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import quote, urlparse

import app
from app import FluxDropConfig, make_handler


class PreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.config = FluxDropConfig(Path(self.tempdir.name), "https://public.example", "secret", 4 * 1024 * 1024)
        self.config.ensure_dirs()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.config))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

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

    def upload(self, filename, payload):
        response = self.request("PUT", "/upload/" + quote(filename), payload, {"Authorization": "Bearer secret"})
        self.assertEqual(response.status, 201)
        return json.loads(response.read())

    def assert_preview_headers(self, response):
        self.assertEqual(response.getheader("X-Content-Type-Options"), "nosniff")
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertIn("sandbox", response.getheader("Content-Security-Policy"))
        self.assertIn("default-src 'none'", response.getheader("Content-Security-Policy"))

    def test_upload_and_listing_advertise_previews_for_existing_files(self):
        cases = [
            ("notes.txt", b"hello", "text"),
            ("photo.PNG", b"\x89PNG\r\n\x1a\n", "image"),
            ("report.pdf", b"%PDF-1.7\n", "pdf"),
            ("recording.mp3", b"ID3\x00", "audio"),
            ("movie.mp4", b"\x00\x00\x00\x18ftypmp42", "video"),
            ("backup.zip", b"PK\x03\x04\x00\x00", "unsupported"),
        ]
        uploaded = [self.upload(name, payload) for name, payload, _ in cases]
        for stored, (_, _, expected_type) in zip(uploaded, cases):
            self.assertEqual(stored["preview_type"], expected_type)
            self.assertEqual(stored["preview_url"], f'/p/{stored["file_id"]}/{quote(stored["filename"], safe="")}')
            # Preview metadata is derived, so previously uploaded records also work.
            metadata = json.loads((self.config.meta_dir / f'{stored["file_id"]}.json').read_text())
            self.assertNotIn("preview_type", metadata)
        response = self.request("GET", "/api/files", headers={"Authorization": "Bearer secret"})
        self.assertEqual(response.status, 200)
        listed = {stored["file_id"]: stored for stored in json.loads(response.read())["files"]}
        for stored in uploaded:
            self.assertEqual(listed[stored["file_id"]]["preview_type"], stored["preview_type"])
            self.assertEqual(listed[stored["file_id"]]["preview_url"], stored["preview_url"])

    def test_active_content_is_plain_text_and_download_stays_attachment(self):
        for filename in ("page.html", "vector.svg", "code.js", "markup.xml", "PAGE.XHTML"):
            with self.subTest(filename=filename):
                payload = b'<script>fetch("/api/files")</script><svg onload="alert(1)"></svg>'
                stored = self.upload(filename, payload)
                self.assertEqual(stored["preview_type"], "text")
                response = self.request("GET", stored["preview_url"])
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Content-Type"), "text/plain; charset=utf-8")
                self.assertTrue(response.getheader("Content-Disposition").startswith("inline;"))
                self.assert_preview_headers(response)
                self.assertEqual(response.read(), payload)
                download = self.request("GET", urlparse(stored["download_url"]).path)
                self.assertEqual(download.status, 200)
                self.assertTrue(download.getheader("Content-Disposition").startswith("attachment;"))
                self.assertEqual(download.read(), payload)

    def test_unknown_utf8_text_is_detected_without_splitting_unicode(self):
        payload = ("你好，在线预览 👋 👩‍💻\n" * 1000).encode("utf-8")
        stored = self.upload("clipboard", payload)
        self.assertEqual(stored["preview_type"], "text")
        response = self.request("GET", stored["preview_url"])
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("X-Preview-Truncated"), "false")
        self.assertEqual(response.read().decode("utf-8"), payload.decode("utf-8"))
        boundary = b"a" * (app.PREVIEW_SAMPLE_BYTES - 1) + "你好".encode("utf-8")
        self.assertEqual(self.upload("sample-boundary.unknown", boundary)["preview_type"], "text")

    def test_text_is_bounded_and_head_matches_without_truncating_download(self):
        payload = b"a" * (app.MAX_TEXT_PREVIEW_BYTES - 1) + "你好".encode("utf-8")
        stored = self.upload("large.txt", payload)
        response = self.request("GET", stored["preview_url"])
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("X-Preview-Truncated"), "true")
        body = response.read()
        self.assertLessEqual(len(body), app.MAX_TEXT_PREVIEW_BYTES)
        self.assertEqual(body.decode("utf-8"), "a" * (app.MAX_TEXT_PREVIEW_BYTES - 1))
        head = self.request("HEAD", stored["preview_url"])
        self.assertEqual(head.status, response.status)
        for name in ("Content-Length", "Content-Type", "Content-Disposition", "X-Preview-Truncated", "Content-Security-Policy"):
            self.assertEqual(head.getheader(name), response.getheader(name))
        self.assertEqual(head.read(), b"")
        download = self.request("GET", urlparse(stored["download_url"]).path)
        self.assertEqual(download.read(), payload)

    def test_empty_and_non_utf8_known_text_remain_readable(self):
        for payload, expected in ((b"", b""), (b"caf\xe9\n", "caf�\n".encode("utf-8"))):
            stored = self.upload("notes.txt", payload)
            response = self.request("GET", stored["preview_url"])
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), expected)

    def test_binary_unsupported_and_missing_previews_return_json_for_get_and_head(self):
        stored = self.upload("binary.bin", bytes(range(256)))
        self.assertEqual(stored["preview_type"], "unsupported")
        cases = [(stored["preview_url"], 415)]
        cases += [(path, 404) for path in ("/p/", "/p/../outside", "/p/%2e%2e%2foutside", "/p/" + "x" * 22)]
        for path, status in cases:
            with self.subTest(path=path):
                response = self.request("GET", path)
                self.assertEqual(response.status, status)
                self.assert_preview_headers(response)
                self.assertFalse(json.loads(response.read())["ok"])
                head = self.request("HEAD", path)
                self.assertEqual(head.status, status)
                self.assertEqual(head.getheader("Content-Length"), response.getheader("Content-Length"))
                self.assertEqual(head.read(), b"")
        (self.config.files_dir / stored["file_id"]).unlink()
        response = self.request("GET", stored["preview_url"])
        self.assertEqual(response.status, 404)
        response.read()

    def test_media_supports_closed_open_suffix_and_clamped_ranges(self):
        payload = bytes(range(256))
        stored = self.upload("movie.mp4", payload)
        response = self.request("GET", stored["preview_url"])
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Content-Type"), "video/mp4")
        self.assertEqual(response.getheader("Accept-Ranges"), "bytes")
        self.assert_preview_headers(response)
        self.assertEqual(response.read(), payload)
        for value, start, end in (("bytes=3-11", 3, 11), ("bytes=200-", 200, 255), ("bytes=-7", 249, 255), ("bytes=250-999", 250, 255), ("bytes=-999", 0, 255)):
            with self.subTest(value=value):
                response = self.request("GET", stored["preview_url"], headers={"Range": value})
                self.assertEqual(response.status, 206)
                self.assertEqual(response.getheader("Content-Range"), f"bytes {start}-{end}/{len(payload)}")
                self.assertEqual(int(response.getheader("Content-Length")), end - start + 1)
                self.assertEqual(response.read(), payload[start:end + 1])
                head = self.request("HEAD", stored["preview_url"], headers={"Range": value})
                self.assertEqual(head.status, 206)
                self.assertEqual(head.getheader("Content-Range"), response.getheader("Content-Range"))
                self.assertEqual(head.getheader("Content-Length"), response.getheader("Content-Length"))
                self.assertEqual(head.read(), b"")

    def test_invalid_unsatisfiable_and_multiple_ranges_are_rejected(self):
        stored = self.upload("audio.mp3", b"0123456789")
        for value in ("bytes=10-", "bytes=8-2", "bytes=-0", "bytes=-", "bytes=a-b", "bytes=0-1,3-4", "items=0-1", "bytes=" + "9" * 5000 + "-"):
            with self.subTest(value=value[:40]):
                response = self.request("GET", stored["preview_url"], headers={"Range": value})
                self.assertEqual(response.status, 416)
                self.assertEqual(response.getheader("Content-Range"), "bytes */10")
                self.assertFalse(json.loads(response.read())["ok"])
                head = self.request("HEAD", stored["preview_url"], headers={"Range": value})
                self.assertEqual(head.status, 416)
                self.assertEqual(head.read(), b"")
        empty = self.upload("empty.mp4", b"")
        response = self.request("GET", empty["preview_url"], headers={"Range": "bytes=0-"})
        self.assertEqual(response.status, 416)
        self.assertEqual(response.getheader("Content-Range"), "bytes */0")
        response.read()

    def test_streaming_is_bounded_and_survives_concurrent_deletion(self):
        payload = bytes(range(256)) * (app.CHUNK_SIZE // 128)
        stored = self.upload("movie.mp4", payload)
        blob = self.config.files_dir / stored["file_id"]
        original_open = Path.open
        read_sizes = []

        class TrackedReader:
            def __init__(self, source):
                self.source = source

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.source.close()

            def fileno(self):
                return self.source.fileno()

            def seek(self, *args):
                return self.source.seek(*args)

            def read(self, size=-1):
                if not read_sizes:
                    blob.unlink()
                read_sizes.append(size)
                return self.source.read(size)

        def tracked_open(path, *args, **kwargs):
            source = original_open(path, *args, **kwargs)
            return TrackedReader(source) if path == blob else source

        with mock.patch("app.Path.open", autospec=True, side_effect=tracked_open):
            response = self.request("GET", stored["preview_url"])
            self.assertEqual(response.status, 200)
            self.assertEqual(response.read(), payload)
        self.assertTrue(all(0 < size <= app.CHUNK_SIZE for size in read_sizes))
        self.assertGreaterEqual(len(read_sizes), 3)
        self.assertFalse(blob.exists())


if __name__ == "__main__":
    unittest.main()
