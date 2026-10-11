"""Durable cold storage for immutable FluxDrop uploads.

The JSON upload records remain owned by the HTTP application.  SQLite records
storage state, display indexes and restartable jobs; remote objects are private.
No third-party dependency is imported while cold storage is disabled.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import threading
import time
import uuid
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Iterator, Mapping, Protocol

LOG = logging.getLogger(__name__)
_ID = re.compile(r"[A-Za-z0-9_-]{16,64}\Z")
_ACTIVE = ("queued", "running", "retry")
_FORMAT = "fluxdrop-cold-v1"


class StorageError(Exception):
    status = 503
    code = "storage_error"
    retry_after = 5


class StorageUnavailable(StorageError):
    code = "storage_unavailable"


class _StorageBusy(StorageUnavailable):
    code = "storage_busy"


class StorageIntegrityError(StorageUnavailable):
    code = "storage_integrity"


class StorageConfigurationError(StorageError):
    status = 409
    code = "storage_configuration"


class StorageCapacityError(StorageError):
    status = 507
    code = "storage_capacity"


class StorageNotFound(StorageError):
    status = 404
    code = "storage_not_found"


class StorageDisabled(StorageConfigurationError):
    code = "storage_disabled"


class _JobCancelled(Exception):
    pass


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.b64decode(value, validate=True)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass
class StorageSettings:
    enabled: bool = False
    bucket: str = ""
    region: str = "ap-northeast-3"
    endpoint: str | None = None
    prefix: str = "fluxdrop-cold/"
    key_file: Path | None = None
    key_id: str = ""
    keyring: dict[str, bytes] = field(default_factory=dict, repr=False)
    idle_seconds: float = 7 * 86400
    block_size: int = 32 * 1024 * 1024
    max_object_bytes: int = 100 * 1024 * 1024
    disk_reserve_bytes: int = 512 * 1024 * 1024
    wait_seconds: float = 15
    connect_timeout: float = 5
    read_timeout: float = 30
    scan_seconds: float = 300
    retry_base_seconds: float = 2
    retry_max_seconds: float = 300
    circuit_failures: int = 3
    circuit_seconds: float = 30
    task_lease_seconds: float = 120

    @classmethod
    def from_env(cls, storage_dir: Path | str | None = None) -> "StorageSettings":
        # storage_dir is accepted to keep application configuration convenient.
        value = os.environ.get("FLUXDROP_COLD_STORAGE_ENABLED", "false").lower()
        if value not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
            raise StorageConfigurationError("FLUXDROP_COLD_STORAGE_ENABLED must be a boolean")
        enabled = value in {"true", "1", "yes", "on"}
        if not enabled:
            return cls(enabled=False)
        try:
            settings = cls(
                enabled=True,
                bucket=os.environ.get("FLUXDROP_S3_BUCKET", ""),
                region=os.environ.get("FLUXDROP_S3_REGION", "ap-northeast-3"),
                endpoint=os.environ.get("FLUXDROP_S3_ENDPOINT") or None,
                prefix=os.environ.get("FLUXDROP_S3_PREFIX", "fluxdrop-cold/"),
                key_file=Path(os.environ["FLUXDROP_ENCRYPTION_KEY_FILE"]),
                key_id=os.environ.get("FLUXDROP_ENCRYPTION_KEY_ID", ""),
                idle_seconds=float(os.environ.get("FLUXDROP_COLD_IDLE_DAYS", "7")) * 86400,
                block_size=int(os.environ.get("FLUXDROP_COLD_BLOCK_MB", "32")) * 1024 * 1024,
                max_object_bytes=int(os.environ.get("FLUXDROP_COLD_MAX_OBJECT_MB", "100")) * 1024 * 1024,
                disk_reserve_bytes=int(os.environ.get("FLUXDROP_COLD_DISK_RESERVE_MB", "512")) * 1024 * 1024,
                wait_seconds=float(os.environ.get("FLUXDROP_COLD_WAIT_SECONDS", "15")),
                connect_timeout=float(os.environ.get("FLUXDROP_S3_CONNECT_TIMEOUT", "5")),
                read_timeout=float(os.environ.get("FLUXDROP_S3_READ_TIMEOUT", "30")),
                scan_seconds=float(os.environ.get("FLUXDROP_COLD_SCAN_SECONDS", "300")),
                retry_base_seconds=float(os.environ.get("FLUXDROP_COLD_RETRY_BASE_SECONDS", "2")),
                retry_max_seconds=float(os.environ.get("FLUXDROP_COLD_RETRY_MAX_SECONDS", "300")),
            )
        except (ValueError, KeyError) as exc:
            raise StorageConfigurationError("Invalid or missing cold storage configuration") from exc
        return settings


class RemoteStore(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def head(self, key: str) -> Mapping[str, Any]: ...
    def delete(self, key: str) -> None: ...
    def list_keys(self, prefix: str) -> Iterator[str]: ...


class S3Store:
    """Small bounded S3 adapter; credentials come only from the SDK chain."""

    def __init__(self, settings: StorageSettings):
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise StorageConfigurationError("Cold storage requires boto3") from exc
        self.settings = settings
        self._client = None
        self._client_lock = threading.Lock()
        self._client_factory = boto3.client
        self._client_config = Config(connect_timeout=settings.connect_timeout,
                                     read_timeout=settings.read_timeout,
                                     retries={"mode": "standard", "total_max_attempts": 3})

    @property
    def client(self):
        # Credential providers may access the network (for example IMDS).
        # Existing local files must remain available if that network is down.
        with self._client_lock:
            if self._client is None:
                self._client = self._client_factory("s3", region_name=self.settings.region,
                                    endpoint_url=self.settings.endpoint, config=self._client_config)
            return self._client

    @contextmanager
    def _errors(self):
        try:
            yield
        except StorageError:
            raise
        except Exception as exc:
            from botocore.exceptions import NoCredentialsError, PartialCredentialsError, ProfileNotFound
            if isinstance(exc, (NoCredentialsError, PartialCredentialsError, ProfileNotFound)):
                raise StorageConfigurationError("S3 credentials are missing or incomplete") from exc
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if code in {"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch", "ExpiredToken", "NoSuchBucket"}:
                raise StorageConfigurationError("S3 credentials, bucket or permissions are invalid") from exc
            if code in {"NoSuchKey", "NotFound", "404"}:
                raise StorageIntegrityError("An archive object is missing") from exc
            # Do not return SDK exception text, which can contain request details.
            raise StorageUnavailable("S3 request failed; retry later") from exc

    def put(self, key: str, data: bytes) -> None:
        with self._errors():
            self.client.put_object(Bucket=self.settings.bucket, Key=key, Body=data,
                                   ContentType="application/octet-stream", StorageClass="STANDARD",
                                   ChecksumSHA256=_b64(hashlib.sha256(data).digest()))

    def get(self, key: str) -> bytes:
        with self._errors():
            response = self.client.get_object(Bucket=self.settings.bucket, Key=key)
            body = response["Body"]
            try:
                if response.get("ContentLength", 0) > self.settings.max_object_bytes:
                    raise StorageIntegrityError("Archive object exceeds the configured limit")
                data = body.read(self.settings.max_object_bytes + 1)
                if len(data) > self.settings.max_object_bytes:
                    raise StorageIntegrityError("Archive object exceeds the configured limit")
                return data
            finally:
                body.close()

    def head(self, key: str) -> Mapping[str, Any]:
        with self._errors():
            return self.client.head_object(Bucket=self.settings.bucket, Key=key)

    def delete(self, key: str) -> None:
        with self._errors():
            self.client.delete_object(Bucket=self.settings.bucket, Key=key)

    def list_keys(self, prefix: str) -> Iterator[str]:
        with self._errors():
            for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.settings.bucket, Prefix=prefix):
                for item in page.get("Contents", []):
                    yield item["Key"]


class SpaceReservation:
    def __init__(self, storage: "ColdStorage", amount: int):
        self.storage = storage
        self.reservation_id = uuid.uuid4().hex
        self.remaining = 0
        self.closed = False
        self.grow(amount)

    def grow(self, amount: int) -> None:
        if self.closed or amount < 0:
            raise ValueError("Invalid space reservation")
        with self.storage._budget_lock:
            self.storage._reserve(self.reservation_id, amount)
            self.remaining += amount

    def consume(self, amount: int) -> None:
        """Call after these bytes have been written to the shared filesystem."""
        if amount < 0 or amount > self.remaining:
            raise ValueError("Invalid reservation consumption")
        with self.storage._budget_lock:
            self.remaining -= amount
            self.storage._set_reservation(self.reservation_id, self.remaining)

    def release(self) -> None:
        if not self.closed:
            with self.storage._budget_lock:
                self.storage._set_reservation(self.reservation_id, 0, remove=True)
                self.remaining = 0
                self.closed = True

    def __enter__(self) -> "SpaceReservation":
        return self

    def __exit__(self, *args) -> None:
        self.release()


class ColdStorage:
    def __init__(self, storage_dir: Path | str, settings: StorageSettings | None = None,
                 remote: RemoteStore | None = None, *, clock=time.time, disk_usage=shutil.disk_usage):
        self.storage_dir = Path(storage_dir)
        self.files_dir = self.storage_dir / "files"
        self.meta_dir = self.storage_dir / "meta"
        self.settings = settings or StorageSettings.from_env(self.storage_dir)
        self.enabled = self.settings.enabled
        self._clock, self._disk_usage = clock, disk_usage
        self._owner = uuid.uuid4().hex
        self._locks: dict[str, threading.RLock] = {}
        self._locks_lock = threading.Lock()
        self._budget_lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._active_calls = 0
        self._owner_closed = False
        self._disabled_reservations: dict[str, int] = {}
        self._disabled_cache: dict[str, dict] = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._threads: list[threading.Thread] = []
        self.remote = remote
        if not self.enabled:
            return
        self._validate_settings(remote is not None)
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            self._AESGCM = AESGCM
        except ImportError as exc:
            raise StorageConfigurationError("Cold storage requires cryptography") from exc
        if not self.settings.keyring:
            self._load_keys()
        if self.settings.key_id not in self.settings.keyring or any(len(k) != 32 for k in self.settings.keyring.values()):
            raise StorageConfigurationError("Encryption keyring must contain 32-byte AES keys and an active key_id")
        self.remote = remote if remote is not None else S3Store(self.settings)
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        self._lock_dir = self.storage_dir / ".storage-locks"
        self._lock_dir.mkdir(exist_ok=True)
        import fcntl
        self._owner_lock = (self._lock_dir / (self._owner + ".owner")).open("a+b")
        fcntl.flock(self._owner_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.db_path = self.storage_dir / "storage.sqlite3"
        self._initialize()
        self.reconcile()

    def _validate_settings(self, fake: bool) -> None:
        s = self.settings
        if not fake and not s.bucket:
            raise StorageConfigurationError("FLUXDROP_S3_BUCKET is required when cold storage is enabled")
        if not s.keyring and s.key_file is None:
            raise StorageConfigurationError("FLUXDROP_ENCRYPTION_KEY_FILE is required")
        if not 0 < s.block_size <= 64 * 1024 * 1024 or s.max_object_bytes < s.block_size + 16:
            raise StorageConfigurationError("Invalid cold storage block/object size")
        if s.disk_reserve_bytes < 0 or s.idle_seconds <= 0 or s.wait_seconds < 0 or s.scan_seconds <= 0:
            raise StorageConfigurationError("Invalid cold storage timing or disk budget")
        if min(s.connect_timeout, s.read_timeout, s.retry_base_seconds, s.retry_max_seconds, s.task_lease_seconds) <= 0:
            raise StorageConfigurationError("Cold storage timeouts must be positive")
        s.prefix = s.prefix.strip("/") + "/"

    def _load_keys(self) -> None:
        try:
            value = json.loads(self.settings.key_file.read_text(encoding="utf-8"))
            self.settings.keyring = {name: _unb64(key) for name, key in value["keys"].items()}
            self.settings.key_id = self.settings.key_id or value["active"]
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise StorageConfigurationError("Cannot read the cold storage encryption keyring") from exc

    @contextmanager
    def _db(self, write: bool = False):
        db = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA synchronous=FULL")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if write:
                db.commit()
        except BaseException:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    def _initialize(self) -> None:
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS files (
                    file_id TEXT PRIMARY KEY, metadata TEXT NOT NULL, cached TEXT NOT NULL DEFAULT '{}',
                    state TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, size INTEGER NOT NULL,
                    created_at REAL NOT NULL, modified_at REAL NOT NULL, last_access REAL NOT NULL,
                    access_epoch INTEGER NOT NULL DEFAULT 0, remote_verified INTEGER NOT NULL DEFAULT 0,
                    manifest_key TEXT, manifest_sha TEXT, generation TEXT, content_sha TEXT,
                    fingerprint TEXT, error TEXT, deleted_at REAL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY, file_id TEXT NOT NULL, kind TEXT NOT NULL,
                    state TEXT NOT NULL, progress REAL NOT NULL DEFAULT 0, error TEXT, error_code TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0, next_run REAL NOT NULL DEFAULT 0,
                    lease_owner TEXT, lease_until REAL NOT NULL DEFAULT 0, created_at REAL NOT NULL,
                    access_epoch INTEGER NOT NULL, details TEXT NOT NULL DEFAULT '{}'
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_storage_job
                    ON jobs(file_id) WHERE state IN ('queued','running','retry');
                CREATE TABLE IF NOT EXISTS reservations (
                    reservation_id TEXT PRIMARY KEY, owner TEXT NOT NULL, remaining INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS owners (owner TEXT PRIMARY KEY, pid INTEGER NOT NULL, heartbeat REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS readers (reader_id TEXT PRIMARY KEY, file_id TEXT NOT NULL, owner TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runtime (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            if "error_code" not in {r[1] for r in db.execute("PRAGMA table_info(jobs)")}:
                db.execute("ALTER TABLE jobs ADD COLUMN error_code TEXT")
        with self._db(True) as db:
            db.execute("INSERT OR REPLACE INTO owners VALUES (?,?,?)", (self._owner, os.getpid(), self._clock()))

    @contextmanager
    def _guard(self, file_id: str):
        if not _ID.fullmatch(file_id):
            raise StorageNotFound("File not found")
        with self._locks_lock:
            lock = self._locks.setdefault(file_id, threading.RLock())
        with lock:
            import fcntl
            with (self._lock_dir / (file_id + ".mutex")).open("a+b") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def _file(self, db, file_id: str):
        row = db.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone()
        if row is None or row["deleted_at"] is not None:
            raise StorageNotFound("File not found")
        return row

    def register_file(self, metadata: Mapping[str, Any], cached: Mapping[str, Any] | None = None) -> dict:
        metadata = dict(metadata)
        metadata.setdefault("kind", "file")
        metadata.setdefault("file_count", 0)
        file_id = metadata["file_id"]
        if not _ID.fullmatch(file_id):
            raise StorageNotFound("File not found")
        if not self.enabled:
            if cached:
                self._disabled_cache.setdefault(file_id, {}).update(cached)
            return self.storage_info(file_id)
        with self._guard(file_id), self._db(True) as db:
            existing = db.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone()
            if existing is not None:
                if existing["deleted_at"] is not None:
                    raise StorageNotFound("File has been deleted")
                original = json.loads(existing["metadata"])
                if any(original.get(k) != metadata.get(k) for k in ("file_id", "filename", "size", "created_at", "kind")):
                    raise StorageConfigurationError("An immutable upload record changed")
            else:
                created = float(metadata["created_at"])
                db.execute("""INSERT INTO files(file_id,metadata,cached,state,size,created_at,modified_at,last_access)
                              VALUES (?,?,?,?,?,?,?,?)""",
                           (file_id, _json(dict(metadata)), _json(dict(cached or {})), "hot", metadata["size"], created, created, created))
            if cached and existing is not None:
                merged = json.loads(existing["cached"])
                merged.update(cached)
                db.execute("UPDATE files SET cached=? WHERE file_id=?", (_json(merged), file_id))
        return self.storage_info(file_id)

    def cached_metadata(self, file_id: str) -> dict:
        if not self.enabled:
            return dict(self._disabled_cache.get(file_id, {}))
        with self._db() as db:
            return json.loads(self._file(db, file_id)["cached"])

    def cache_metadata(self, file_id: str, cached: Mapping[str, Any]) -> None:
        if not self.enabled:
            self._disabled_cache.setdefault(file_id, {}).update(cached)
            return
        with self._guard(file_id), self._db(True) as db:
            merged = json.loads(self._file(db, file_id)["cached"])
            merged.update(cached)
            db.execute("UPDATE files SET cached=? WHERE file_id=?", (_json(merged), file_id))

    def has_file(self, file_id: str) -> bool:
        if not self.enabled:
            return (self.files_dir / file_id).is_file()
        with self._db() as db:
            row = db.execute("SELECT deleted_at,remote_verified FROM files WHERE file_id=?", (file_id,)).fetchone()
            return bool(row is not None and row["deleted_at"] is None and (row["remote_verified"] or (self.files_dir / file_id).is_file()))

    def is_deleted(self, file_id: str) -> bool:
        if not self.enabled:
            return False
        with self._db() as db:
            row = db.execute("SELECT deleted_at FROM files WHERE file_id=?", (file_id,)).fetchone()
            return row is not None and row["deleted_at"] is not None

    @staticmethod
    def _job_info(row) -> dict | None:
        if row is None:
            return None
        return {"job_id": row["job_id"], "file_id": row["file_id"], "kind": row["kind"],
                "state": row["state"], "progress": min(100, max(0, row["progress"])),
                "error": row["error"], "attempts": row["attempts"],
                "error_code": row["error_code"],
                "cancellable": row["kind"] == "archive" and row["state"] in _ACTIVE}

    def storage_info(self, file_id: str) -> dict:
        if not self.enabled:
            return {"file_id": file_id, "state": "hot", "job": None, "job_id": None,
                    "error": None, "remote_verified": False, "cached": self.cached_metadata(file_id)}
        with self._db() as db:
            row = self._file(db, file_id)
            job = db.execute("SELECT * FROM jobs WHERE file_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (file_id,)).fetchone()
            return {"file_id": file_id, "state": row["state"], "job": self._job_info(job),
                    "job_id": job["job_id"] if job else None, "error": row["error"],
                    "last_content_access_at": row["last_access"], "remote_verified": bool(row["remote_verified"]),
                    "cached": json.loads(row["cached"])}

    def _require_enabled(self):
        if not self.enabled:
            raise StorageDisabled("Cold storage is disabled")

    def _enqueue(self, db, row, kind: str) -> str:
        old = db.execute("SELECT * FROM jobs WHERE file_id=? AND state IN ('queued','running','retry')", (row["file_id"],)).fetchone()
        if old:
            return old["job_id"]
        if kind == "restore":
            failed = db.execute("SELECT * FROM jobs WHERE file_id=? AND kind='restore' AND state='failed' ORDER BY created_at DESC,rowid DESC LIMIT 1", (row["file_id"],)).fetchone()
            if failed:
                # Reuse its authenticated partial file and execution lock. A new
                # ID would silently abandon the disk space used by that prefix.
                db.execute("UPDATE jobs SET state='queued',error=NULL,error_code=NULL,next_run=0,lease_owner=NULL,lease_until=0 WHERE job_id=?", (failed["job_id"],))
                return failed["job_id"]
        job_id = uuid.uuid4().hex
        db.execute("""INSERT INTO jobs(job_id,file_id,kind,state,created_at,access_epoch)
                      VALUES (?,?,?,'queued',?,?)""", (job_id, row["file_id"], kind, self._clock(), row["access_epoch"]))
        return job_id

    def request_cold(self, file_id: str, manual: bool = True, *, _pressure: bool = False) -> dict:
        self._require_enabled()
        with self._guard(file_id), self._db(True) as db:
            row = self._file(db, file_id)
            if _pressure and (not row["remote_verified"] or self._clock() - row["last_access"] < max(60, self.settings.wait_seconds)):
                raise _StorageBusy("Recently accessed files are protected from pressure eviction")
            if row["state"] == "cold":
                return {"ok": True, "file_id": file_id, "state": "cold", "job_id": None, "job": None}
            cached = json.loads(row["cached"])
            metadata = json.loads(row["metadata"])
            if "preview_type" not in cached or (metadata.get("kind") == "directory" and "directory_entries" not in cached):
                raise StorageConfigurationError("Preview metadata and directory indexes must be cached before archiving")
            if not manual and self._clock() - max(row["last_access"], row["modified_at"], row["created_at"]) <= self.settings.idle_seconds:
                raise StorageConfigurationError("File is still within its hot retention period")
            existing = db.execute("SELECT kind FROM jobs WHERE file_id=? AND state IN ('queued','running','retry')", (file_id,)).fetchone()
            if existing and existing["kind"] != "archive":
                raise StorageUnavailable("File is currently restoring")
            self._enqueue(db, row, "archive")
        self._wake.set()
        return {"ok": True, **self.storage_info(file_id)}

    def _touch(self, db, row) -> None:
        db.execute("UPDATE files SET last_access=?,access_epoch=access_epoch+1 WHERE file_id=?", (self._clock(), row["file_id"]))
        db.execute("UPDATE jobs SET state='cancelled',error=NULL,lease_owner=NULL WHERE file_id=? AND kind='archive' AND state IN ('queued','running','retry')", (row["file_id"],))
        if row["state"] in {"archiving", "evicting"}:
            db.execute("UPDATE files SET state=?,error=NULL WHERE file_id=?", ("hot_backed" if row["remote_verified"] else "hot", row["file_id"]))

    def touch_content(self, file_id: str) -> None:
        if not self.enabled:
            return
        with self._guard(file_id), self._db(True) as db:
            self._touch(db, self._file(db, file_id))

    def request_restore(self, file_id: str, content_access: bool = True) -> dict:
        self._require_enabled()
        with self._guard(file_id), self._db(True) as db:
            row = self._file(db, file_id)
            if content_access:
                self._touch(db, row)
            if not (self.files_dir / file_id).is_file():
                if not row["remote_verified"]:
                    raise StorageNotFound("File content is missing")
                latest = db.execute("SELECT * FROM jobs WHERE file_id=? AND kind='restore' ORDER BY created_at DESC,rowid DESC LIMIT 1", (file_id,)).fetchone()
                if not latest or latest["state"] not in {"queued", "running"}:
                    temp = self.storage_dir / f".restore-{file_id}-{latest['job_id']}.tmp" if latest else None
                    remaining = max(0, row["size"] - (min(row["size"], temp.stat().st_size) if temp and temp.exists() else 0))
                    # This is only a preflight: the worker makes an atomic persistent
                    # reservation before writing. Reusing its task never reserves twice.
                    pending = db.execute("SELECT COALESCE(SUM(remaining),0) FROM reservations").fetchone()[0]
                    if remaining > self._disk_usage(self.storage_dir).free - self.settings.disk_reserve_bytes - pending:
                        raise StorageCapacityError("Insufficient local storage to restore this file")
                    if latest and latest["state"] == "retry":
                        db.execute("UPDATE jobs SET next_run=?,error=NULL,error_code=NULL WHERE job_id=?", (self._clock(), latest["job_id"]))
                self._enqueue(db, row, "restore")
        self._wake.set()
        return {"ok": True, **self.storage_info(file_id)}

    def cancel_job(self, job_id: str) -> dict:
        self._require_enabled()
        with self._db() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise StorageNotFound("Storage job not found")
            file_id = row["file_id"]
        with self._guard(file_id), self._db(True) as db:
            job = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if job["kind"] != "archive" or job["state"] not in _ACTIVE:
                return {"ok": True, "cancelled": job["state"] == "cancelled", "already_committed": job["state"] == "completed", "job": self._job_info(job)}
            row = self._file(db, file_id)
            db.execute("UPDATE jobs SET state='cancelled',lease_owner=NULL,error=NULL WHERE job_id=?", (job_id,))
            db.execute("UPDATE files SET state=?,error=NULL WHERE file_id=?", ("hot_backed" if row["remote_verified"] else "hot", file_id))
        self._wake.set()
        return {"ok": True, "cancelled": True, **self.storage_info(file_id)}

    @contextmanager
    def open_file(self, file_id: str, access: bool = True, timeout: float | None = None) -> Iterator[BinaryIO]:
        if not _ID.fullmatch(file_id):
            raise StorageNotFound("File not found")
        if not self.enabled:
            try:
                with (self.files_dir / file_id).open("rb") as source:
                    yield source
            except FileNotFoundError as exc:
                raise StorageNotFound("File not found") from exc
            return
        import fcntl
        deadline = time.monotonic() + (self.settings.wait_seconds if timeout is None else timeout)
        touched = False
        source = pin = None
        reader_id = uuid.uuid4().hex
        try:
            while source is None:
                with self._guard(file_id), self._db(True) as db:
                    row = self._file(db, file_id)
                    if access and not touched:
                        self._touch(db, row)
                        touched = True
                    path = self.files_dir / file_id
                    if path.is_file():
                        pin = (self._lock_dir / (file_id + ".pin")).open("a+b")
                        fcntl.flock(pin, fcntl.LOCK_SH)
                        source = path.open("rb")
                        db.execute("INSERT INTO readers VALUES (?,?,?)", (reader_id, file_id, self._owner))
                    elif not access:
                        raise StorageUnavailable("File is cold; content access is required to restore it")
                    elif not row["remote_verified"]:
                        raise StorageNotFound("File content is missing")
                    else:
                        latest = db.execute("SELECT * FROM jobs WHERE file_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1", (file_id,)).fetchone()
                        if latest and latest["kind"] == "restore" and latest["state"] in {"failed", "retry"}:
                            if latest["error_code"] == StorageCapacityError.code:
                                raise StorageCapacityError(latest["error"] or "Insufficient local storage")
                            if latest["state"] == "failed":
                                error_type = StorageConfigurationError if latest["error_code"] == StorageConfigurationError.code else StorageIntegrityError
                                raise error_type(latest["error"] or "Archive restore failed")
                        pending = db.execute("SELECT COALESCE(SUM(remaining),0) FROM reservations").fetchone()[0]
                        if not latest or latest["kind"] != "restore" or latest["state"] not in _ACTIVE:
                            if row["size"] > self._disk_usage(self.storage_dir).free - self.settings.disk_reserve_bytes - pending:
                                raise StorageCapacityError("Insufficient local storage to restore this file")
                        self._enqueue(db, row, "restore")
                if source is None:
                    self._wake.set()
                    if time.monotonic() >= deadline:
                        raise StorageUnavailable("File is restoring; retry shortly")
                    self._stop.wait(min(0.1, max(0, deadline - time.monotonic())))
                    if self._stop.is_set():
                        raise StorageUnavailable("Storage worker is stopping")
            yield source
        finally:
            if source is not None:
                source.close()
            if pin is not None:
                fcntl.flock(pin, fcntl.LOCK_UN)
                pin.close()
            with self._db(True) as db:
                db.execute("DELETE FROM readers WHERE reader_id=?", (reader_id,))

    def reserve_space(self, amount: int) -> SpaceReservation:
        return SpaceReservation(self, amount)

    def _reserve(self, reservation_id: str, amount: int) -> None:
        if self.enabled:
            with self._db(True) as db:
                pending = db.execute("SELECT COALESCE(SUM(remaining),0) FROM reservations").fetchone()[0]
                free = self._disk_usage(self.storage_dir).free
                if amount > free - self.settings.disk_reserve_bytes - pending:
                    raise StorageCapacityError("Insufficient local storage; retry after space is available")
                db.execute("""INSERT INTO reservations VALUES (?,?,?) ON CONFLICT(reservation_id)
                              DO UPDATE SET remaining=remaining+excluded.remaining""", (reservation_id, self._owner, amount))
        else:
            pending = sum(self._disabled_reservations.values())
            if amount > self._disk_usage(self.storage_dir).free - self.settings.disk_reserve_bytes - pending:
                raise StorageCapacityError("Insufficient local storage")
            self._disabled_reservations[reservation_id] = self._disabled_reservations.get(reservation_id, 0) + amount

    def _set_reservation(self, reservation_id: str, remaining: int, remove: bool = False) -> None:
        if self.enabled:
            with self._db(True) as db:
                if remove:
                    db.execute("DELETE FROM reservations WHERE reservation_id=?", (reservation_id,))
                else:
                    db.execute("UPDATE reservations SET remaining=? WHERE reservation_id=?", (remaining, reservation_id))
        elif remove:
            self._disabled_reservations.pop(reservation_id, None)
        else:
            self._disabled_reservations[reservation_id] = remaining

    def scan_idle(self) -> int:
        if not self.enabled:
            return 0
        with self._db() as db:
            ids = [r[0] for r in db.execute("""SELECT file_id FROM files WHERE deleted_at IS NULL
                   AND state IN ('hot','hot_backed') AND MAX(created_at,modified_at,last_access)<?
                   AND NOT EXISTS (SELECT 1 FROM jobs WHERE jobs.file_id=files.file_id AND jobs.state IN ('queued','running','retry'))""",
                                          (self._clock() - self.settings.idle_seconds,))]
        count = 0
        for file_id in ids:
            try:
                self.request_cold(file_id, manual=False)
                count += 1
            except StorageError:
                continue
        return count

    def scan_pressure(self) -> int:
        """Queue a verified, unpinned old cache entry when below the low watermark."""
        if not self.enabled:
            return 0
        import fcntl
        with self._db() as db:
            free = self._disk_usage(self.storage_dir).free
            pending = db.execute("SELECT COALESCE(SUM(remaining),0) FROM reservations").fetchone()[0]
            capacity_need = db.execute("SELECT COALESCE(MAX(files.size),0) FROM jobs JOIN files USING(file_id) WHERE jobs.kind='restore' AND jobs.state='retry' AND jobs.error_code='storage_capacity'").fetchone()[0]
            if free >= self.settings.disk_reserve_bytes and capacity_need <= free - self.settings.disk_reserve_bytes - pending:
                return 0
            if db.execute("SELECT 1 FROM jobs WHERE (kind='archive' AND state IN ('queued','running','retry')) OR (kind='restore' AND (state IN ('queued','running') OR (state='retry' AND COALESCE(error_code,'')<>'storage_capacity'))) LIMIT 1").fetchone():
                return 0
            ids = [r[0] for r in db.execute("SELECT file_id FROM files WHERE deleted_at IS NULL AND state='hot_backed' AND remote_verified=1 ORDER BY last_access")]
        for file_id in ids:
            with (self._lock_dir / (file_id + ".pin")).open("a+b") as pin:
                try:
                    fcntl.flock(pin, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                finally:
                    # Admission is advisory; final eviction acquires the same pin.
                    fcntl.flock(pin, fcntl.LOCK_UN)
            try:
                self.request_cold(file_id, _pressure=True)
                return 1
            except StorageError:
                continue
        return 0

    def delete_file(self, file_id: str) -> dict:
        if not _ID.fullmatch(file_id):
            raise StorageNotFound("File not found")
        if not self.enabled:
            (self.files_dir / file_id).unlink(missing_ok=True)
            (self.meta_dir / (file_id + ".json")).unlink(missing_ok=True)
            return {"ok": True, "file_id": file_id}
        with self._guard(file_id):
            with self._db(True) as db:
                row = db.execute("SELECT * FROM files WHERE file_id=?", (file_id,)).fetchone()
                if row is None:
                    raise StorageNotFound("File not found")
                db.execute("UPDATE files SET state='deleted',deleted_at=COALESCE(deleted_at,?) WHERE file_id=?", (self._clock(), file_id))
                db.execute("UPDATE jobs SET state='cancelled',lease_owner=NULL WHERE file_id=? AND state IN ('queued','running','retry')", (file_id,))
                self._enqueue(db, row, "delete")
            self._remove_local(file_id)
        self._wake.set()
        return {"ok": True, "file_id": file_id}

    def _remove_local(self, file_id: str) -> None:
        (self.files_dir / file_id).unlink(missing_ok=True)
        (self.meta_dir / (file_id + ".json")).unlink(missing_ok=True)
        for path in self.storage_dir.glob(f".restore-{file_id}-*.tmp"):
            path.unlink(missing_ok=True)
        self._sync_dir(self.files_dir)
        self._sync_dir(self.meta_dir)

    @staticmethod
    def _sync_dir(path: Path) -> None:
        handle = os.open(path, os.O_RDONLY)
        try:
            os.fsync(handle)
        finally:
            os.close(handle)

    @staticmethod
    def _fingerprint(path: Path) -> str:
        stat = path.stat()
        return _json([stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns])

    def _checkpoint(self, job_id: str, token: str, *, progress: float | None = None, details: dict | None = None) -> None:
        if self._stop.is_set():
            raise _JobCancelled()
        with self._db(True) as db:
            job = db.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if job is None or job["state"] != "running" or job["lease_owner"] != token:
                raise _JobCancelled()
            if job["kind"] != "delete":
                self._file(db, job["file_id"])
            db.execute("UPDATE jobs SET lease_until=?,progress=?,details=? WHERE job_id=?",
                       (self._clock() + self.settings.task_lease_seconds,
                        progress if progress is not None else job["progress"],
                        _json(details) if details is not None else job["details"], job_id))

    def _remote_call(self, method: str, *args):
        with self._db() as db:
            stored = db.execute("SELECT value FROM runtime WHERE key='circuit'").fetchone()
            circuit = json.loads(stored[0]) if stored else {"failures": 0, "until": 0}
        if circuit["until"] > self._clock():
            raise StorageUnavailable("S3 circuit is temporarily open; retry later")
        try:
            result = getattr(self.remote, method)(*args)
            if method == "list_keys":
                return list(result)
            return result
        except StorageError:
            raise
        except (KeyError, FileNotFoundError) as exc:
            raise StorageIntegrityError("An archive object is missing") from exc
        except Exception as exc:
            raise StorageUnavailable("Remote storage is temporarily unavailable") from exc

    def _upload(self, job, token: str, key: str, data: bytes) -> None:
        """Fence in-flight uploads against remote deletion, including late PUTs."""
        import fcntl
        with (self._lock_dir / (job["file_id"] + ".remote")).open("a+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_SH)
            try:
                self._checkpoint(job["job_id"], token)
                self._remote_call("put", key, data)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _record_circuit(self, success: bool) -> None:
        with self._db(True) as db:
            row = db.execute("SELECT value FROM runtime WHERE key='circuit'").fetchone()
            circuit = json.loads(row[0]) if row else {"failures": 0, "until": 0}
            failures = 0 if success else circuit["failures"] + 1
            until = self._clock() + self.settings.circuit_seconds if failures >= self.settings.circuit_failures else 0
            db.execute("INSERT OR REPLACE INTO runtime VALUES ('circuit',?)", (_json({"failures": failures, "until": until}),))

    def _base(self, file_id: str) -> str:
        return self.settings.prefix + file_id + "/"

    def _aad(self, file_id: str, version: int, generation: str, purpose: str) -> bytes:
        return _json([_FORMAT, file_id, version, generation, purpose]).encode()

    def _unwrap(self, file_id: str, version: int, detail: dict) -> bytes:
        try:
            key_id = detail["key_id"]
            if key_id not in self.settings.keyring:
                raise StorageConfigurationError("The archive's encryption key_id is not installed")
            return self._AESGCM(self.settings.keyring[key_id]).decrypt(
                _unb64(detail["wrap_nonce"]), _unb64(detail["wrapped_key"]),
                self._aad(file_id, version, detail["generation"], "key:" + key_id))
        except StorageConfigurationError:
            raise
        except Exception as exc:
            raise StorageIntegrityError("Archive encryption key authentication failed") from exc

    def _read_manifest(self, row) -> tuple[dict, bytes]:
        data = self._remote_call("get", row["manifest_key"])
        if len(data) > self.settings.max_object_bytes or _sha(data) != row["manifest_sha"]:
            raise StorageIntegrityError("Archive manifest checksum failed")
        try:
            outer = json.loads(data)
            if outer["format"] != _FORMAT or outer["generation"] != row["generation"]:
                raise ValueError("Invalid archive version")
            dek = self._unwrap(row["file_id"], row["version"], outer)
            plain = self._AESGCM(dek).decrypt(_unb64(outer["nonce"]), _unb64(outer["ciphertext"]),
                                             self._aad(row["file_id"], row["version"], row["generation"], "manifest"))
            inner = json.loads(plain)
            self._validate_manifest(row, inner)
            return inner, dek
        except StorageError:
            raise
        except Exception as exc:
            raise StorageIntegrityError("Archive manifest authentication failed") from exc

    def _validate_manifest(self, row, manifest: dict) -> None:
        if manifest.get("file_id") != row["file_id"] or manifest.get("version") != row["version"] or manifest.get("size") != row["size"]:
            raise StorageIntegrityError("Archive identity or size mismatch")
        if manifest.get("sha256") != row["content_sha"]:
            raise StorageIntegrityError("Archive content hash mismatch")
        offset = 0
        chunks = manifest.get("chunks")
        if not isinstance(chunks, list):
            raise StorageIntegrityError("Invalid archive chunk index")
        for index, chunk in enumerate(chunks):
            if (chunk.get("index") != index or chunk.get("offset") != offset
                    or type(chunk.get("size")) is not int or not 0 < chunk["size"] <= 64 * 1024 * 1024
                    or chunk.get("codec") not in {"raw", "zlib"}
                    or not chunk.get("key", "").startswith(self._base(row["file_id"]) + row["generation"] + "/")):
                raise StorageIntegrityError("Invalid archive chunk mapping")
            offset += chunk["size"]
        if offset != row["size"]:
            raise StorageIntegrityError("Archive chunk lengths do not match the file")

    def _decode(self, row, dek: bytes, record: dict) -> bytes:
        data = self._remote_call("get", record["key"])
        if len(data) > self.settings.max_object_bytes or len(data) != record["cipher_size"] or _sha(data) != record["cipher_sha256"]:
            raise StorageIntegrityError("Archive block checksum failed")
        try:
            decoded = self._AESGCM(dek).decrypt(_unb64(record["nonce"]), data,
                        self._aad(row["file_id"], row["version"], row["generation"], "block:" + str(record["index"])))
            if record["codec"] == "zlib":
                decompressor = zlib.decompressobj()
                raw = decompressor.decompress(decoded, record["size"] + 1)
                if not decompressor.eof or decompressor.unused_data or decompressor.unconsumed_tail:
                    raise ValueError("Invalid compressed block")
            else:
                raw = decoded
            if len(raw) != record["size"] or _sha(raw) != record["sha256"]:
                raise ValueError("Invalid raw block")
            return raw
        except Exception as exc:
            raise StorageIntegrityError("Archive block authentication or decompression failed") from exc

    def _archive(self, job, token: str) -> None:
        file_id = job["file_id"]
        path = self.files_dir / file_id
        with self._guard(file_id), self._db(True) as db:
            row = self._file(db, file_id)
            if row["access_epoch"] != job["access_epoch"]:
                raise _JobCancelled()
            db.execute("UPDATE files SET state='archiving',error=NULL WHERE file_id=?", (file_id,))
            row = dict(row)
        if row["remote_verified"]:
            # Reuse existing objects, but do not trust their continued existence
            # or integrity merely because they passed verification previously.
            manifest, dek = self._read_manifest(row)
            digest = hashlib.sha256()
            for chunk in manifest["chunks"]:
                digest.update(self._decode(row, dek, chunk))
                self._checkpoint(job["job_id"], token)
            if digest.hexdigest() != row["content_sha"]:
                raise StorageIntegrityError("Previously verified archive content changed")
            if row["fingerprint"] and self._fingerprint(path) != row["fingerprint"]:
                # A restored local inode has its own fingerprint recorded below.
                raise StorageIntegrityError("Local immutable file changed since its archive was verified")
            self._evict(job, token)
            return
        details = json.loads(job["details"])
        if not details:
            generation = uuid.uuid4().hex
            dek = secrets.token_bytes(32)
            wrap_nonce = secrets.token_bytes(12)
            key_id = self.settings.key_id
            details = {"generation": generation, "key_id": key_id, "wrap_nonce": _b64(wrap_nonce),
                       "wrapped_key": _b64(self._AESGCM(self.settings.keyring[key_id]).encrypt(wrap_nonce, dek,
                                      self._aad(file_id, row["version"], generation, "key:" + key_id))),
                       "fingerprint": self._fingerprint(path), "chunks": []}
            self._checkpoint(job["job_id"], token, details=details)
        else:
            dek = self._unwrap(file_id, row["version"], details)
        if self._fingerprint(path) != details["fingerprint"] or path.stat().st_size != row["size"]:
            raise StorageIntegrityError("Immutable file changed during archiving")
        row["generation"] = details["generation"]
        digest = hashlib.sha256()
        offset = index = 0
        with path.open("rb") as source:
            while raw := source.read(self.settings.block_size):
                self._checkpoint(job["job_id"], token)
                digest.update(raw)
                compressed = zlib.compress(raw)
                codec, payload = ("zlib", compressed) if len(compressed) < len(raw) else ("raw", raw)
                if index < len(details["chunks"]):
                    record = details["chunks"][index]
                    if record["sha256"] != _sha(raw) or record["codec"] != codec:
                        raise StorageIntegrityError("Archive retry source changed")
                    nonce = _unb64(record["nonce"])
                else:
                    nonce = secrets.token_bytes(12)
                    record = {"index": index, "offset": offset, "size": len(raw), "sha256": _sha(raw),
                              "codec": codec, "nonce": _b64(nonce),
                              "key": self._base(file_id) + details["generation"] + f"/block-{index:08d}",
                              "uploaded": False}
                ciphertext = self._AESGCM(dek).encrypt(nonce, payload,
                                self._aad(file_id, row["version"], details["generation"], "block:" + str(index)))
                if len(ciphertext) > self.settings.max_object_bytes:
                    raise StorageConfigurationError("Encrypted archive block exceeds its object limit")
                if "cipher_sha256" in record and record["cipher_sha256"] != _sha(ciphertext):
                    raise StorageIntegrityError("Archive retry cannot reproduce its immutable ciphertext")
                record.update(cipher_sha256=_sha(ciphertext), cipher_size=len(ciphertext))
                if index == len(details["chunks"]):
                    details["chunks"].append(record)
                # Persist nonce and expected ciphertext before any remote write.
                self._checkpoint(job["job_id"], token, details=details)
                if not record["uploaded"]:
                    self._upload(job, token, record["key"], ciphertext)
                    record["uploaded"] = True
                    self._checkpoint(job["job_id"], token, details=details)
                offset += len(raw)
                index += 1
                self._checkpoint(job["job_id"], token, progress=45 * offset / max(1, row["size"]))
        if offset != row["size"] or self._fingerprint(path) != details["fingerprint"]:
            raise StorageIntegrityError("Immutable file changed during archiving")
        manifest = {"format": _FORMAT, "file_id": file_id, "version": row["version"],
                    "size": offset, "sha256": digest.hexdigest(), "chunks": details["chunks"]}
        if "manifest" not in details:
            nonce = secrets.token_bytes(12)
            outer = {key: details[key] for key in ("generation", "key_id", "wrap_nonce", "wrapped_key")}
            outer.update(format=_FORMAT, nonce=_b64(nonce), ciphertext=_b64(self._AESGCM(dek).encrypt(
                         nonce, _json(manifest).encode(), self._aad(file_id, row["version"], details["generation"], "manifest"))))
            details["manifest"] = outer
            self._checkpoint(job["job_id"], token, details=details)
        manifest_data = _json(details["manifest"]).encode()
        if len(manifest_data) > self.settings.max_object_bytes:
            raise StorageConfigurationError("Archive manifest exceeds its object limit")
        manifest_key = self._base(file_id) + details["generation"] + "/manifest.json"
        self._upload(job, token, manifest_key, manifest_data)
        row.update(manifest_key=manifest_key, manifest_sha=_sha(manifest_data), content_sha=digest.hexdigest())
        verified_manifest, verified_dek = self._read_manifest(row)
        verified_hash = hashlib.sha256()
        for record in verified_manifest["chunks"]:
            verified_hash.update(self._decode(row, verified_dek, record))
            self._checkpoint(job["job_id"], token, progress=50 + 45 * (record["offset"] + record["size"]) / max(1, row["size"]))
        if verified_hash.hexdigest() != digest.hexdigest():
            raise StorageIntegrityError("Full archive verification failed")
        with self._guard(file_id):
            self._checkpoint(job["job_id"], token)
            with self._db(True) as db:
                self._file(db, file_id)
                db.execute("""UPDATE files SET state='hot_backed',remote_verified=1,manifest_key=?,
                              manifest_sha=?,generation=?,content_sha=?,fingerprint=? WHERE file_id=?""",
                           (manifest_key, row["manifest_sha"], row["generation"], row["content_sha"], details["fingerprint"], file_id))
        self._evict(job, token)

    def _evict(self, job, token: str) -> None:
        import fcntl
        file_id = job["file_id"]
        with self._guard(file_id), (self._lock_dir / (file_id + ".pin")).open("a+b") as pin:
            self._checkpoint(job["job_id"], token)
            try:
                fcntl.flock(pin, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise _StorageBusy("Waiting for active file readers before eviction")
            try:
                with self._db(True) as db:
                    row = self._file(db, file_id)
                    if not row["remote_verified"] or row["access_epoch"] != job["access_epoch"]:
                        raise _JobCancelled()
                    if row["fingerprint"] != self._fingerprint(self.files_dir / file_id):
                        raise StorageIntegrityError("Local immutable file changed before eviction")
                    db.execute("UPDATE files SET state='evicting' WHERE file_id=?", (file_id,))
                # Guard is shared with content access, cancellation and deletion.
                # The persisted evicting phase makes a crash after unlink repairable.
                (self.files_dir / file_id).unlink()
                self._sync_dir(self.files_dir)
                with self._db(True) as db:
                    self._file(db, file_id)
                    db.execute("UPDATE files SET state='cold',error=NULL WHERE file_id=?", (file_id,))
                    db.execute("UPDATE jobs SET state='completed',progress=100,lease_owner=NULL,error=NULL WHERE job_id=?", (job["job_id"],))
            finally:
                fcntl.flock(pin, fcntl.LOCK_UN)

    def _restore(self, job, token: str) -> None:
        file_id = job["file_id"]
        with self._guard(file_id), self._db(True) as db:
            row = dict(self._file(db, file_id))
            db.execute("UPDATE files SET state='restoring',error=NULL WHERE file_id=?", (file_id,))
        manifest, dek = self._read_manifest(row)
        temp = self.storage_dir / f".restore-{file_id}-{job['job_id']}.tmp"
        # Revalidate every saved local block after restart; truncate any bytes
        # not represented by a fully authenticated completed block.
        digest = hashlib.sha256()
        completed = 0
        if temp.exists():
            with temp.open("r+b") as output:
                for record in manifest["chunks"]:
                    raw = output.read(record["size"])
                    if len(raw) != record["size"] or _sha(raw) != record["sha256"]:
                        break
                    digest.update(raw)
                    completed += 1
                output.truncate(output.tell() if completed == len(manifest["chunks"]) else sum(c["size"] for c in manifest["chunks"][:completed]))
        written = sum(c["size"] for c in manifest["chunks"][:completed])
        with self.reserve_space(row["size"] - written) as reservation:
            with temp.open("ab") as output:
                for record in manifest["chunks"][completed:]:
                    self._checkpoint(job["job_id"], token)
                    raw = self._decode(row, dek, record)
                    self._checkpoint(job["job_id"], token)
                    output.write(raw)
                    output.flush()
                    os.fsync(output.fileno())
                    reservation.consume(len(raw))
                    digest.update(raw)
                    written += len(raw)
                    self._checkpoint(job["job_id"], token, progress=95 * written / max(1, row["size"]), details={"completed_bytes": written})
                output.flush()
                os.fsync(output.fileno())
            if written != row["size"] or digest.hexdigest() != row["content_sha"]:
                raise StorageIntegrityError("Restored file checksum failed")
            actual_hash = hashlib.sha256()
            with temp.open("rb") as source:
                while data := source.read(1024 * 1024):
                    actual_hash.update(data)
            if temp.stat().st_size != row["size"] or actual_hash.hexdigest() != row["content_sha"]:
                raise StorageIntegrityError("Restored temporary file changed before publication")
            with self._guard(file_id):
                self._checkpoint(job["job_id"], token)
                with self._db() as db:
                    self._file(db, file_id)
                os.replace(temp, self.files_dir / file_id)
                self._sync_dir(self.files_dir)
                with self._db(True) as db:
                    self._file(db, file_id)
                    db.execute("UPDATE files SET state='hot_backed',fingerprint=?,error=NULL WHERE file_id=?",
                               (self._fingerprint(self.files_dir / file_id), file_id))
                    db.execute("UPDATE jobs SET state='completed',progress=100,lease_owner=NULL,error=NULL WHERE job_id=?", (job["job_id"],))

    def _delete_remote(self, job, token: str) -> None:
        import fcntl
        with (self._lock_dir / (job["file_id"] + ".remote")).open("a+b") as handle:
            # Wait for every PUT admitted before the tombstone, then enumerate.
            # Later PUTs are rejected by their job checkpoint before transmission.
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                self._remove_local(job["file_id"])
                keys = self._remote_call("list_keys", self._base(job["file_id"]))
                for key in keys:
                    self._checkpoint(job["job_id"], token)
                    self._remote_call("delete", key)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
        with self._db(True) as db:
            db.execute("UPDATE jobs SET state='completed',progress=100,lease_owner=NULL,error=NULL WHERE job_id=? AND lease_owner=?", (job["job_id"], token))

    def run_pending(self, limit: int = 1, *, kinds: tuple[str, ...] | None = None) -> int:
        if not self.enabled:
            return 0
        import fcntl
        count = 0
        skipped = set()
        while count < limit and not self._stop.is_set():
            token = self._owner + ":" + uuid.uuid4().hex
            with self._db() as db:
                if kinds == ("archive",) and db.execute("SELECT 1 FROM jobs WHERE kind='restore' AND (state='running' OR (state IN ('queued','retry') AND next_run<=? AND COALESCE(error_code,'')<>'storage_capacity')) LIMIT 1", (self._clock(),)).fetchone():
                    break
                kind_sql = " AND kind IN (" + ",".join("?" for _ in kinds) + ")" if kinds else ""
                skip_sql = " AND job_id NOT IN (" + ",".join("?" for _ in skipped) + ")" if skipped else ""
                job = db.execute("""SELECT * FROM jobs WHERE ((state IN ('queued','retry') AND next_run<=?)
                             OR (state='running' AND lease_until<=?))""" + kind_sql + skip_sql +
                             " ORDER BY CASE kind WHEN 'restore' THEN 0 WHEN 'delete' THEN 1 ELSE 2 END,created_at LIMIT 1",
                             (self._clock(), self._clock(), *(kinds or ()), *skipped)).fetchone()
                if job is None:
                    break
            handle = (self._lock_dir / ("job-" + job["job_id"] + ".execution")).open("a+b")
            counted = False
            try:
                try:
                    # A lease is scheduling metadata, not permission to share a
                    # mutable temporary file. The OS execution lock fences live
                    # old workers even when their SQLite lease has expired.
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    skipped.add(job["job_id"])
                    continue
                with self._lifecycle_lock:
                    if self._stop.is_set():
                        break
                    self._active_calls += 1
                    counted = True
                with self._db(True) as db:
                    fresh = db.execute("SELECT * FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()
                    eligible = ((fresh["state"] in {"queued", "retry"} and fresh["next_run"] <= self._clock())
                                or (fresh["state"] == "running" and fresh["lease_until"] <= self._clock()))
                    if not eligible:
                        skipped.add(job["job_id"])
                        continue
                    job = fresh
                    db.execute("UPDATE jobs SET state='running',attempts=attempts+1,lease_owner=?,lease_until=?,error=NULL,error_code=NULL WHERE job_id=?",
                               (token, self._clock() + self.settings.task_lease_seconds, job["job_id"]))
                count += 1
                self._execute_job(job, token)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
                handle.close()
                if counted:
                    with self._lifecycle_lock:
                        self._active_calls -= 1
                        if self._active_calls == 0 and self._stop.is_set():
                            self._cleanup_owner()
        return count

    def _execute_job(self, job, token: str) -> None:
        try:
            {"archive": self._archive, "restore": self._restore, "delete": self._delete_remote}[job["kind"]](job, token)
            self._record_circuit(True)
        except (_JobCancelled, StorageNotFound):
            with self._db(True) as db:
                db.execute("UPDATE jobs SET state=?,next_run=?,lease_owner=NULL WHERE job_id=? AND lease_owner=?",
                           ("retry" if self._stop.is_set() else "cancelled", self._clock(), job["job_id"], token))
        except Exception as exc:
            permanent = isinstance(exc, (StorageConfigurationError, StorageIntegrityError))
            error = str(exc) if isinstance(exc, StorageError) else "Local storage operation failed"
            attempts = job["attempts"] + 1
            delay = min(self.settings.retry_max_seconds, self.settings.retry_base_seconds * 2 ** min(attempts - 1, 16))
            delay *= 0.75 + secrets.randbelow(501) / 1000
            with self._db(True) as db:
                changed = db.execute("UPDATE jobs SET state=?,error=?,error_code=?,next_run=?,lease_owner=NULL WHERE job_id=? AND lease_owner=? AND state='running'",
                                    ("failed" if permanent else "retry", error,
                                     exc.code if isinstance(exc, StorageError) else "local_storage_error",
                                     self._clock() + delay, job["job_id"], token)).rowcount
                if changed and job["kind"] != "delete":
                    row = db.execute("SELECT * FROM files WHERE file_id=?", (job["file_id"],)).fetchone()
                    if row is not None and row["deleted_at"] is None:
                        local = (self.files_dir / job["file_id"]).is_file()
                        state = ("hot_backed" if row["remote_verified"] else "hot") if local else "cold"
                        db.execute("UPDATE files SET state=?,error=? WHERE file_id=?", (state, error, job["file_id"]))
            if isinstance(exc, StorageUnavailable) and not isinstance(exc, (StorageIntegrityError, _StorageBusy)):
                self._record_circuit(False)
            LOG.warning("Storage %s job failed for %s: %s", job["kind"], job["file_id"], error)

    def cleanup_orphans(self, limit: int = 1) -> int:
        if not self.enabled:
            return 0
        import fcntl
        with self._db() as db:
            jobs = [dict(row) for row in db.execute("SELECT * FROM jobs WHERE kind='archive' AND state IN ('failed','cancelled') ORDER BY created_at")]
        cleaned = 0
        for job in jobs:
            details = json.loads(job["details"])
            generation = details.get("generation")
            if not generation or details.get("garbage_collected"):
                continue
            with (self._lock_dir / (job["file_id"] + ".remote")).open("a+b") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                try:
                    with self._db() as db:
                        row = db.execute("SELECT generation FROM files WHERE file_id=?", (job["file_id"],)).fetchone()
                        if row and row["generation"] == generation:
                            continue
                    for key in self._remote_call("list_keys", self._base(job["file_id"]) + generation + "/"):
                        self._remote_call("delete", key)
                    details["garbage_collected"] = True
                    with self._db(True) as db:
                        db.execute("UPDATE jobs SET details=? WHERE job_id=? AND state IN ('failed','cancelled')", (_json(details), job["job_id"]))
                    cleaned += 1
                except StorageError:
                    continue
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)
            if cleaned >= limit:
                break
        return cleaned

    def reap_dead_owners(self) -> None:
        if not self.enabled:
            return
        with self._db(True) as db:
            owners = db.execute("SELECT * FROM owners WHERE owner<>?", (self._owner,)).fetchall()
            import fcntl
            for owner in owners:
                handle = (self._lock_dir / (owner["owner"] + ".owner")).open("a+b")
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    handle.close()
                    continue
                try:
                    db.execute("UPDATE jobs SET state='retry',next_run=?,lease_owner=NULL,lease_until=0 WHERE state='running' AND lease_owner LIKE ?", (self._clock(), owner["owner"] + ":%"))
                    db.execute("DELETE FROM reservations WHERE owner=?", (owner["owner"],))
                    db.execute("DELETE FROM readers WHERE owner=?", (owner["owner"],))
                    db.execute("DELETE FROM owners WHERE owner=?", (owner["owner"],))
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                    handle.close()
    def reconcile(self) -> None:
        if not self.enabled:
            return
        self.reap_dead_owners()
        with self._db() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM files")]
        for row in rows:
            file_id = row["file_id"]
            with self._guard(file_id):
                if row["deleted_at"] is not None:
                    self._remove_local(file_id)
                    with self._db(True) as db:
                        last = db.execute("SELECT * FROM jobs WHERE file_id=? AND kind='delete' ORDER BY created_at DESC,rowid DESC LIMIT 1", (file_id,)).fetchone()
                        if last and last["state"] == "failed":
                            # A startup is an explicit new configuration attempt;
                            # permanent credential failures do not spin meanwhile.
                            db.execute("UPDATE jobs SET state='retry',next_run=?,error=NULL,error_code=NULL WHERE job_id=?", (self._clock(), last["job_id"]))
                        elif last is None or last["state"] == "cancelled":
                            self._enqueue(db, row, "delete")
                    continue
                path = self.files_dir / file_id
                if row["state"] == "evicting":
                    with self._db(True) as db:
                        if row["remote_verified"]:
                            db.execute("UPDATE files SET state=? WHERE file_id=?", ("hot_backed" if path.exists() else "cold", file_id))
                            if not path.exists():
                                db.execute("UPDATE jobs SET state='completed',progress=100,lease_owner=NULL WHERE file_id=? AND kind='archive' AND state IN ('queued','running','retry')", (file_id,))
                elif row["state"] == "restoring" and path.exists():
                    digest = hashlib.sha256()
                    with path.open("rb") as source:
                        while data := source.read(1024 * 1024):
                            digest.update(data)
                    if path.stat().st_size == row["size"] and digest.hexdigest() == row["content_sha"]:
                        with self._db(True) as db:
                            db.execute("UPDATE files SET state='hot_backed',fingerprint=?,error=NULL WHERE file_id=?", (self._fingerprint(path), file_id))
                            db.execute("UPDATE jobs SET state='completed',progress=100,lease_owner=NULL WHERE file_id=? AND kind='restore' AND state IN ('queued','running','retry')", (file_id,))
        # Import only ordinary JSON upload metadata; cached indexes are supplied
        # by the application after startup. Tombstones are never re-imported.
        for path in self.meta_dir.glob("*.json"):
            try:
                metadata = json.loads(path.read_text(encoding="utf-8"))
                if metadata.get("file_id") == path.stem and (self.files_dir / path.stem).is_file():
                    self.register_file(metadata)
            except (StorageError, OSError, ValueError, KeyError, TypeError):
                continue

    def start_worker(self) -> None:
        if not self.enabled or self._threads:
            return
        self._stop.clear()

        def worker(kinds):
            while not self._stop.is_set():
                worked = self.run_pending(kinds=kinds)
                if not worked:
                    self._wake.wait(0.5)
                    self._wake.clear()

        def maintenance():
            last_scan = 0
            while not self._stop.wait(1):
                now = self._clock()
                with self._db(True) as db:
                    db.execute("UPDATE owners SET heartbeat=? WHERE owner=?", (now, self._owner))
                self.reap_dead_owners()
                if now - last_scan >= self.settings.scan_seconds:
                    self.scan_pressure()
                    self.scan_idle()
                    self.cleanup_orphans()
                    last_scan = now

        for target, args, name in ((worker, (("restore", "delete"),), "fluxdrop-restore"),
                                   (worker, (("archive",),), "fluxdrop-archive"),
                                   (maintenance, (), "fluxdrop-storage-maintenance")):
            thread = threading.Thread(target=target, args=args, name=name, daemon=True)
            self._threads.append(thread)
            thread.start()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=self.settings.read_timeout + self.settings.connect_timeout + 2)
        alive = [thread for thread in self._threads if thread.is_alive()]
        self._threads = alive
        with self._lifecycle_lock:
            if self.enabled and not self._active_calls:
                self._cleanup_owner()

    def _cleanup_owner(self) -> None:
        if not self._owner_closed:
            with self._db(True) as db:
                db.execute("DELETE FROM reservations WHERE owner=?", (self._owner,))
                db.execute("DELETE FROM readers WHERE owner=?", (self._owner,))
                db.execute("DELETE FROM owners WHERE owner=?", (self._owner,))
                db.execute("UPDATE jobs SET state='retry',next_run=?,lease_owner=NULL,lease_until=0 WHERE state='running' AND lease_owner LIKE ?", (self._clock(), self._owner + ":%"))
            import fcntl
            if not self._owner_lock.closed:
                fcntl.flock(self._owner_lock, fcntl.LOCK_UN)
                self._owner_lock.close()
            self._owner_closed = True
