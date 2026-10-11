"""Check the shared disk budget against real process lifetimes, without S3."""

import importlib.util
import select
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fluxdrop_storage import ColdStorage, StorageCapacityError, StorageSettings


CHILD = """
import sys
from pathlib import Path
from types import SimpleNamespace
from fluxdrop_storage import ColdStorage, StorageSettings
settings = StorageSettings(enabled=True, keyring={'test': b'k' * 32}, key_id='test', disk_reserve_bytes=0)
storage = ColdStorage(Path(sys.argv[1]), settings, remote=object(), disk_usage=lambda _: SimpleNamespace(free=100))
reservation = storage.reserve_space(70)
print('ready', flush=True)
sys.stdin.readline()
storage.close()
"""


@unittest.skipUnless(importlib.util.find_spec("cryptography") and sys.platform != "win32", "Requires optional crypto and POSIX locks")
class StorageProcessTests(unittest.TestCase):
    def test_live_process_budget_is_preserved_and_crash_budget_is_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            child = subprocess.Popen(
                [sys.executable, "-c", CHILD, directory],
                cwd=Path(__file__).resolve().parents[1],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            try:
                self.assertTrue(select.select([child.stdout], [], [], 5)[0], "Child did not start")
                self.assertEqual(child.stdout.readline().strip(), "ready")
                settings = StorageSettings(enabled=True, keyring={"test": b"k" * 32}, key_id="test", disk_reserve_bytes=0)
                storage = ColdStorage(Path(directory), settings, remote=object(), disk_usage=lambda _: SimpleNamespace(free=100))
                try:
                    with self.assertRaises(StorageCapacityError):
                        storage.reserve_space(31)
                    with storage.reserve_space(30):
                        pass
                    child.kill()
                    child.wait(timeout=5)
                    storage.reconcile()
                    with storage.reserve_space(100):
                        pass
                finally:
                    storage.close()
            finally:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
