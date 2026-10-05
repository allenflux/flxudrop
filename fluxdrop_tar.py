"""Stream a tar or tar.gz directory into the service's validated ZIP format."""

from __future__ import annotations

import gzip
import os
import tarfile
import zipfile
import zlib
from contextlib import ExitStack
from pathlib import Path

from fluxdrop_directories import (
    CHUNK_SIZE,
    MAX_DIRECTORY_ENTRIES,
    MAX_DIRECTORY_METADATA_BYTES,
    _DirectoryIndex,
    validate_directory_archive,
)


MAX_TAR_EXTENDED_HEADER_BYTES = 64 * 1024
MAX_TAR_RECORDS = MAX_DIRECTORY_ENTRIES * 3


class _LimitedTarStream:
    def __init__(self, source, max_bytes: int):
        self.source = source
        self.remaining = max_bytes
        self.total_read = 0
        self.last_nonzero = -1

    def read(self, size: int) -> bytes:
        if size < 0:
            raise ValueError("Unbounded archive reads are not supported")
        chunk = self.source.read(min(size, self.remaining + 1))
        self.remaining -= len(chunk)
        if self.remaining < 0:
            raise OverflowError("Expanded directory archive is larger than the configured limit")
        # bytes.rstrip performs this scan in C, with at most one chunk retained.
        nonzero_prefix = chunk.rstrip(b"\0")
        if nonzero_prefix:
            self.last_nonzero = self.total_read + len(nonzero_prefix) - 1
        self.total_read += len(chunk)
        return chunk


def convert_tar_directory(source_path: Path, target_path: Path, max_bytes: int):
    index = _DirectoryIndex(max_bytes)
    records = metadata_bytes = 0
    content_end = 0

    class BoundedTarInfo(tarfile.TarInfo):
        @classmethod
        def frombuf(cls, buf, encoding, errors):
            return cls._checked_frombuf(buf, encoding, errors)

        @classmethod
        def _frombuf(cls, buf, encoding, errors, *, dircheck=True):
            # Newer Python security releases call this private decoder directly,
            # including from recursive PAX/GNU extended-header processing.
            return cls._checked_frombuf(buf, encoding, errors, dircheck=dircheck)

        @classmethod
        def _checked_frombuf(cls, buf, encoding, errors, *, dircheck=True):
            nonlocal records, metadata_bytes
            if len(buf) != tarfile.BLOCKSIZE:
                raise ValueError("Directory tar archive is truncated")
            if buf == b"\0" * tarfile.BLOCKSIZE:
                raise tarfile.EOFHeaderError("End of tar archive")
            try:
                decoder = getattr(super(), "_frombuf", None)
                if decoder is None:
                    info = super().frombuf(buf, encoding, errors)
                else:
                    info = decoder(buf, encoding, errors, dircheck=dircheck)
            except tarfile.HeaderError as exc:
                # TarFile.next otherwise tolerates invalid headers after a file.
                raise ValueError("Invalid directory tar header") from exc
            records += 1
            metadata_bytes += tarfile.BLOCKSIZE
            if records > MAX_TAR_RECORDS:
                raise ValueError("Directory tar archive contains too many records")
            if info.size < 0:
                raise ValueError("Invalid directory tar entry size")
            if info.type in {
                tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.SOLARIS_XHDTYPE,
                tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK,
            }:
                if info.size > MAX_TAR_EXTENDED_HEADER_BYTES:
                    raise ValueError("Directory tar extended header is too large")
                metadata_bytes += (info.size + 511) // 512 * 512
            elif info.type not in {tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE}:
                raise ValueError("Directory archives cannot contain links, sparse files or special files")
            if metadata_bytes > MAX_DIRECTORY_METADATA_BYTES:
                raise ValueError("Directory archive metadata is too large")
            return info

        def _proc_gnusparse_00(self, *args):
            raise ValueError("Sparse directory archive entries are not supported")

        def _proc_gnusparse_01(self, *args):
            raise ValueError("Sparse directory archive entries are not supported")

        def _proc_gnusparse_10(self, *args):
            raise ValueError("Sparse directory archive entries are not supported")

    try:
        with ExitStack() as stack:
            raw = stack.enter_context(source_path.open("rb"))
            signature = raw.read(2)
            raw.seek(0)
            decoded = stack.enter_context(gzip.GzipFile(fileobj=raw)) if signature == b"\x1f\x8b" else raw
            bounded = _LimitedTarStream(decoded, max_bytes + MAX_DIRECTORY_METADATA_BYTES)
            archive = stack.enter_context(tarfile.open(fileobj=bounded, mode="r|", tarinfo=BoundedTarInfo))
            output = stack.enter_context(zipfile.ZipFile(target_path, "w", compression=zipfile.ZIP_STORED))
            for member in archive:
                if member.size < 0:
                    raise ValueError("Invalid directory tar entry size")
                if member.sparse is not None or any("sparse" in key.lower() for key in member.pax_headers):
                    raise ValueError("Sparse directory archive entries are not supported")
                if member.type not in {tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE}:
                    raise ValueError("Directory archives cannot contain links or special files")
                # Native `tar ... ./folder` emits this prefix; other path segments
                # stay literal and pass through the shared traversal checks.
                name = member.name.removeprefix("./")
                path = index.add(name, member.isdir(), member.size)
                content_end = member.offset_data + (member.size + 511) // 512 * 512
                info = zipfile.ZipInfo(path + ("/" if member.isdir() else ""))
                info.external_attr = (0o40700 if member.isdir() else 0o100600) << 16
                info.file_size = member.size
                with output.open(info, "w", force_zip64=True) as target:
                    if member.isfile():
                        source = archive.extractfile(member)
                        if source is None:
                            raise ValueError("Could not read directory tar entry")
                        with source:
                            remaining = member.size
                            while remaining:
                                chunk = source.read(min(CHUNK_SIZE, remaining))
                                if not chunk:
                                    raise ValueError("Directory tar archive is truncated")
                                target.write(chunk)
                                remaining -= len(chunk)
                # TarFile versions before 3.13 cache stream entries by default.
                archive.members.clear()
            while archive.fileobj.read(CHUNK_SIZE):
                pass
            # Validate raw stream offsets rather than relying on which tarfile
            # decoder callback observes EOF in a particular Python release.
            padding = bounded.total_read - content_end
            if bounded.last_nonzero >= content_end:
                raise ValueError("Unexpected data after directory tar end marker")
            if padding < 2 * tarfile.BLOCKSIZE or padding % tarfile.BLOCKSIZE:
                raise ValueError("Directory tar archive is missing its complete end marker")
            # Draining the tar stream also drains gzip through its CRC/footer.
            return index.summary()
    except (tarfile.TarError, gzip.BadGzipFile, EOFError, zlib.error, RecursionError) as exc:
        raise ValueError("Invalid or truncated directory tar archive") from exc


def prepare_directory_archive(source_path: Path, converted_path: Path, max_bytes: int):
    """Preserve ZIPs, or convert streamed tar archives without extracting paths."""
    try:
        is_zip = zipfile.is_zipfile(source_path)
    except zipfile.BadZipFile as exc:
        raise ValueError("Invalid directory ZIP archive") from exc
    if is_zip:
        return validate_directory_archive(source_path, max_bytes)
    summary = convert_tar_directory(source_path, converted_path, max_bytes)
    os.replace(converted_path, source_path)
    return summary
