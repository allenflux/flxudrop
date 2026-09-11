from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from app import FluxDropConfig, StoredFile, load_metadata, save_metadata, store_file


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.config = FluxDropConfig(
            storage_dir=Path(self.tempdir.name),
            public_base_url=None,
            upload_token=None,
            max_upload_bytes=1024,
        )
        self.config.ensure_dirs()
        self.stored = StoredFile("test-file", "example.txt", 12, 123456)
        self.meta_path = self.config.meta_dir / "test-file.json"

    def test_metadata_replacement_publishes_complete_json(self) -> None:
        previous = StoredFile("test-file", "previous.txt", 1, 123455)
        save_metadata(self.config, previous)
        replace = os.replace

        def check_before_replace(source: Path, destination: Path) -> None:
            self.assertEqual(load_metadata(self.config, "test-file"), previous)
            self.assertEqual(json.loads(source.read_text()), asdict(self.stored))
            self.assertEqual(source.parent, destination.parent)
            replace(source, destination)

        with patch("app.os.replace", side_effect=check_before_replace) as replace_mock:
            save_metadata(self.config, self.stored)

        replace_mock.assert_called_once()
        self.assertEqual(load_metadata(self.config, "test-file"), self.stored)
        self.assertEqual(list(self.config.meta_dir.iterdir()), [self.meta_path])

    def test_failed_metadata_write_keeps_previous_record_and_cleans_temporary_file(self) -> None:
        previous = StoredFile("test-file", "previous.txt", 1, 123455)
        save_metadata(self.config, previous)

        def fail_during_write(data, target, **kwargs) -> None:
            target.write('{"file_id":')
            raise OSError("disk full")

        with patch("app.json.dump", side_effect=fail_during_write):
            with self.assertRaisesRegex(OSError, "disk full"):
                save_metadata(self.config, self.stored)

        self.assertEqual(load_metadata(self.config, "test-file"), previous)
        self.assertEqual(list(self.config.meta_dir.iterdir()), [self.meta_path])

    def test_failed_metadata_replace_cleans_temporary_file(self) -> None:
        with patch("app.os.replace", side_effect=OSError("cannot replace")):
            with self.assertRaisesRegex(OSError, "cannot replace"):
                save_metadata(self.config, self.stored)

        self.assertEqual(list(self.config.meta_dir.iterdir()), [])

    def test_failed_metadata_commit_removes_uploaded_data(self) -> None:
        upload = self.config.files_dir / "upload.tmp"
        upload.write_bytes(b"uploaded data")

        with patch("app.os.replace", side_effect=OSError("metadata commit failed")):
            with self.assertRaisesRegex(OSError, "metadata commit failed"):
                store_file(self.config, "example.txt", upload, upload.stat().st_size)

        self.assertEqual(list(self.config.files_dir.iterdir()), [])
        self.assertEqual(list(self.config.meta_dir.iterdir()), [])

    def test_store_file_round_trip(self) -> None:
        upload = self.config.files_dir / "upload.tmp"
        payload = b"uploaded data"
        upload.write_bytes(payload)

        stored = store_file(self.config, "../example?.txt", upload, len(payload))

        self.assertEqual(stored.filename, "example_.txt")
        self.assertEqual(stored.size, len(payload))
        self.assertEqual(load_metadata(self.config, stored.file_id), stored)
        self.assertEqual((self.config.files_dir / stored.file_id).read_bytes(), payload)
        self.assertFalse(upload.exists())

    def test_missing_or_corrupt_metadata_is_ignored(self) -> None:
        self.assertIsNone(load_metadata(self.config, "test-file"))
        for payload in (b"\xff", b"{", b"[]", b'{"file_id":"test-file"}'):
            with self.subTest(payload=payload):
                self.meta_path.write_bytes(payload)
                self.assertIsNone(load_metadata(self.config, "test-file"))


if __name__ == "__main__":
    unittest.main()
