from __future__ import annotations

import http.client
import errno
import io
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
import zipfile
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from app import FluxDropConfig, make_handler


class DirectoryStreamTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = FluxDropConfig(self.root / "storage", None, None, 1024 * 1024)
        self.config.ensure_dirs()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.config))
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.temporary.cleanup()

    def request(self, method, path, body=b"", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        self.addCleanup(conn.close)
        url = urlsplit(path)
        conn.request(method, url.path + ("?" + url.query if url.query else ""), body, headers or {})
        response = conn.getresponse()
        return response.status, response.read()

    def raw_chunks(self, body, extra_headers=(), path="/upload-directory"):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        self.addCleanup(conn.close)
        conn.putrequest("PUT", path)
        conn.putheader("Transfer-Encoding", "chunked")
        for key, value in extra_headers:
            conn.putheader(key, value)
        conn.endheaders(body)
        try:
            conn.sock.shutdown(socket.SHUT_WR)
        except OSError as exc:
            # Invalid framing may be rejected before this client half-closes.
            # Keep reading the response; unrelated socket errors must still fail.
            if exc.errno not in {errno.ENOTCONN, errno.ECONNRESET, errno.EPIPE}:
                raise
        response = conn.getresponse()
        return response.status, response.read()

    def assert_empty_storage(self):
        self.assertEqual(list(self.config.files_dir.iterdir()), [])
        self.assertEqual(list(self.config.meta_dir.iterdir()), [])
        self.assertEqual(sorted(path.name for path in self.config.storage_dir.iterdir()), ["files", "meta"])

    @unittest.skipUnless(shutil.which("curl") and shutil.which("tar"), "Native tar and curl are needed")
    def test_actual_tar_curl_pipeline_uploads_arbitrary_folder_and_original_form_still_works(self):
        folder = self.root / "任意名称 with spaces.log"
        (folder / "nested" / "empty").mkdir(parents=True)
        (folder / "nested" / "数据.json").write_text('{"ok": true}\n')
        (folder / ".hidden").write_text("hidden content")
        base = f"http://127.0.0.1:{self.server.server_port}"
        with subprocess.Popen(
            ["tar", "-C", str(folder.parent), "-czf", "-", "--", folder.name],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, "COPYFILE_DISABLE": "1"},
        ) as pack:
            uploaded = subprocess.run(
                ["curl", "--fail", "--silent", "--show-error", "--max-time", "10", "-T", "-", base + "/upload-directory"],
                stdin=pack.stdout, capture_output=True, timeout=15,
            )
            pack.stdout.close()
            pack.wait(timeout=5)
            self.assertEqual(pack.returncode, 0, pack.stderr.read().decode())
        self.assertEqual(uploaded.returncode, 0, uploaded.stderr.decode() + uploaded.stdout.decode())
        stored = json.loads(uploaded.stdout)
        self.assertEqual(stored["kind"], "directory")
        self.assertEqual(stored["filename"], folder.name)
        status, payload = self.request("GET", stored["download_url"])
        self.assertEqual(status, 200)
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertEqual(archive.read(folder.name + "/nested/数据.json"), b'{"ok": true}\n')
            self.assertIn(folder.name + "/nested/empty/", archive.namelist())
            self.assertEqual(archive.read(folder.name + "/.hidden"), b"hidden content")
        # The exact original multipart-file command still uses /upload.
        original = subprocess.run(
            ["curl", "--fail", "--silent", "--show-error", "--max-time", "10", "-F", f'file=@{folder / ".hidden"}', base + "/upload"],
            capture_output=True, timeout=15,
        )
        self.assertEqual(original.returncode, 0, original.stderr.decode())
        file = json.loads(original.stdout)
        self.assertEqual(file["kind"], "file")
        self.assertEqual(self.request("GET", file["download_url"])[1], b"hidden content")

    def test_chunked_zip_with_extensions_and_trailers(self):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("arbitrary/root.txt", b"data")
        payload = output.getvalue()
        chunks = b"".join(
            f"{len(piece):x};example=value\r\n".encode() + piece + b"\r\n"
            for start in range(0, len(payload), 37)
            for piece in [payload[start:start + 37]]
        ) + b"0\r\nX-Upload-Complete: yes\r\n\r\n"
        status, raw = self.raw_chunks(chunks)
        self.assertEqual(status, 201, raw)
        self.assertEqual(json.loads(raw)["filename"], "arbitrary")

    def test_invalid_chunk_framing_and_conflicting_lengths_do_not_publish(self):
        bodies = [
            b"not-hex\r\n", b"-1\r\n", b"1\na\n0\n\n", b"3\r\nab",
            b"1\r\naX\r\n0\r\n\r\n", b"0\r\n", b"1" * 20000 + b"\r\n",
        ]
        for body in bodies:
            with self.subTest(body=body[:30]):
                status, raw = self.raw_chunks(body)
                self.assertEqual(status, 400, raw)
                self.assert_empty_storage()
        status, raw = self.raw_chunks(b"0\r\n\r\n", [("Content-Length", "0")])
        self.assertEqual(status, 400, raw)
        self.assert_empty_storage()

    def test_chunked_upload_limit_is_enforced_on_actual_bytes(self):
        self.config.max_upload_bytes = 8
        status, raw = self.raw_chunks(b"4\r\nabcd\r\n5\r\nefghi\r\n0\r\n\r\n")
        self.assertEqual(status, 413, raw)
        self.assert_empty_storage()

    def test_regular_upload_does_not_start_accepting_chunked_requests(self):
        status, _ = self.raw_chunks(b"4\r\ndata\r\n0\r\n\r\n", path="/upload")
        self.assertEqual(status, 501)
        self.assert_empty_storage()


if __name__ == "__main__":
    unittest.main()
