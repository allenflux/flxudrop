from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from app import FluxDropConfig, infer_extension_from_sample, make_handler, sanitize_filename


class FluxDropTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.config = FluxDropConfig(
            storage_dir=Path(self.tempdir.name),
            public_base_url=None,
            upload_token=None,
            max_upload_bytes=1024 * 1024,
        )
        self.config.ensure_dirs()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.config))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host, self.port = self.server.server_address

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.tempdir.cleanup()

    def request(self, method: str, path: str, body: bytes = b"", headers: dict[str, str] | None = None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        return conn.getresponse()

    def test_put_upload_returns_downloadable_link(self) -> None:
        payload = b"2026-07-03 11:20:01 INFO hello from fluxdrop\n"
        response = self.request(
            "PUT",
            "/upload",
            payload,
            {"Content-Length": str(len(payload))},
        )

        self.assertEqual(response.status, 201)
        data = json.loads(response.read())
        self.assertTrue(data["ok"])
        self.assertEqual(data["filename"], "upload.log")
        self.assertEqual(data["size"], len(payload))

        download_path = "/" + data["download_url"].split("/", 3)[3]
        download = self.request("GET", download_path)
        self.assertEqual(download.status, 200)
        self.assertEqual(download.read(), payload)

    def test_put_upload_can_take_filename_from_header(self) -> None:
        payload = b"named file\n"
        response = self.request(
            "PUT",
            "/upload",
            payload,
            {
                "Content-Length": str(len(payload)),
                "X-Filename": "example.txt",
            },
        )

        self.assertEqual(response.status, 201)
        data = json.loads(response.read())
        self.assertEqual(data["filename"], "example.txt")

    def test_token_is_required_when_configured(self) -> None:
        self.config.upload_token = "secret"
        payload = b"secret data"
        response = self.request(
            "PUT",
            "/upload/secret.txt",
            payload,
            {"Content-Length": str(len(payload))},
        )

        self.assertEqual(response.status, 401)

        authed = self.request(
            "PUT",
            "/upload/secret.txt",
            payload,
            {
                "Content-Length": str(len(payload)),
                "Authorization": "Bearer secret",
            },
        )
        self.assertEqual(authed.status, 201)

    def test_filename_is_sanitized(self) -> None:
        self.assertEqual(sanitize_filename("../weird/name?.txt"), "name_.txt")
        self.assertEqual(sanitize_filename(""), "upload.bin")

    def test_extension_is_inferred_from_content(self) -> None:
        self.assertEqual(infer_extension_from_sample(b'{"ok": true}\n'), "json")
        self.assertEqual(infer_extension_from_sample(b"col_a,col_b\n1,2\n"), "csv")
        self.assertEqual(infer_extension_from_sample(b"2026-07-03 11:00:00 ERROR failed\n"), "log")
        self.assertEqual(infer_extension_from_sample(b"%PDF-1.7\n"), "pdf")


if __name__ == "__main__":
    unittest.main()
