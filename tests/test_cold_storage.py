"""Offline fault-injection tests: no AWS client or real bucket is used."""

import builtins
import hashlib
import importlib.util
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fluxdrop_storage import (
    ColdStorage, StorageCapacityError, StorageConfigurationError, StorageDisabled,
    StorageIntegrityError, StorageNotFound, StorageSettings, StorageUnavailable,
)


HAS_CRYPTO = importlib.util.find_spec("cryptography") is not None


class FakeRemote:
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.before = None

    def event(self, operation, key):
        self.calls.append((operation, key))
        if self.before:
            self.before(operation, key)

    def put(self, key, data):
        self.event("put", key)
        self.objects[key] = bytes(data)

    def get(self, key):
        self.event("get", key)
        return self.objects[key]

    def head(self, key):
        self.event("head", key)
        return {"ContentLength": len(self.objects[key])}

    def delete(self, key):
        self.event("delete", key)
        self.objects.pop(key, None)

    def list_keys(self, prefix):
        self.event("list_keys", prefix)
        return [key for key in self.objects if key.startswith(prefix)]


class DisabledStorageTests(unittest.TestCase):
    def test_disabled_storage_never_imports_optional_libraries(self):
        real_import = builtins.__import__

        def guarded_import(name, *args, **kwargs):
            if name.startswith(("boto3", "botocore", "cryptography")):
                raise AssertionError("disabled storage imported an optional dependency")
            return real_import(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory, mock.patch("builtins.__import__", side_effect=guarded_import):
            root = Path(directory)
            (root / "files").mkdir()
            file_id = "a" * 22
            (root / "files" / file_id).write_bytes(b"still local")
            storage = ColdStorage(root, StorageSettings(enabled=False))
            with storage.open_file(file_id) as source:
                self.assertEqual(source.read(), b"still local")
            self.assertFalse((root / "storage.sqlite3").exists())
            with self.assertRaises(StorageDisabled):
                storage.request_cold(file_id)
            storage.close()

    def test_disabled_environment_ignores_unused_invalid_cold_settings(self):
        with mock.patch.dict(os.environ, {"FLUXDROP_COLD_STORAGE_ENABLED": "false", "FLUXDROP_COLD_BLOCK_MB": "invalid"}):
            self.assertFalse(StorageSettings.from_env().enabled)

    def test_enabled_environment_requires_keyring_path(self):
        with mock.patch.dict(os.environ, {"FLUXDROP_COLD_STORAGE_ENABLED": "true"}, clear=True):
            with self.assertRaises(StorageConfigurationError):
                StorageSettings.from_env()


@unittest.skipUnless(HAS_CRYPTO, "cryptography is an optional cold storage dependency")
class ColdStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.now = 1_000_000.0
        self.free = 1024 * 1024 * 1024
        self.remote = FakeRemote()
        self.settings = StorageSettings(enabled=True, keyring={"test": b"k" * 32}, key_id="test",
                                        block_size=128, max_object_bytes=1024 * 1024,
                                        disk_reserve_bytes=0, wait_seconds=0.02, idle_seconds=100,
                                        retry_base_seconds=1, retry_max_seconds=5, scan_seconds=1000)
        self.storage = self.make_storage()
        self.addCleanup(self.storage.close)
        self.counter = 0

    def make_storage(self):
        return ColdStorage(self.root, self.settings, self.remote, clock=lambda: self.now,
                           disk_usage=lambda _: SimpleNamespace(free=self.free))

    def upload(self, data=b"hello world\n" * 90, *, cached=None, created=None, kind="file"):
        self.counter += 1
        file_id = f"{self.counter:022d}"
        metadata = {"file_id": file_id, "filename": "example.txt", "size": len(data),
                    "created_at": self.now if created is None else created, "kind": kind, "file_count": 0}
        (self.root / "files" / file_id).write_bytes(data)
        (self.root / "meta" / f"{file_id}.json").write_text(json.dumps(metadata))
        self.storage.register_file(metadata, {"preview_type": "text"} if cached is None else cached)
        return file_id, metadata, data

    def cold(self, file_id):
        self.storage.request_cold(file_id)
        self.assertEqual(self.storage.run_pending(), 1)
        self.assertEqual(self.storage.storage_info(file_id)["state"], "cold")

    def restore(self, file_id):
        self.storage.request_restore(file_id)
        self.assertEqual(self.storage.run_pending(), 1)
        self.assertEqual(self.storage.storage_info(file_id)["state"], "hot_backed")

    def test_authenticated_chunk_roundtrip_and_json_compatibility(self):
        data = b"secret plaintext marker\n" * 25 + bytes(range(256)) * 2
        file_id, metadata, _ = self.upload(data)
        self.cold(file_id)
        self.assertTrue(self.storage.has_file(file_id))
        self.assertFalse((self.root / "files" / file_id).exists())
        self.assertEqual(json.loads((self.root / "meta" / f"{file_id}.json").read_text()), metadata)
        for ciphertext in self.remote.objects.values():
            self.assertNotIn(b"secret plaintext marker", ciphertext)
        self.restore(file_id)
        with self.storage.open_file(file_id) as source:
            self.assertEqual(source.read(), data)
        self.assertEqual(self.storage.storage_info(file_id)["job"]["progress"], 100)

    def test_empty_file_is_archived_and_restored(self):
        file_id, _, _ = self.upload(b"")
        self.cold(file_id)
        self.restore(file_id)
        self.assertEqual((self.root / "files" / file_id).read_bytes(), b"")

    def test_first_publication_requires_full_remote_readback(self):
        file_id, _, original = self.upload()

        def corrupt_get(operation, key):
            if operation == "get" and "/block-" in key:
                self.remote.objects[key] = b"x" + self.remote.objects[key][1:]

        self.remote.before = corrupt_get
        self.storage.request_cold(file_id)
        self.storage.run_pending()
        info = self.storage.storage_info(file_id)
        self.assertEqual(info["state"], "hot")
        self.assertEqual(info["job"]["state"], "failed")
        self.assertEqual(info["job"]["error_code"], "storage_integrity")
        self.assertFalse(info["remote_verified"])
        self.assertEqual((self.root / "files" / file_id).read_bytes(), original)

    def test_corrupt_manifest_and_blocks_never_publish_restored_content(self):
        for target in ("manifest.json", "block-00000000"):
            with self.subTest(target=target):
                file_id, _, _ = self.upload()
                self.cold(file_id)
                key = next(k for k in self.remote.objects if k.endswith(target) and file_id in k)
                self.remote.objects[key] = b"\xff" + self.remote.objects[key][1:]
                self.storage.request_restore(file_id)
                self.storage.run_pending()
                self.assertFalse((self.root / "files" / file_id).exists())
                self.assertEqual(self.storage.storage_info(file_id)["job"]["state"], "failed")
                with self.assertRaises(StorageIntegrityError):
                    with self.storage.open_file(file_id):
                        pass

    def test_remote_reuse_never_uploads_same_content_again(self):
        file_id, _, _ = self.upload()
        self.cold(file_id)
        self.restore(file_id)
        puts = len([op for op, _ in self.remote.calls if op == "put"])
        self.cold(file_id)
        self.assertEqual(len([op for op, _ in self.remote.calls if op == "put"]), puts)

    def test_reused_archive_tampering_keeps_local_file(self):
        file_id, _, data = self.upload()
        self.cold(file_id)
        self.restore(file_id)
        key = next(k for k in self.remote.objects if "/block-" in k)
        self.remote.objects[key] = b"\xff" + self.remote.objects[key][1:]
        self.storage.request_cold(file_id)
        self.storage.run_pending()
        self.assertEqual((self.root / "files" / file_id).read_bytes(), data)
        self.assertEqual(self.storage.storage_info(file_id)["job"]["state"], "failed")

    def test_duplicate_manual_requests_and_cancel_are_idempotent(self):
        file_id, _, _ = self.upload()
        one = self.storage.request_cold(file_id)
        two = self.storage.request_cold(file_id)
        self.assertEqual(one["job_id"], two["job_id"])
        self.assertTrue(self.storage.cancel_job(one["job_id"])["cancelled"])
        self.assertTrue(self.storage.cancel_job(one["job_id"])["cancelled"])
        self.assertEqual(self.storage.run_pending(), 0)
        self.assertTrue((self.root / "files" / file_id).exists())

    def test_content_access_cancels_archive_during_remote_put(self):
        file_id, _, data = self.upload()

        def access_during_put(operation, key):
            if operation == "put":
                self.remote.before = None
                with self.storage.open_file(file_id) as source:
                    self.assertEqual(source.read(), data)

        self.remote.before = access_during_put
        self.storage.request_cold(file_id)
        self.storage.run_pending()
        self.assertEqual(self.storage.storage_info(file_id)["job"]["state"], "cancelled")
        self.assertTrue((self.root / "files" / file_id).exists())
        self.assertEqual(self.storage.cleanup_orphans(), 1)
        self.assertEqual(self.remote.objects, {})

    def test_active_read_pin_prevents_unlink_without_counting_head_as_access(self):
        file_id, _, data = self.upload()
        with self.storage.open_file(file_id, access=False) as source:
            self.storage.request_cold(file_id)
            self.storage.run_pending()
            self.assertEqual(source.read(), data)
            self.assertTrue((self.root / "files" / file_id).exists())
            self.assertEqual(self.storage.storage_info(file_id)["job"]["state"], "retry")
        self.now += 10
        self.storage.run_pending()
        self.assertEqual(self.storage.storage_info(file_id)["state"], "cold")

    def test_cancel_after_commit_does_not_claim_to_preserve_local_file(self):
        file_id, _, _ = self.upload()
        self.cold(file_id)
        info = self.storage.storage_info(file_id)
        result = self.storage.cancel_job(info["job_id"])
        self.assertFalse(result["cancelled"])
        self.assertTrue(result["already_committed"])

    def test_restore_is_single_flight_even_when_own_reservation_uses_all_space(self):
        file_id, _, data = self.upload()
        self.cold(file_id)
        self.free = len(data)
        first = self.storage.request_restore(file_id)
        reservation = self.storage.reserve_space(len(data))
        try:
            second = self.storage.request_restore(file_id)
            self.assertEqual(first["job_id"], second["job_id"])
        finally:
            reservation.release()

    def test_restore_capacity_preflight_returns_507(self):
        file_id, _, _ = self.upload()
        self.cold(file_id)
        self.free = 0
        with self.assertRaises(StorageCapacityError) as raised:
            self.storage.request_restore(file_id)
        self.assertEqual(raised.exception.status, 507)
        with self.assertRaises(StorageCapacityError):
            with self.storage.open_file(file_id):
                pass

    def test_worker_capacity_race_is_persisted_and_returned_as_507(self):
        file_id, _, _ = self.upload()
        self.cold(file_id)
        self.storage.request_restore(file_id)
        self.free = 0
        self.storage.run_pending()
        job = self.storage.storage_info(file_id)["job"]
        self.assertEqual(job["state"], "retry")
        self.assertEqual(job["error_code"], "storage_capacity")
        with self.assertRaises(StorageCapacityError):
            with self.storage.open_file(file_id):
                pass

    def test_space_reservations_account_only_for_remaining_bytes(self):
        self.free = 100
        with self.storage.reserve_space(80) as reservation:
            self.free = 60
            reservation.consume(40)
            with self.storage.reserve_space(20):
                with self.assertRaises(StorageCapacityError):
                    self.storage.reserve_space(1)
        with self.storage.reserve_space(60):
            pass

    def test_restore_keeps_verified_progress_and_resumes_after_outage(self):
        file_id, _, data = self.upload()
        self.cold(file_id)
        failed = [False]

        def fail_second_block(operation, key):
            if operation == "get" and key.endswith("block-00000001") and not failed[0]:
                failed[0] = True
                raise ConnectionError("offline")

        self.remote.before = fail_second_block
        self.storage.request_restore(file_id)
        self.storage.run_pending()
        job = self.storage.storage_info(file_id)["job"]
        self.assertEqual(job["state"], "retry")
        temp = next(self.root.glob(".restore-*.tmp"))
        self.assertEqual(temp.read_bytes(), data[:128])
        first_gets = len([k for op, k in self.remote.calls if op == "get" and k.endswith("block-00000000")])
        self.remote.before = None
        self.now += 10
        self.storage.run_pending()
        self.assertEqual((self.root / "files" / file_id).read_bytes(), data)
        self.assertEqual(len([k for op, k in self.remote.calls if op == "get" and k.endswith("block-00000000")]), first_gets)

    def test_restore_revalidates_corrupted_local_partial_before_resuming(self):
        file_id, _, data = self.upload()
        self.cold(file_id)

        def fail_second(operation, key):
            if operation == "get" and key.endswith("block-00000001"):
                raise ConnectionError("offline")

        self.remote.before = fail_second
        self.storage.request_restore(file_id)
        self.storage.run_pending()
        next(self.root.glob(".restore-*.tmp")).write_bytes(b"tampered")
        self.remote.before = None
        self.now += 10
        self.storage.run_pending()
        self.assertEqual((self.root / "files" / file_id).read_bytes(), data)

    def test_transient_archive_failure_preserves_original_and_retries_blocks(self):
        file_id, _, original = self.upload()
        failed = [False]

        def fail_second_put(operation, key):
            if operation == "put" and key.endswith("block-00000001") and not failed[0]:
                failed[0] = True
                raise ConnectionError("offline")

        self.remote.before = fail_second_put
        self.storage.request_cold(file_id)
        self.storage.run_pending()
        self.assertEqual((self.root / "files" / file_id).read_bytes(), original)
        self.assertEqual(self.storage.storage_info(file_id)["job"]["state"], "retry")
        self.now += 10
        self.remote.before = None
        self.storage.run_pending()
        self.assertEqual(self.storage.storage_info(file_id)["state"], "cold")
        self.assertEqual(len([k for op, k in self.remote.calls if op == "put" and k.endswith("block-00000000")]), 1)

    def test_persistent_tombstone_prevents_json_reimport_and_removes_remote(self):
        file_id, metadata, _ = self.upload()
        self.cold(file_id)
        self.storage.delete_file(file_id)
        self.assertTrue(self.storage.is_deleted(file_id))
        self.assertFalse(self.storage.has_file(file_id))
        with self.assertRaises(StorageNotFound):
            self.storage.register_file(metadata)
        self.storage.run_pending()
        self.assertFalse(self.remote.objects)
        self.assertFalse((self.root / "meta" / f"{file_id}.json").exists())

    def test_delete_waits_for_inflight_put_and_cleans_late_object(self):
        file_id, _, _ = self.upload()
        admitted, finish = threading.Event(), threading.Event()

        def block_put(operation, key):
            if operation == "put":
                admitted.set()
                if not finish.wait(3):
                    raise TimeoutError("test did not release PUT")

        self.remote.before = block_put
        self.storage.request_cold(file_id)
        archive = threading.Thread(target=self.storage.run_pending)
        archive.start()
        self.assertTrue(admitted.wait(2))
        self.storage.delete_file(file_id)
        deletion = threading.Thread(target=lambda: self.storage.run_pending(kinds=("delete",)))
        deletion.start()
        time.sleep(0.02)
        self.assertTrue(deletion.is_alive())
        finish.set()
        archive.join(3)
        deletion.join(3)
        self.assertFalse(archive.is_alive())
        self.assertFalse(deletion.is_alive())
        self.assertEqual(self.remote.objects, {})
        self.assertTrue(self.storage.is_deleted(file_id))

    def test_restart_reconciles_crash_between_eviction_unlink_and_db_commit(self):
        file_id, _, _ = self.upload()
        self.cold(file_id)
        with self.storage._db(True) as db:
            db.execute("UPDATE files SET state='evicting' WHERE file_id=?", (file_id,))
            db.execute("UPDATE jobs SET state='running',lease_owner=?,lease_until=? WHERE file_id=?", (self.storage._owner + ":crash", self.now + 999, file_id))
        self.storage.close()
        reopened = self.make_storage()
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.storage_info(file_id)["state"], "cold")
        self.assertEqual(reopened.storage_info(file_id)["job"]["state"], "completed")

    def test_restart_reconciles_restore_rename_before_state_commit(self):
        file_id, _, _ = self.upload()
        self.cold(file_id)
        self.restore(file_id)
        with self.storage._db(True) as db:
            db.execute("UPDATE files SET state='restoring' WHERE file_id=?", (file_id,))
            db.execute("UPDATE jobs SET state='running',lease_owner=?,lease_until=? WHERE file_id=? AND kind='restore'", (self.storage._owner + ":crash", self.now + 999, file_id))
        self.storage.close()
        reopened = self.make_storage()
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.storage_info(file_id)["state"], "hot_backed")
        self.assertEqual(reopened.storage_info(file_id)["job"]["state"], "completed")

    def test_shutdown_during_archiving_keeps_intent_restartable(self):
        file_id, _, _ = self.upload()

        def stop_on_put(operation, key):
            if operation == "put":
                self.storage._stop.set()

        self.remote.before = stop_on_put
        self.storage.request_cold(file_id)
        self.storage.run_pending()
        self.assertEqual(self.storage.storage_info(file_id)["job"]["state"], "retry")
        self.storage.close()
        self.remote.before = None
        reopened = self.make_storage()
        self.addCleanup(reopened.close)
        reopened.run_pending()
        self.assertEqual(reopened.storage_info(file_id)["state"], "cold")

    def test_automatic_rule_uses_latest_access_and_metadata_cache(self):
        old, _, _ = self.upload(created=self.now - 200)
        accessed, _, _ = self.upload(created=self.now - 200)
        uncached, _, _ = self.upload(created=self.now - 200, cached={})
        self.storage.touch_content(accessed)
        self.assertEqual(self.storage.scan_idle(), 1)
        self.assertIsNotNone(self.storage.storage_info(old)["job"])
        self.assertIsNone(self.storage.storage_info(accessed)["job"])
        self.assertIsNone(self.storage.storage_info(uncached)["job"])

    def test_pressure_eviction_only_reuses_verified_old_unpinned_files(self):
        file_id, _, _ = self.upload()
        self.cold(file_id)
        self.restore(file_id)
        self.settings.disk_reserve_bytes = 100
        self.free = 50
        self.assertEqual(self.storage.scan_pressure(), 0)
        self.now += 61
        with self.storage.open_file(file_id, access=False):
            self.assertEqual(self.storage.scan_pressure(), 0)
        self.assertEqual(self.storage.scan_pressure(), 1)
        self.storage.run_pending()
        self.assertEqual(self.storage.storage_info(file_id)["state"], "cold")

    def test_background_worker_restores_with_bounded_request_wait(self):
        file_id, _, data = self.upload()
        self.cold(file_id)
        self.storage.start_worker()
        with self.storage.open_file(file_id, timeout=3) as source:
            self.assertEqual(source.read(), data)

    def test_key_rotation_retains_ability_to_restore_old_archives(self):
        file_id, _, data = self.upload()
        self.cold(file_id)
        self.settings.keyring["next"] = b"n" * 32
        self.settings.key_id = "next"
        self.restore(file_id)
        self.assertEqual((self.root / "files" / file_id).read_bytes(), data)

    def test_missing_historical_key_does_not_publish_plaintext(self):
        file_id, _, _ = self.upload()
        self.cold(file_id)
        self.settings.keyring = {"next": b"n" * 32}
        self.settings.key_id = "next"
        self.storage.request_restore(file_id)
        self.storage.run_pending()
        self.assertEqual(self.storage.storage_info(file_id)["job"]["error_code"], "storage_configuration")
        self.assertFalse((self.root / "files" / file_id).exists())

    def test_expired_lease_cannot_take_over_a_live_restore_writer(self):
        file_id, _, data = self.upload(b"original")
        self.cold(file_id)
        self.settings.task_lease_seconds = 1
        other = self.make_storage()
        self.addCleanup(other.close)
        admitted, finish = threading.Event(), threading.Event()

        def blocked_decode(operation, key):
            if operation == "get" and "/block-" in key:
                admitted.set()
                if not finish.wait(3):
                    raise TimeoutError("test did not release GET")

        self.remote.before = blocked_decode
        self.storage.request_restore(file_id)
        first = threading.Thread(target=self.storage.run_pending)
        first.start()
        self.assertTrue(admitted.wait(2))
        self.now += 2
        self.assertEqual(other.run_pending(), 0)
        finish.set()
        first.join(3)
        self.assertFalse(first.is_alive())
        self.assertEqual((self.root / "files" / file_id).read_bytes(), data)
        self.assertEqual(self.storage.storage_info(file_id)["state"], "hot_backed")

    def test_actual_temporary_file_is_verified_before_publish(self):
        file_id, _, _ = self.upload()
        self.cold(file_id)
        real_checkpoint = self.storage._checkpoint

        def append_unexpected_bytes(*args, **kwargs):
            real_checkpoint(*args, **kwargs)
            if kwargs.get("progress") == 95:
                with next(self.root.glob(".restore-*.tmp")).open("ab") as target:
                    target.write(b"unexpected")

        self.storage.request_restore(file_id)
        with mock.patch.object(self.storage, "_checkpoint", side_effect=append_unexpected_bytes):
            self.storage.run_pending()
        self.assertEqual(self.storage.storage_info(file_id)["job"]["state"], "failed")
        self.assertFalse((self.root / "files" / file_id).exists())

    def test_s3_client_and_credentials_are_lazy_and_network_failure_is_wrapped(self):
        self.settings.bucket = "offline-test-bucket"
        with mock.patch("boto3.client", side_effect=ConnectionError("metadata service offline")) as client:
            storage = ColdStorage(self.root, self.settings, clock=lambda: self.now)
            self.addCleanup(storage.close)
            client.assert_not_called()
            with self.assertRaises(StorageUnavailable):
                storage.remote.head("does-not-connect-to-a-real-service")
            client.assert_called_once()

    def test_reaper_preserves_live_owners_and_recovers_dead_owner_budget(self):
        self.free = 100
        other = self.make_storage()
        self.addCleanup(other.close)
        reservation = other.reserve_space(80)
        self.storage.reap_dead_owners()
        with self.assertRaises(StorageCapacityError):
            self.storage.reserve_space(30)
        # Simulate process death: its OS owner lock releases independently of DB.
        other._owner_lock.close()
        self.storage.reap_dead_owners()
        with self.storage.reserve_space(100):
            pass
        reservation.release()

    def test_capacity_failed_restore_does_not_block_pressure_eviction(self):
        cache_id, _, _ = self.upload(b"cached" * 100)
        self.cold(cache_id)
        self.restore(cache_id)
        cold_id, _, _ = self.upload(b"cold" * 200)
        self.cold(cold_id)
        self.storage.request_restore(cold_id)
        self.free = 500
        self.storage.run_pending(kinds=("restore",))
        self.assertEqual(self.storage.storage_info(cold_id)["job"]["error_code"], "storage_capacity")
        self.now += 61
        self.assertEqual(self.storage.scan_pressure(), 1)
        self.assertEqual(self.storage.run_pending(kinds=("archive",)), 1)
        self.assertEqual(self.storage.storage_info(cache_id)["state"], "cold")

    def test_circuit_breaker_is_persistent_and_limits_outage_requests(self):
        self.settings.circuit_failures = 2

        def offline(operation, key):
            if operation == "put":
                raise ConnectionError("offline")

        self.remote.before = offline
        ids = [self.upload()[0] for _ in range(3)]
        for file_id in ids:
            self.storage.request_cold(file_id)
        self.storage.run_pending(limit=3)
        self.assertEqual(len([op for op, _ in self.remote.calls if op == "put"]), 2)
        with self.storage._db() as db:
            circuit = json.loads(db.execute("SELECT value FROM runtime WHERE key='circuit'").fetchone()[0])
            self.assertGreater(circuit["until"], self.now)
        for file_id in ids:
            self.assertTrue((self.root / "files" / file_id).exists())

    def test_permissions_failure_stops_automatic_retry_without_deleting_local(self):
        file_id, _, data = self.upload()

        def denied(operation, key):
            if operation == "put":
                raise StorageConfigurationError("S3 permissions are invalid")

        self.remote.before = denied
        self.storage.request_cold(file_id)
        self.storage.run_pending()
        self.now += 100
        self.assertEqual(self.storage.run_pending(), 0)
        self.assertEqual(self.storage.storage_info(file_id)["job"]["state"], "failed")
        self.assertEqual((self.root / "files" / file_id).read_bytes(), data)

    def test_failed_restore_retry_reuses_same_verified_prefix_and_job_id(self):
        file_id, _, data = self.upload()
        self.cold(file_id)
        key = next(k for k in self.remote.objects if k.endswith("block-00000001"))
        good = self.remote.objects[key]
        self.remote.objects[key] = b"\xff" + good[1:]
        first = self.storage.request_restore(file_id)
        self.storage.run_pending()
        self.assertEqual(self.storage.storage_info(file_id)["job"]["state"], "failed")
        self.assertEqual(next(self.root.glob(".restore-*.tmp")).stat().st_size, 128)
        self.remote.objects[key] = good
        self.free = len(data) - 128
        second = self.storage.request_restore(file_id)
        self.assertEqual(first["job_id"], second["job_id"])
        self.storage.run_pending()
        self.assertEqual((self.root / "files" / file_id).read_bytes(), data)
        self.assertFalse(list(self.root.glob(".restore-*.tmp")))

    def test_missing_sdk_credentials_are_permanent_configuration_error(self):
        from botocore.exceptions import NoCredentialsError, PartialCredentialsError
        self.settings.bucket = "offline-test-bucket"
        for error in (NoCredentialsError(), PartialCredentialsError(provider="env", cred_var="secret_key")):
            with self.subTest(error=type(error).__name__), mock.patch("boto3.client", side_effect=error):
                storage = ColdStorage(self.root, self.settings, clock=lambda: self.now)
                try:
                    with self.assertRaises(StorageConfigurationError):
                        storage.remote.head("offline")
                finally:
                    storage.close()

    def test_failed_delete_restarts_once_after_configuration_is_repaired(self):
        file_id, _, _ = self.upload()
        self.cold(file_id)
        self.storage.delete_file(file_id)

        def deny_list(operation, key):
            if operation == "list_keys":
                raise StorageConfigurationError("permission denied")

        self.remote.before = deny_list
        self.storage.run_pending()
        self.now += 100
        self.assertEqual(self.storage.run_pending(), 0)
        self.storage.close()
        self.remote.before = None
        reopened = self.make_storage()
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.run_pending(), 1)
        self.assertFalse(self.remote.objects)
        self.assertTrue(reopened.is_deleted(file_id))

    def test_archive_scheduler_recovers_from_transient_claim_failure(self):
        file_id, _, _ = self.upload()
        real_run = self.storage.run_pending
        failed = threading.Event()

        def transient_failure(*args, **kwargs):
            if kwargs.get("kinds") == ("archive",) and not failed.is_set():
                failed.set()
                raise sqlite3.OperationalError("database is locked")
            return real_run(*args, **kwargs)

        self.settings.retry_base_seconds = 0.05
        self.storage.request_cold(file_id)
        with mock.patch.object(self.storage, "run_pending", side_effect=transient_failure), self.assertLogs("fluxdrop_storage", level="WARNING") as logs:
            self.storage.start_worker()
            self.assertTrue(failed.wait(2))
            deadline = time.monotonic() + 3
            while self.storage.storage_info(file_id)["state"] != "cold" and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual(self.storage.storage_info(file_id)["state"], "cold")
            self.assertTrue(all(thread.is_alive() for thread in self.storage._threads))
        self.assertTrue(any("archive scheduler encountered OperationalError" in line for line in logs.output))

    def test_maintenance_recovers_without_stopping_content_workers(self):
        real_reap = self.storage.reap_dead_owners
        failed, recovered = threading.Event(), threading.Event()

        def transient_failure():
            if not failed.is_set():
                failed.set()
                raise sqlite3.OperationalError("database is locked")
            real_reap()
            recovered.set()

        with mock.patch.object(self.storage, "reap_dead_owners", side_effect=transient_failure), self.assertLogs("fluxdrop_storage", level="WARNING") as logs:
            self.storage.start_worker()
            self.assertTrue(failed.wait(2))
            file_id, _, _ = self.upload()
            self.storage.request_cold(file_id)
            self.assertTrue(recovered.wait(2))
            self.assertEqual(self.storage.storage_info(file_id)["state"], "cold")
            self.assertTrue(all(thread.is_alive() for thread in self.storage._threads))
        self.assertTrue(any("maintenance encountered OperationalError" in line for line in logs.output))

    def test_start_worker_replaces_dead_role_without_duplicating_live_threads(self):
        real_run = self.storage.run_pending
        exited = threading.Event()

        def exit_once(*args, **kwargs):
            if kwargs.get("kinds") == ("archive",) and not exited.is_set():
                exited.set()
                raise SystemExit()
            return real_run(*args, **kwargs)

        with mock.patch.object(self.storage, "run_pending", side_effect=exit_once):
            self.storage.start_worker()
            self.assertTrue(exited.wait(2))
            old = {thread.name: thread for thread in self.storage._threads}
            old["fluxdrop-archive"].join(2)
            self.assertFalse(old["fluxdrop-archive"].is_alive())
        self.storage.start_worker()
        current = {thread.name: thread for thread in self.storage._threads}
        self.assertIsNot(current["fluxdrop-archive"], old["fluxdrop-archive"])
        self.assertIs(current["fluxdrop-restore"], old["fluxdrop-restore"])
        self.assertIs(current["fluxdrop-storage-maintenance"], old["fluxdrop-storage-maintenance"])
        starters = [threading.Thread(target=self.storage.start_worker) for _ in range(6)]
        for thread in starters:
            thread.start()
        for thread in starters:
            thread.join(2)
        self.assertEqual(len(self.storage._threads), 3)
        self.assertTrue(all(thread.is_alive() for thread in self.storage._threads))
        file_id, _, _ = self.upload()
        self.storage.request_cold(file_id)
        deadline = time.monotonic() + 3
        while self.storage.storage_info(file_id)["state"] != "cold" and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(self.storage.storage_info(file_id)["state"], "cold")

    def test_closed_manager_does_not_restart_released_owner(self):
        self.storage.close()
        with self.assertRaises(StorageUnavailable):
            self.storage.start_worker()
        self.assertTrue(self.storage._stop.is_set())


if __name__ == "__main__":
    unittest.main()
