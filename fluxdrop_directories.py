"""Safe, bounded-memory directory archives; uploaded files are never extracted."""

from __future__ import annotations

import re
import shutil
import stat
import struct
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from fluxdrop_multipart import read_multipart_files


MAX_DIRECTORY_ENTRIES = 10_000
MAX_DIRECTORY_METADATA_BYTES = 64 * 1024 * 1024
CHUNK_SIZE = 1024 * 1024


def validate_relative_path(value: str, *, allow_empty: bool = False, directory: bool = False) -> str:
    """Validate a literal POSIX path, without decoding or normalizing it."""
    if not isinstance(value, str):
        raise ValueError("Invalid directory entry path")
    if not value and allow_empty:
        return ""
    if not value or "\\" in value or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise ValueError("Directory entry paths must be relative")
    if any(ord(char) < 32 or 127 <= ord(char) < 160 for char in value):
        raise ValueError("Directory entry paths cannot contain control characters")
    if directory and value.endswith("/"):
        value = value[:-1]
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("Directory entry paths cannot contain empty, '.' or '..' segments")
    try:
        if len(value.encode("utf-8")) > 4096 or any(len(part.encode("utf-8")) > 255 for part in parts):
            raise ValueError("Directory entry path is too long")
    except UnicodeError as exc:
        raise ValueError("Directory entry path must be valid Unicode") from exc
    return value


@dataclass
class DirectorySummary:
    filename: str
    size: int
    file_count: int


class _DirectoryIndex:
    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self.root = ""
        self.kinds: dict[str, str] = {}
        self.explicit: set[str] = set()
        self.size = 0
        self.file_count = 0

    def add(self, name: str, directory: bool, size: int) -> str:
        path = validate_relative_path(name, directory=directory)
        parts = path.split("/")
        root = parts[0]
        if self.root and self.root != root:
            raise ValueError("A directory upload must contain exactly one root folder")
        self.root = root
        if len(parts) == 1 and not directory:
            raise ValueError("Directory files must be inside a root folder")
        kind = "directory" if directory else "file"
        if path in self.explicit or (path in self.kinds and self.kinds[path] != kind):
            raise ValueError("Directory contains duplicate or conflicting paths")
        for index in range(1, len(parts)):
            parent = "/".join(parts[:index])
            if self.kinds.get(parent) == "file":
                raise ValueError("A file cannot also be a parent directory")
            self.kinds[parent] = "directory"
        self.kinds[path] = kind
        self.explicit.add(path)
        if len(self.kinds) > MAX_DIRECTORY_ENTRIES:
            raise ValueError("Directory contains too many entries")
        if directory and size:
            raise ValueError("Directory entries must have empty content")
        if not directory:
            self.size += size
            self.file_count += 1
            if self.size > self.max_bytes:
                raise OverflowError("Expanded directory is larger than the configured upload limit")
        return path

    def summary(self) -> DirectorySummary:
        if not self.root:
            raise ValueError("Directory archive is empty; include a root folder entry")
        return DirectorySummary(self.root, self.size, self.file_count)


def _check_archive_metadata(path: Path) -> None:
    """Bound central-directory allocation before ``ZipFile`` reads its index.

    Walk the fixed-size central headers on disk as well as checking the stated
    count: ZIP readers do not necessarily enforce the count in the end record.
    ZIP64 uses the fixed end record also supported by the standard ZIP reader.
    """
    with path.open("rb") as source:
        source.seek(0, 2)
        size = source.tell()
        tail_size = min(size, 65535 + 22)
        source.seek(size - tail_size)
        tail = source.read(tail_size)
        offset = tail.rfind(b"PK\x05\x06")
        if offset < 0 or len(tail) - offset < 22:
            raise ValueError("Invalid directory ZIP archive")
        end = struct.unpack_from("<4s4H2LH", tail, offset)
        if offset + 22 + end[7] != len(tail) or end[1] or end[2]:
            raise ValueError("Invalid or multipart directory ZIP archive")
        entries, metadata_size = end[4], end[5]
        end_offset = size - tail_size + offset
        metadata_end = end_offset
        if end_offset >= 76:
            source.seek(end_offset - 20)
            locator = source.read(20)
            if locator.startswith(b"PK\x06\x07"):
                _, disk, _, disks = struct.unpack("<4sLQL", locator)
                source.seek(end_offset - 76)
                raw_zip64 = source.read(56)
                if raw_zip64.startswith(b"PK\x06\x06"):
                    record = struct.unpack("<4sQ2H2L4Q", raw_zip64)
                    if disk or disks > 1 or record[4] or record[5]:
                        raise ValueError("Multipart ZIP archives are not supported")
                    entries, metadata_size = record[7], record[8]
                    metadata_end -= 76
        if entries > MAX_DIRECTORY_ENTRIES:
            raise ValueError("Directory contains too many entries")
        if metadata_size > MAX_DIRECTORY_METADATA_BYTES:
            raise ValueError("Directory archive metadata is too large")
        if metadata_size > metadata_end:
            raise ValueError("Invalid directory ZIP archive metadata")
        source.seek(metadata_end - metadata_size)
        consumed = count = 0
        while consumed < metadata_size:
            header = source.read(46)
            if len(header) != 46 or not header.startswith(b"PK\x01\x02"):
                raise ValueError("Invalid directory ZIP archive metadata")
            count += 1
            if count > MAX_DIRECTORY_ENTRIES:
                raise ValueError("Directory contains too many entries")
            variable_size = sum(struct.unpack_from("<3H", header, 28))
            consumed += 46 + variable_size
            if consumed > metadata_size:
                raise ValueError("Invalid directory ZIP archive metadata")
            source.seek(variable_size, 1)


def validate_directory_archive(path: Path, max_bytes: int) -> DirectorySummary:
    """Check paths, entry types, total expanded bytes and CRCs without extraction."""
    index = _DirectoryIndex(max_bytes)
    try:
        _check_archive_metadata(path)
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_DIRECTORY_ENTRIES:
                raise ValueError("Directory contains too many entries")
            for info in entries:
                # ZipInfo truncates filenames at NUL; inspect the original too.
                if info.orig_filename != info.filename:
                    raise ValueError("Directory entry path contains a NUL character")
                directory = info.is_dir()
                mode = (info.external_attr >> 16) & 0xFFFF
                file_type = stat.S_IFMT(mode)
                expected = stat.S_IFDIR if directory else stat.S_IFREG
                if file_type not in {0, expected}:
                    raise ValueError("Directory archives cannot contain symlinks or special files")
                if info.flag_bits & 1:
                    raise ValueError("Encrypted directory archives are not supported")
                # LZMA headers can request an arbitrarily large decoder dictionary.
                # Standard stored/deflated ZIPs have predictable decoder memory.
                if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                    raise ValueError("Directory ZIP entries must use stored or deflate compression")
                index.add(info.filename, directory, info.file_size)
            summary = index.summary()
            # Read all entries, including empty ones, to verify CRCs and headers.
            expanded = 0
            for info in entries:
                with archive.open(info) as source:
                    while chunk := source.read(CHUNK_SIZE):
                        expanded += len(chunk)
                        if expanded > max_bytes:
                            raise OverflowError("Expanded directory is larger than the configured upload limit")
            return summary
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError, zlib.error) as exc:
        raise ValueError("Invalid or unsupported directory ZIP archive") from exc


def read_multipart_directory(
    source: BinaryIO,
    target_path: Path,
    content_length: int,
    content_type: str,
    max_bytes: int,
) -> DirectorySummary:
    index = _DirectoryIndex(max_bytes)
    with zipfile.ZipFile(target_path, "w", compression=zipfile.ZIP_STORED) as archive:
        def consume(name: str, stream: BinaryIO, size: int) -> None:
            directory = name.endswith("/")
            path = index.add(name, directory, size)
            info = zipfile.ZipInfo(path + ("/" if directory else ""))
            info.external_attr = (0o40700 if directory else 0o100600) << 16
            info.file_size = size
            with archive.open(info, "w", force_zip64=True) as target:
                shutil.copyfileobj(stream, target, length=CHUNK_SIZE)
        read_multipart_files(
            source, content_length, content_type, consume,
            temp_dir=target_path.parent, max_parts=MAX_DIRECTORY_ENTRIES,
        )
    return index.summary()
