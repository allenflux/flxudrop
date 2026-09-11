from __future__ import annotations

import http.client
import json
import socket
import tempfile
import threading
import unittest
from unittest import mock
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
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.thread.start()
        self.host, self.port = self.server.server_address

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.tempdir.cleanup()

    def request(self, method: str, path: str, body: bytes = b"", headers: dict[str, str] | None = None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request(method, path, body=body, headers=headers or {})
        return conn.getresponse()

    def raw_request(self, method: str, headers: list[tuple[str, str]], body: bytes = b""):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        self.addCleanup(conn.close)
        conn.putrequest(method, "/upload")
        for name, value in headers:
            conn.putheader(name, value)
        conn.endheaders(body)
        conn.sock.shutdown(socket.SHUT_WR)
        return conn.getresponse()

    def assert_storage_empty(self) -> None:
        self.assertEqual(list(self.config.files_dir.iterdir()), [])
        self.assertEqual(list(self.config.meta_dir.iterdir()), [])
        self.assertEqual(list(self.config.storage_dir.glob(".upload-*.tmp")), [])

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

    def test_multipart_upload_returns_downloadable_file(self) -> None:
        payload = bytes(range(256)) * 1200 + b"\r\n--test-boundary-invalid\x00\xff"
        body = (
            b'--test-boundary\r\nContent-Disposition: form-data; name="description"\r\n\r\n'
            b'an ignored field\r\n--test-boundary\r\n'
            b'Content-Disposition: form-data; name="file"; filename="large data.bin"\r\n'
            b'Content-Type: application/octet-stream\r\n\r\n'
            + payload
            + b'\r\n--test-boundary\r\nContent-Disposition: form-data; name="extra"\r\n\r\n'
            b'another ignored field\r\n--test-boundary--\r\n'
        )
        response = self.request(
            "POST", "/upload", body,
            {"Content-Type": 'Multipart/Form-Data; boundary="test-boundary"'},
        )
        self.assertEqual(response.status, 201)
        data = json.loads(response.read())
        self.assertEqual(data["filename"], "large data.bin")
        self.assertEqual(data["size"], len(payload))
        download = self.request("GET", f'/f/{data["file_id"]}/large%20data.bin')
        self.assertEqual(download.read(), payload)
        self.assertEqual(list(self.config.storage_dir.glob(".upload-*.tmp")), [])

    def test_multipart_without_filename_infers_extension(self) -> None:
        body = (
            b'--b\r\nContent-Disposition: form-data; name="file"\r\n'
            b'Content-Type: application/octet-stream\r\n\r\n'
            b'2026-09-11 09:13:52 INFO example\n\r\n--b--\r\n'
        )
        response = self.request("POST", "/upload", body, {"Content-Type": "multipart/form-data; boundary=b"})
        self.assertEqual(response.status, 201)
        self.assertEqual(json.loads(response.read())["filename"], "upload.log")

    def test_growing_multipart_upload_returns_guidance_without_storing_partial_file(self) -> None:
        boundary = b"------------------------curl-boundary"
        headers = (
            b"--" + boundary
            + b'\r\nContent-Disposition: form-data; name="file"; filename="active.log"\r\n'
            b'Content-Type: application/octet-stream\r\n\r\n'
        )
        original = b"original log line\n" * 8192
        closing = b"\r\n--" + boundary + b"--\r\n"
        declared_length = len(headers) + len(original) + len(closing)
        # curl -F calculates Content-Length before reading the file. Appended
        # lines can displace the closing boundary beyond that declared length.
        grown_body = headers + original + b"appended log line\n" * 100 + closing
        response = self.raw_request(
            "POST",
            [("Content-Length", str(declared_length)),
             ("Content-Type", "multipart/form-data; boundary=" + boundary.decode())],
            grown_body[:declared_length],
        )
        self.assertEqual(response.status, 400)
        result = json.loads(response.read())
        self.assertFalse(result["ok"])
        self.assertIn("closing boundary", result["error"])
        self.assertIn("curl -T", result["error"])
        self.assert_storage_empty()

    def test_put_upload_keeps_initial_length_when_file_grows(self) -> None:
        original = b"original log line\n" * 8192
        response = self.raw_request(
            "PUT",
            [("Content-Length", str(len(original))), ("X-Filename", "active.log")],
            original + b"appended log line\n" * 100,
        )
        self.assertEqual(response.status, 201)
        result = json.loads(response.read())
        self.assertEqual(result["filename"], "active.log")
        self.assertEqual(result["size"], len(original))
        download = self.request("GET", f'/f/{result["file_id"]}/active.log')
        self.assertEqual(download.status, 200)
        self.assertEqual(download.read(), original)
        self.assertEqual(list(self.config.storage_dir.glob(".upload-*.tmp")), [])

    def test_malformed_upload_lengths_are_rejected_without_files(self) -> None:
        cases = [
            ([], 411),
            ([("Content-Length", "-1")], 400),
            ([("Content-Length", "+1")], 400),
            ([("Content-Length", "abc")], 400),
            ([("Content-Length", "1"), ("Content-Length", "2")], 400),
            ([("Content-Length", str(self.config.max_upload_bytes + 1))], 413),
            ([("Transfer-Encoding", "chunked")], 501),
            ([("Transfer-Encoding", "chunked"), ("Content-Length", "0")], 501),
        ]
        for method in ("PUT", "POST"):
            for headers, expected in cases:
                with self.subTest(method=method, headers=headers):
                    response = self.raw_request(method, headers)
                    self.assertEqual(response.status, expected)
                    self.assertFalse(json.loads(response.read())["ok"])
                    self.assert_storage_empty()

    def test_multipart_limit_includes_envelope(self) -> None:
        self.config.max_upload_bytes = 5
        response = self.request(
            "POST", "/upload", b"123456",
            {"Content-Type": "multipart/form-data; boundary=b"},
        )
        self.assertEqual(response.status, 413)
        response.read()
        self.assert_storage_empty()

    def test_content_length_allows_http_whitespace(self) -> None:
        response = self.raw_request("PUT", [("Content-Length", "4 \t")], b"data")
        self.assertEqual(response.status, 201)
        self.assertEqual(json.loads(response.read())["size"], 4)

    def test_truncated_uploads_are_rejected_and_cleaned_up(self) -> None:
        multipart_body = (
            b'--b\r\nContent-Disposition: form-data; name="file"; filename="test.txt"\r\n\r\n'
            b'partial file\r\n--b--\r\n'
        )
        for method, body, extra_headers in (
            ("PUT", b"partial file", []),
            ("POST", multipart_body, [("Content-Type", "multipart/form-data; boundary=b")]),
        ):
            with self.subTest(method=method):
                response = self.raw_request(
                    method, [("Content-Length", str(len(body) + 10)), *extra_headers], body
                )
                self.assertEqual(response.status, 400)
                response.read()
                self.assert_storage_empty()

    def test_malformed_multipart_is_rejected_and_cleaned_up(self) -> None:
        cases = [
            ("multipart/form-data", b"anything"),
            ("multipart/form-data; boundary=b", b'--b\r\nContent-Disposition: form-data; name="file"\r\n\r\nunfinished'),
            ("multipart/form-data; boundary=b", b'--b\r\nContent-Disposition: form-data; name="other"\r\n\r\nignored\r\n--b--\r\n'),
        ]
        for content_type, body in cases:
            with self.subTest(body=body):
                response = self.request("POST", "/upload", body, {"Content-Type": content_type})
                self.assertEqual(response.status, 400)
                response.read()
                self.assert_storage_empty()

    def test_storage_failure_returns_server_error_and_cleans_upload(self) -> None:
        with mock.patch("app.save_metadata", side_effect=OSError("Disk full")):
            response = self.request("PUT", "/upload/test.txt", b"some data")
            self.assertEqual(response.status, 500)
            self.assertEqual(json.loads(response.read())["error"], "Could not store upload")
        self.assert_storage_empty()

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
