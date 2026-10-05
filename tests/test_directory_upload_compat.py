from __future__ import annotations

import http.client
import io
import json
import tempfile
import threading
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from app import FluxDropConfig, make_handler


def make_archive(root):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{root}/empty/", b"")
        archive.writestr(f"{root}/sub/data.txt", b"arbitrary folder content")
    return output.getvalue()


def make_multipart(parts):
    data = bytearray()
    for name, content in parts:
        data.extend(b'--compat-boundary\r\nContent-Disposition: form-data; name="file"; filename="')
        data.extend(name.encode("utf-8"))
        data.extend(b'"\r\nContent-Type: application/octet-stream\r\n\r\n')
        data.extend(content)
        data.extend(b"\r\n")
    data.extend(b"--compat-boundary--\r\n")
    return bytes(data)


class SameEndpointDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = FluxDropConfig(Path(self.temp.name), None, None, 1024 * 1024)
        self.config.ensure_dirs()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.config))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.temp.cleanup()

    def request(self, method, body=b"", headers=None, path="/upload"):
        parts = urlsplit(path)
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            conn.request(method, parts.path + ("?" + parts.query if parts.query else ""), body, headers or {})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def form_headers(self, mode=None):
        headers = {"Content-Type": "multipart/form-data; boundary=compat-boundary"}
        if mode is not None:
            headers["X-FluxDrop-Directory"] = mode
        return headers

    def test_multipart_zip_uses_arbitrary_archive_root_and_original_upload_endpoint(self):
        for root in ("customer backups", "任意目录", "logs-2026"):
            raw = make_archive(root)
            status, payload = self.request("POST", make_multipart([("temporary-name.zip", raw)]), self.form_headers("zip"))
            self.assertEqual(status, 201, payload)
            stored = json.loads(payload)
            self.assertEqual((stored["kind"], stored["filename"], stored["file_count"]), ("directory", root, 1))
            self.assertEqual(stored["download_filename"], root + ".zip")
            status, downloaded = self.request("GET", path=stored["download_url"])
            self.assertEqual(status, 200)
            self.assertEqual(downloaded, raw)
            status, listing = self.request("GET", path=stored["browse_url"])
            self.assertEqual(status, 200)
            self.assertEqual({entry["filename"] for entry in json.loads(listing)["entries"]}, {"empty", "sub"})

    def test_raw_put_zip_supports_same_upload_endpoint(self):
        status, payload = self.request("PUT", make_archive("reports"), {"X-FluxDrop-Directory": "zip"})
        self.assertEqual(status, 201, payload)
        self.assertEqual(json.loads(payload)["filename"], "reports")
        self.assertEqual(json.loads(payload)["kind"], "directory")

    def test_browser_file_parts_support_same_upload_endpoint(self):
        status, payload = self.request("POST", make_multipart([
            ("项目资料/空目录/", b""), ("项目资料/层级/文件.txt", b"hello"),
        ]), self.form_headers("files"))
        self.assertEqual(status, 201, payload)
        stored = json.loads(payload)
        self.assertEqual((stored["kind"], stored["filename"]), ("directory", "项目资料"))

    def test_unmarked_multipart_zip_remains_an_ordinary_file(self):
        raw = make_archive("archive-root")
        status, payload = self.request("POST", make_multipart([("original.zip", raw)]), self.form_headers())
        self.assertEqual(status, 201, payload)
        stored = json.loads(payload)
        self.assertEqual((stored["kind"], stored["filename"]), ("file", "original.zip"))
        self.assertEqual(self.request("GET", path=stored["download_url"])[1], raw)

    def test_unmarked_multipart_keeps_first_file_and_basename_semantics(self):
        status, payload = self.request("POST", make_multipart([
            ("any-root/one.txt", b"first"), ("any-root/two.txt", b"second"),
        ]), self.form_headers())
        self.assertEqual(status, 201, payload)
        stored = json.loads(payload)
        self.assertEqual((stored["kind"], stored["filename"]), ("file", "one.txt"))
        self.assertEqual(self.request("GET", path=stored["download_url"])[1], b"first")

    def test_invalid_directory_marker_or_zip_cannot_leave_partial_uploads(self):
        for mode, body in (("invalid", b"x"), ("zip", b"not a zip")):
            status, payload = self.request("POST", make_multipart([("a.zip", body)]), self.form_headers(mode))
            self.assertEqual(status, 400, payload)
            self.assertEqual(list(self.config.files_dir.iterdir()), [])
            self.assertEqual(list(self.config.meta_dir.iterdir()), [])
            self.assertEqual(list(self.config.storage_dir.glob(".upload-*.tmp")), [])

    def test_marked_directory_upload_retains_authentication(self):
        self.config.upload_token = "secret"
        body = make_multipart([("any.zip", make_archive("data"))])
        headers = self.form_headers("zip")
        self.assertEqual(self.request("POST", body, headers)[0], 401)
        headers["Authorization"] = "Bearer secret"
        self.assertEqual(self.request("POST", body, headers)[0], 201)


if __name__ == "__main__":
    unittest.main()
