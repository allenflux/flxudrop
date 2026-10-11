"""Exercise cold storage through the public and management HTTP boundaries.

All remote objects live in memory; these tests never contact an AWS endpoint.
"""

from __future__ import annotations

import base64
import http.client
import io
import json
import os
import tarfile
import tempfile
import threading
import time
import unittest
import zipfile
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import quote, urlencode, urlsplit

from app import FluxDropConfig, make_handler
from fluxdrop_storage import ColdStorage, StorageSettings


class MemoryRemote:
    def __init__(self):
        self.objects = {}
        self.lock = threading.RLock()
        self.puts = 0
        self.gets = 0
        self.unavailable = False
        self.put_gate = None
        self.put_entered = threading.Event()
        self.get_gate = None
        self.get_entered = threading.Event()

    def _check(self):
        if self.unavailable:
            raise OSError("Simulated remote outage")

    def put(self, key, data):
        with self.lock:
            self._check()
            self.put_entered.set()
            if self.put_gate is not None and not self.put_gate.wait(timeout=5):
                raise OSError("Simulated remote write timed out")
            self.puts += 1
            self.objects[key] = bytes(data)

    def get(self, key):
        with self.lock:
            self._check()
            self.get_entered.set()
            if self.get_gate is not None and not self.get_gate.wait(timeout=5):
                raise OSError("Simulated remote read timed out")
            self.gets += 1
            return self.objects[key]

    def head(self, key):
        with self.lock:
            self._check()
            return {"ContentLength": len(self.objects[key])}

    def delete(self, key):
        with self.lock:
            self._check()
            self.objects.pop(key, None)

    def list_keys(self, prefix):
        with self.lock:
            self._check()
            return [key for key in self.objects if key.startswith(prefix)]


class ColdStorageHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        key_file = self.root / "keys.json"
        key_file.write_text(json.dumps({"active": "test", "keys": {
            "test": base64.b64encode(b"k" * 32).decode("ascii")
        }}), encoding="utf-8")
        environment = {
            "FLUXDROP_COLD_STORAGE_ENABLED": "1",
            "FLUXDROP_S3_BUCKET": "tests-never-connect",
            "FLUXDROP_ENCRYPTION_KEY_FILE": str(key_file),
            "FLUXDROP_ENCRYPTION_KEY_ID": "test",
            "FLUXDROP_COLD_DISK_RESERVE_MB": "0",
            "FLUXDROP_COLD_WAIT_SECONDS": "1",
            "FLUXDROP_COLD_SCAN_SECONDS": "3600",
            "FLUXDROP_COLD_RETRY_BASE_SECONDS": "60",
            "FLUXDROP_COLD_RETRY_MAX_SECONDS": "60",
        }
        with mock.patch.dict("os.environ", environment):
            settings = StorageSettings.from_env(self.root)
        self.remote = MemoryRemote()
        self.storage = ColdStorage(self.root, settings=settings, remote=self.remote)
        self.config = FluxDropConfig(
            self.root, None, "management-token", 4 * 1024 * 1024,
            cold_storage=self.storage,
        )
        self.config.ensure_dirs()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.config))
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        self.thread.start()
        self.auth = {"Authorization": "Bearer management-token"}

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.config.close()
        self.tempdir.cleanup()

    def request(self, method, path, body=b"", headers=None):
        with closing(http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)) as connection:
            parsed = urlsplit(path)
            connection.request(method, parsed.path + ("?" + parsed.query if parsed.query else ""), body, headers or {})
            response = connection.getresponse()
            payload = response.read()
            return response, payload

    def upload(self, name="notes.txt", payload=b"hello from cold storage\n"):
        response, raw = self.request("PUT", "/upload/" + quote(name), payload, self.auth)
        self.assertEqual(response.status, 201, raw)
        return json.loads(raw)

    def info(self, file_id):
        response, raw = self.request("GET", f"/api/files/{file_id}/storage")
        self.assertEqual(response.status, 200, raw)
        return json.loads(raw)

    def wait_state(self, file_id, expected, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            info = self.storage.storage_info(file_id)
            if info["state"] in expected:
                return info
            time.sleep(0.01)
        self.fail(f"Expected {expected}; actual {self.storage.storage_info(file_id)}")

    def cold(self, stored):
        response, raw = self.request("POST", f'/api/files/{stored["file_id"]}/cold', headers=self.auth)
        self.assertEqual(response.status, 202, raw)
        self.wait_state(stored["file_id"], {"cold"})
        self.assertFalse((self.config.files_dir / stored["file_id"]).exists())
        return json.loads(raw)

    def test_manual_cold_preserves_listing_and_head_does_not_restore(self):
        payload = b"a confidential text file\n" * 100
        stored = self.upload(payload=payload)
        self.cold(stored)
        get_count = self.remote.gets
        response, raw = self.request("GET", "/api/files", headers=self.auth)
        self.assertEqual(response.status, 200)
        listing = json.loads(raw)
        self.assertTrue(listing["storage_enabled"])
        self.assertEqual(len(listing["files"]), 1)
        self.assertEqual(listing["files"][0]["storage_state"], "cold")
        self.assertEqual(listing["files"][0]["preview_type"], "text")
        for path in (stored["download_url"], stored["preview_url"]):
            response, body = self.request("HEAD", path)
            self.assertEqual(response.status, 200)
            self.assertEqual(body, b"")
            self.assertEqual(int(response.getheader("Content-Length")), len(payload))
        self.assertEqual(self.remote.gets, get_count)
        self.assertEqual(self.info(stored["file_id"])["storage_state"], "cold")
        self.assertTrue(self.remote.objects)
        self.assertTrue(all(payload not in obj for obj in self.remote.objects.values()))

    def test_head_does_not_advertise_missing_local_file_without_remote_archive(self):
        stored = self.upload(payload=b"lost local original")
        (self.config.files_dir / stored["file_id"]).unlink()
        self.assertFalse(self.remote.objects)
        for path in (stored["download_url"], stored["preview_url"]):
            response, raw = self.request("HEAD", path)
            self.assertEqual(response.status, 404, raw)
            self.assertEqual(raw, b"")

    def test_repeated_active_restore_reuses_job_despite_low_free_space(self):
        stored = self.upload(payload=b"one shared restoration")
        self.cold(stored)
        gate = threading.Event()
        self.remote.get_gate = gate
        self.remote.get_entered.clear()
        try:
            response, raw = self.request("POST", f'/api/files/{stored["file_id"]}/restore?preview=1')
            self.assertEqual(response.status, 202, raw)
            job_id = json.loads(raw)["storage_job"]["job_id"]
            self.assertTrue(self.remote.get_entered.wait(timeout=2))
            with mock.patch.object(self.storage, "_disk_usage", return_value=SimpleNamespace(free=0)):
                response, raw = self.request("POST", f'/api/files/{stored["file_id"]}/restore?preview=1')
                self.assertEqual(response.status, 202, raw)
                self.assertEqual(json.loads(raw)["storage_job"]["job_id"], job_id)
        finally:
            gate.set()
        self.wait_state(stored["file_id"], {"hot", "hot_backed"})
        response, raw = self.request("GET", stored["preview_url"])
        self.assertEqual(response.status, 200, raw)
        self.assertEqual(raw, b"one shared restoration")

    def test_download_recovers_original_and_next_cold_reuses_archive(self):
        payload = bytes(range(256)) * 1024
        stored = self.upload("archive.bin", payload)
        self.cold(stored)
        put_count = self.remote.puts
        response, raw = self.request("GET", stored["download_url"])
        self.assertEqual(response.status, 200, raw[:200])
        self.assertEqual(raw, payload)
        self.wait_state(stored["file_id"], {"hot", "hot_backed"})
        self.cold(stored)
        self.assertEqual(self.remote.puts, put_count)

    def test_duplicate_manual_requests_share_job_and_cancel_preserves_local_file(self):
        stored = self.upload(payload=b"cancel archive safely")
        gate = threading.Event()
        self.remote.put_gate = gate
        try:
            response, raw = self.request("POST", f'/api/files/{stored["file_id"]}/cold', headers=self.auth)
            self.assertEqual(response.status, 202, raw)
            job_id = json.loads(raw)["storage_job"]["job_id"]
            self.assertTrue(self.remote.put_entered.wait(timeout=2))
            response, raw = self.request("POST", f'/api/files/{stored["file_id"]}/cold', headers=self.auth)
            self.assertEqual(response.status, 202, raw)
            self.assertEqual(json.loads(raw)["storage_job"]["job_id"], job_id)
            response, raw = self.request("POST", f"/api/storage-jobs/{job_id}/cancel")
            self.assertEqual(response.status, 401, raw)
            response, raw = self.request("POST", f"/api/storage-jobs/{job_id}/cancel", headers=self.auth)
            self.assertEqual(response.status, 200, raw)
            response, raw = self.request("POST", f"/api/storage-jobs/{job_id}/cancel", headers=self.auth)
            self.assertEqual(response.status, 200, raw)
        finally:
            gate.set()
        self.wait_state(stored["file_id"], {"hot", "hot_backed"})
        response, raw = self.request("GET", stored["download_url"])
        self.assertEqual(response.status, 200, raw)
        self.assertEqual(raw, b"cancel archive safely")

    def test_cancellation_after_eviction_commit_reports_conflict(self):
        stored = self.upload()
        result = self.cold(stored)
        job_id = result["storage_job"]["job_id"]
        response, raw = self.request("POST", f"/api/storage-jobs/{job_id}/cancel", headers=self.auth)
        self.assertEqual(response.status, 409, raw)
        self.assertEqual(json.loads(raw)["error_code"], "storage_cancel_committed")
        self.assertEqual(self.storage.storage_info(stored["file_id"])["state"], "cold")
        self.assertFalse((self.config.files_dir / stored["file_id"]).exists())

    def test_failed_archive_preserves_readable_local_original(self):
        payload = b"must remain on this VPS"
        stored = self.upload(payload=payload)
        self.remote.unavailable = True
        response, raw = self.request("POST", f'/api/files/{stored["file_id"]}/cold', headers=self.auth)
        self.assertEqual(response.status, 202, raw)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            info = self.storage.storage_info(stored["file_id"])
            if info["job"] and info["job"]["state"] in {"retry", "failed"}:
                break
            time.sleep(0.01)
        else:
            self.fail(f"Archive failure was not recorded: {info}")
        self.assertIn(info["state"], {"hot", "hot_backed"})
        response, raw = self.request("GET", stored["download_url"])
        self.assertEqual(response.status, 200, raw)
        self.assertEqual(raw, payload)

    def test_media_range_preview_recovers_and_preserves_range_response(self):
        payload = bytes(range(256)) * 32
        stored = self.upload("movie.mp4", payload)
        self.cold(stored)
        response, raw = self.request("GET", stored["preview_url"], headers={"Range": "bytes=17-99"})
        self.assertEqual(response.status, 206, raw)
        self.assertEqual(response.getheader("Content-Range"), f"bytes 17-99/{len(payload)}")
        self.assertEqual(raw, payload[17:100])
        self.wait_state(stored["file_id"], {"hot", "hot_backed"})

    def test_invalid_range_and_unsupported_preview_do_not_restore(self):
        stored = self.upload("audio.mp3", b"0123456789")
        self.cold(stored)
        response, _ = self.request("GET", stored["preview_url"], headers={"Range": "bytes=20-"})
        self.assertEqual(response.status, 416)
        self.assertEqual(self.storage.storage_info(stored["file_id"])["state"], "cold")
        unsupported = self.upload("binary.bin", bytes(range(256)))
        self.cold(unsupported)
        response, _ = self.request("GET", unsupported["preview_url"])
        self.assertEqual(response.status, 415)
        self.assertEqual(self.storage.storage_info(unsupported["file_id"])["state"], "cold")

    def test_invalid_restore_requests_do_not_touch_or_restore_file(self):
        stored = self.upload("notes.txt", b"retain cold state")
        self.cold(stored)
        before = self.storage.storage_info(stored["file_id"])["last_content_access_at"]
        for query in ("preview=0", "unexpected=1", "path=..%2Foutside", "preview=1&x=1"):
            with self.subTest(query=query):
                response, raw = self.request("POST", f'/api/files/{stored["file_id"]}/restore?{query}')
                self.assertEqual(response.status, 400, raw)
                info = self.storage.storage_info(stored["file_id"])
                self.assertEqual(info["state"], "cold")
                self.assertEqual(info["last_content_access_at"], before)
        unsupported = self.upload("blob.bin", bytes(range(256)))
        self.cold(unsupported)
        response, raw = self.request("POST", f'/api/files/{unsupported["file_id"]}/restore?preview=1')
        self.assertEqual(response.status, 415, raw)
        self.assertEqual(self.storage.storage_info(unsupported["file_id"])["state"], "cold")

    def test_directory_browse_stays_cold_and_child_preview_recovers(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as target:
            target.writestr("folder/sub/notes.txt", b"nested notes\n")
            target.writestr("folder/empty/", b"")
        response, raw = self.request("PUT", "/upload-directory", archive.getvalue(), self.auth)
        self.assertEqual(response.status, 201, raw)
        stored = json.loads(raw)
        self.cold(stored)
        get_count = self.remote.gets
        response, raw = self.request("GET", f'/api/directories/{stored["file_id"]}?path=sub', headers=self.auth)
        self.assertEqual(response.status, 200, raw)
        entries = json.loads(raw)["entries"]
        self.assertEqual(entries[0]["filename"], "notes.txt")
        self.assertEqual(self.remote.gets, get_count)
        self.assertEqual(self.storage.storage_info(stored["file_id"])["state"], "cold")
        for path in ("missing.txt", "../outside"):
            response, raw = self.request("POST", f'/api/files/{stored["file_id"]}/restore?' + urlencode({"path": path}))
            self.assertIn(response.status, {400, 404}, raw)
            self.assertEqual(self.storage.storage_info(stored["file_id"])["state"], "cold")
        response, raw = self.request("GET", entries[0]["preview_url"])
        self.assertEqual(response.status, 200, raw)
        self.assertEqual(raw, b"nested notes\n")

    def test_bulk_download_restores_cold_files_before_sending_zip(self):
        first = self.upload("first.txt", b"first")
        second = self.upload("second.txt", b"second")
        self.cold(first)
        self.cold(second)
        response, raw = self.request("GET", "/api/files/download?" + urlencode([
            ("file_id", first["file_id"]), ("file_id", second["file_id"])
        ]))
        self.assertEqual(response.status, 200, raw[:200])
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            self.assertEqual(archive.read("first.txt"), b"first")
            self.assertEqual(archive.read("second.txt"), b"second")

    def test_remote_outage_returns_retryable_error_while_hot_content_works(self):
        stored = self.upload("cold.txt", b"cold content")
        hot = self.upload("hot.txt", b"still hot")
        self.cold(stored)
        self.remote.unavailable = True
        response, raw = self.request("GET", stored["download_url"])
        self.assertEqual(response.status, 503, raw)
        self.assertIsNotNone(response.getheader("Retry-After"))
        self.assertFalse(json.loads(raw)["ok"])
        self.assertFalse((self.config.files_dir / stored["file_id"]).exists())
        response, raw = self.request("GET", hot["download_url"])
        self.assertEqual(response.status, 200)
        self.assertEqual(raw, b"still hot")

    def test_insufficient_space_rejects_upload_and_restore_without_partial_files(self):
        stored = self.upload("cold.txt", b"needs local disk")
        self.cold(stored)
        with mock.patch.object(self.storage, "_disk_usage", return_value=SimpleNamespace(free=0)):
            response, raw = self.request("PUT", "/upload/new.txt", b"new payload", self.auth)
            self.assertEqual(response.status, 507, raw)
            self.assertEqual(list(self.root.glob(".upload-*")), [])
            response, raw = self.request("GET", stored["download_url"])
            self.assertEqual(response.status, 507, raw)
            self.assertFalse((self.config.files_dir / stored["file_id"]).exists())

    def test_multipart_directory_accounts_for_part_and_zip_at_same_time(self):
        payload = b"x" * 1000
        body = (b'--budget\r\nContent-Disposition: form-data; name="file"; filename="folder/data.txt"\r\n\r\n'
                + payload + b'\r\n--budget--\r\n')
        original = tempfile.TemporaryFile
        parts = []

        def tracked_temporary_file(*args, **kwargs):
            part = original(*args, **kwargs)
            parts.append(part)
            return part

        def disk_usage(_):
            used = sum(path.stat().st_size for path in self.root.glob(".upload-*"))
            used += sum(os.fstat(part.fileno()).st_size for part in parts if not part.closed)
            return SimpleNamespace(free=1100 - used)

        with mock.patch("fluxdrop_multipart.tempfile.TemporaryFile", side_effect=tracked_temporary_file), \
                mock.patch.object(self.storage, "_disk_usage", side_effect=disk_usage):
            response, raw = self.request("POST", "/upload-directory", body, {
                **self.auth, "Content-Type": "multipart/form-data; boundary=budget",
                "X-FluxDrop-Directory": "files",
            })
        self.assertEqual(response.status, 507, raw)
        self.assertEqual(list(self.config.files_dir.iterdir()), [])
        self.assertEqual(list(self.root.glob(".upload-*")), [])

    def test_tar_conversion_accounts_for_input_and_output_at_same_time(self):
        payload = b"x" * 4000
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w") as archive:
            info = tarfile.TarInfo("folder/data.txt")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        body = stream.getvalue()

        def disk_usage(_):
            used = sum(path.stat().st_size for path in self.root.glob(".upload-*"))
            return SimpleNamespace(free=len(body) + len(payload) // 2 - used)

        with mock.patch.object(self.storage, "_disk_usage", side_effect=disk_usage):
            response, raw = self.request("PUT", "/upload-directory", body, self.auth)
        self.assertEqual(response.status, 507, raw)
        self.assertEqual(list(self.config.files_dir.iterdir()), [])
        self.assertEqual(list(self.root.glob(".upload-*")), [])

    def test_corrupted_remote_data_is_never_published(self):
        stored = self.upload(payload=b"must be authenticated\n" * 100)
        self.cold(stored)
        with self.remote.lock:
            for key, payload in list(self.remote.objects.items()):
                self.remote.objects[key] = payload[:-1] + bytes([payload[-1] ^ 1])
        response, raw = self.request("GET", stored["download_url"])
        self.assertEqual(response.status, 503, raw)
        self.assertFalse((self.config.files_dir / stored["file_id"]).exists())
        self.assertNotIn(self.storage.storage_info(stored["file_id"])["state"], {"hot", "hot_backed"})

    def test_cold_management_obeys_token_and_deleted_file_cannot_restore(self):
        stored = self.upload()
        response, raw = self.request("POST", f'/api/files/{stored["file_id"]}/cold')
        self.assertEqual(response.status, 401, raw)
        self.cold(stored)
        response, raw = self.request("DELETE", f'/api/files/{stored["file_id"]}', headers=self.auth)
        self.assertEqual(response.status, 200, raw)
        response, raw = self.request("DELETE", f'/api/files/{stored["file_id"]}', headers=self.auth)
        self.assertEqual(response.status, 200, raw)
        for method, path in (("GET", stored["download_url"]),
                             ("POST", f'/api/files/{stored["file_id"]}/restore')):
            response, raw = self.request(method, path)
            self.assertEqual(response.status, 404, raw)


if __name__ == "__main__":
    unittest.main()
