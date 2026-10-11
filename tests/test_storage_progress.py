"""Offline queue and persisted progress checks using a controllable clock."""

from __future__ import annotations

import importlib.util
import http.client
import json
import tempfile
import threading
import unittest
import zlib
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fluxdrop_storage import ColdStorage, StorageSettings, StorageUnavailable


HAS_CRYPTO = importlib.util.find_spec("cryptography") is not None


class ProgressRemote:
    def __init__(self):
        self.objects = {}
        self.before = None

    def call(self, operation, key):
        if self.before is not None:
            self.before(operation, key)

    def put(self, key, value):
        self.call("put", key)
        self.objects[key] = bytes(value)

    def get(self, key):
        self.call("get", key)
        return self.objects[key]

    def head(self, key):
        self.call("head", key)
        return {"ContentLength": len(self.objects[key])}

    def delete(self, key):
        self.call("delete", key)
        self.objects.pop(key, None)

    def list_keys(self, prefix):
        self.call("list_keys", prefix)
        return [key for key in self.objects if key.startswith(prefix)]


@unittest.skipUnless(HAS_CRYPTO, "cryptography is an optional cold storage dependency")
class StorageProgressTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = 1000.0
        self.counter = 0
        self.remote = ProgressRemote()
        self.settings = StorageSettings(
            enabled=True, key_id="progress-test", keyring={"progress-test": b"p" * 32},
            block_size=64, max_object_bytes=1024 * 1024, disk_reserve_bytes=0,
            retry_base_seconds=10, retry_max_seconds=10, scan_seconds=3600,
        )
        self.storage = ColdStorage(
            self.root, self.settings, self.remote,
            clock=lambda: self.now,
            disk_usage=lambda _: SimpleNamespace(free=1024 * 1024 * 1024),
        )
        self.addCleanup(self.storage.close)

    def upload(self):
        self.counter += 1
        file_id = f"{self.counter:022d}"
        payload = (f"file {self.counter}\n" * 30).encode()
        metadata = {
            "file_id": file_id, "filename": f"file-{self.counter}.txt",
            "size": len(payload), "created_at": int(self.now), "kind": "file", "file_count": 0,
        }
        (self.root / "files" / file_id).write_bytes(payload)
        (self.root / "meta" / f"{file_id}.json").write_text(json.dumps(metadata))
        self.storage.register_file(metadata, {"preview_type": "text"})
        return file_id

    def job(self, file_id):
        return self.storage.storage_info(file_id)["job"]

    def cold(self, file_id):
        self.storage.request_cold(file_id)
        self.assertEqual(self.storage.run_pending(kinds=("archive",)), 1)
        self.assertEqual(self.storage.storage_info(file_id)["state"], "cold")

    def start_blocked_job(self, operation, kind):
        entered = threading.Event()
        release = threading.Event()
        failures = []

        def before(actual_operation, _):
            if actual_operation == operation:
                entered.set()
                if not release.wait(timeout=5):
                    raise AssertionError("Timed out waiting for the test to release remote I/O")

        def run():
            try:
                self.storage.run_pending(kinds=(kind,))
            except BaseException as exc:
                failures.append(exc)

        self.remote.before = before
        thread = threading.Thread(target=run, daemon=True)
        thread.start()

        def finish():
            release.set()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), "Progress worker did not stop")
            if failures:
                raise failures[0]

        self.addCleanup(finish)
        self.assertTrue(entered.wait(timeout=3), "Worker never reached remote I/O")
        return finish

    def test_ready_queue_positions_are_one_based_and_cancelled_jobs_drop_out(self):
        first, second, third = (self.upload() for _ in range(3))
        for file_id in (first, second, third):
            self.storage.request_cold(file_id)
            self.now += 1
        self.assertEqual([self.job(file_id)["queue_position"] for file_id in (first, second, third)], [1, 2, 3])
        self.assertEqual([self.job(file_id)["retry_after"] for file_id in (first, second, third)], [0, 0, 0])
        self.storage.cancel_job(self.job(first)["job_id"])
        self.assertIsNone(self.job(first)["queue_position"])
        self.assertEqual(self.job(second)["queue_position"], 1)
        self.assertEqual(self.job(third)["queue_position"], 2)

    def test_future_retry_does_not_block_ready_ranking_and_countdown_follows_clock(self):
        retrying, ready = self.upload(), self.upload()

        def fail_put(operation, _):
            if operation == "put":
                raise StorageUnavailable("offline test remote")

        self.remote.before = fail_put
        self.storage.request_cold(retrying)
        # Fixed jitter keeps the retry deadline exactly ten seconds ahead.
        with mock.patch("fluxdrop_storage.secrets.randbelow", return_value=250):
            self.assertEqual(self.storage.run_pending(kinds=("archive",)), 1)
        self.remote.before = None
        self.storage.request_cold(ready)
        self.assertEqual(self.job(retrying)["state"], "retry")
        self.assertIsNone(self.job(retrying)["queue_position"])
        self.assertEqual(self.job(ready)["queue_position"], 1)
        self.assertEqual(self.job(retrying)["retry_after"], 10)
        self.now += 3.25
        self.assertEqual(self.job(retrying)["retry_after"], 7)
        self.now += 7
        self.assertEqual(self.job(retrying)["retry_after"], 0)
        self.assertEqual(self.job(retrying)["queue_position"], 1)
        self.assertEqual(self.job(ready)["queue_position"], 2)

    def test_running_upload_phase_is_visible_before_slow_put_and_completion_is_100(self):
        first, queued = self.upload(), self.upload()
        self.storage.request_cold(first)
        finish = self.start_blocked_job("put", "archive")
        running = self.job(first)
        self.assertEqual(running["state"], "running")
        self.assertEqual(running["phase"], "uploading")
        self.assertLess(running["progress"], 100)
        self.storage.request_cold(queued)
        self.assertEqual(self.job(queued)["queue_position"], 2)
        finish()
        completed = self.job(first)
        self.assertEqual(completed["state"], "completed")
        self.assertEqual(completed["progress"], 100)
        self.assertIsNone(completed["queue_position"])
        self.assertEqual(self.job(queued)["queue_position"], 1)

    def test_verification_phase_is_visible_before_first_remote_readback(self):
        file_id = self.upload()
        self.storage.request_cold(file_id)
        finish = self.start_blocked_job("get", "archive")
        job = self.job(file_id)
        self.assertEqual(job["state"], "running")
        self.assertEqual(job["phase"], "verifying")
        self.assertFalse(self.storage.storage_info(file_id)["remote_verified"])
        self.assertTrue((self.root / "files" / file_id).exists())
        finish()
        self.assertEqual(self.job(file_id)["progress"], 100)

    def test_preparing_phase_is_persisted_before_first_compression(self):
        file_id = self.upload()
        self.storage.request_cold(file_id)
        original_compress = zlib.compress
        observed = []

        def compress(data):
            if not observed:
                observed.append(self.job(file_id)["phase"])
            return original_compress(data)

        with mock.patch("fluxdrop_storage.zlib.compress", side_effect=compress):
            self.assertEqual(self.storage.run_pending(kinds=("archive",)), 1)
        self.assertEqual(observed, ["preparing"])

    def test_evicting_phase_is_persisted_before_local_original_is_unlinked(self):
        file_id = self.upload()
        self.storage.request_cold(file_id)
        original_unlink = Path.unlink
        original_path = self.root / "files" / file_id
        observed = []

        def unlink(path, *args, **kwargs):
            if path == original_path:
                info = self.storage.storage_info(file_id)
                observed.append((info["job"]["phase"], info["remote_verified"]))
            return original_unlink(path, *args, **kwargs)

        with mock.patch("fluxdrop_storage.Path.unlink", autospec=True, side_effect=unlink):
            self.assertEqual(self.storage.run_pending(kinds=("archive",)), 1)
        self.assertEqual(observed, [("evicting", True)])
        self.assertEqual(self.job(file_id)["progress"], 100)

    def test_restore_phase_precedes_remote_read_and_positions_only_count_restore_jobs(self):
        restoring, waiting, hot = self.upload(), self.upload(), self.upload()
        self.cold(restoring)
        self.cold(waiting)
        self.storage.request_restore(restoring)
        finish = self.start_blocked_job("get", "restore")
        current = self.job(restoring)
        self.assertEqual(current["state"], "running")
        self.assertEqual(current["phase"], "restoring")
        self.storage.request_restore(waiting)
        self.storage.request_cold(hot)
        self.assertEqual(self.job(waiting)["queue_position"], 2)
        self.assertEqual(self.job(hot)["queue_position"], 1)
        finish()
        self.assertEqual(self.storage.storage_info(restoring)["state"], "hot_backed")
        self.assertEqual(self.job(restoring)["progress"], 100)
        self.assertEqual(self.job(waiting)["queue_position"], 1)

    def test_http_list_and_status_publish_running_queue_and_completed_progress(self):
        from app import FluxDropConfig, make_handler

        first, second = self.upload(), self.upload()
        config = FluxDropConfig(self.root, None, None, 1024 * 1024, cold_storage=self.storage)
        # The test drives one job explicitly, keeping the second project queued.
        with mock.patch.object(self.storage, "start_worker"):
            config.ensure_dirs()
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(config))
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()

        def close_server():
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

        self.addCleanup(close_server)

        def get(path):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            try:
                connection.request("GET", path)
                response = connection.getresponse()
                payload = response.read()
                self.assertEqual(response.status, 200, payload)
                return json.loads(payload)
            finally:
                connection.close()

        self.storage.request_cold(first)
        finish = self.start_blocked_job("put", "archive")
        self.storage.request_cold(second)
        status = get(f"/api/files/{first}/storage")
        listing = {item["file_id"]: item for item in get("/api/files")["files"]}
        self.assertEqual(status["storage_job"]["state"], "running")
        self.assertEqual(status["storage_job"]["phase"], "uploading")
        self.assertEqual(listing[first]["storage_job"], status["storage_job"])
        self.assertEqual(listing[second]["storage_job"]["state"], "queued")
        self.assertEqual(listing[second]["storage_job"]["queue_position"], 2)
        finish()
        listing = {item["file_id"]: item for item in get("/api/files")["files"]}
        self.assertEqual(listing[first]["storage_state"], "cold")
        self.assertEqual(listing[first]["storage_job"]["state"], "completed")
        self.assertEqual(listing[first]["storage_job"]["progress"], 100)
        self.assertEqual(listing[second]["storage_job"]["queue_position"], 1)


if __name__ == "__main__":
    unittest.main()
