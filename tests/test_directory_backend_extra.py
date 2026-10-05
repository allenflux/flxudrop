from __future__ import annotations

import io
import gzip
import struct
import tarfile
import tempfile
import unittest
import zipfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fluxdrop_directories import validate_directory_archive
from fluxdrop_tar import convert_tar_directory
from app import FluxDropConfig, FluxDropHandler, read_chunked_to_file


def archive_bytes(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return output.getvalue()


class DirectoryArchiveBoundsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "upload.zip"

    def validate(self, raw):
        self.path.write_bytes(raw)
        return validate_directory_archive(self.path, 1024 * 1024)

    def test_declared_entry_count_is_rejected_before_zipfile_allocates(self):
        raw = bytearray(archive_bytes([("root/file.txt", b"data")]))
        struct.pack_into("<H", raw, len(raw) - 22 + 10, 10001)
        with mock.patch("fluxdrop_directories.zipfile.ZipFile", side_effect=AssertionError("Must preflight first")):
            with self.assertRaisesRegex(ValueError, "too many entries"):
                self.validate(raw)

    def test_declared_metadata_size_is_rejected_before_allocation(self):
        raw = bytearray(archive_bytes([("root/file.txt", b"data")]))
        struct.pack_into("<L", raw, len(raw) - 22 + 12, 64 * 1024 * 1024 + 1)
        with mock.patch("fluxdrop_directories.zipfile.ZipFile", side_effect=AssertionError("Must preflight first")):
            with self.assertRaisesRegex(ValueError, "metadata is too large"):
                self.validate(raw)

    def test_actual_central_headers_are_counted_even_with_forged_low_count(self):
        raw = bytearray(archive_bytes([("root/a", b"a"), ("root/b", b"b")]))
        struct.pack_into("<HH", raw, len(raw) - 22 + 8, 0, 0)
        with mock.patch("fluxdrop_directories.MAX_DIRECTORY_ENTRIES", 1):
            with mock.patch("fluxdrop_directories.zipfile.ZipFile", side_effect=AssertionError("Must preflight first")):
                with self.assertRaisesRegex(ValueError, "too many entries"):
                    self.validate(raw)

    def test_zip64_end_records_keep_valid_archives_supported(self):
        raw = archive_bytes([("root/file.txt", b"data")])
        end = struct.unpack("<4s4H2LH", raw[-22:])
        zip64 = struct.pack("<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, end[3], end[4], end[5], end[6])
        locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, len(raw) - 22, 1)
        conventional = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 65535, 65535, 0xFFFFFFFF, 0xFFFFFFFF, 0)
        summary = self.validate(raw[:-22] + zip64 + locator + conventional)
        self.assertEqual((summary.filename, summary.size, summary.file_count), ("root", 4, 1))

    def test_corrupt_deflate_payload_becomes_validation_error(self):
        raw = bytearray(archive_bytes([("root/file.txt", b"data" * 100)]))
        filename_size, extra_size = struct.unpack_from("<HH", raw, 26)
        start = 30 + filename_size + extra_size
        raw[start] = 0xFF
        with self.assertRaises(ValueError):
            self.validate(raw)

    def test_unbounded_decoder_formats_are_rejected_before_reading(self):
        raw = bytearray(archive_bytes([("root/file.txt", b"data")]))
        central = raw.index(b"PK\x01\x02")
        struct.pack_into("<H", raw, 8, zipfile.ZIP_LZMA)
        struct.pack_into("<H", raw, central + 10, zipfile.ZIP_LZMA)
        with self.assertRaisesRegex(ValueError, "stored or deflate"):
            self.validate(raw)


def tar_bytes(entries, *, compressed=False):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for info, content in entries:
            if isinstance(info, str):
                name = info
                info = tarfile.TarInfo(name)
                if name.endswith("/"):
                    info.type = tarfile.DIRTYPE
                info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    raw = output.getvalue()
    return gzip.compress(raw) if compressed else raw


@contextmanager
def private_tar_decoder_dispatch():
    """Exercise the decoder dispatch used by newer Python security releases."""
    original_private = getattr(tarfile.TarInfo, "_frombuf", None)
    original_public = tarfile.TarInfo.frombuf.__func__

    def private_decoder(cls, buf, encoding, errors, *, dircheck=True):
        if original_private is not None:
            return original_private.__func__(cls, buf, encoding, errors, dircheck=dircheck)
        return original_public(cls, buf, encoding, errors)

    def from_archive(cls, archive):
        buf = archive.fileobj.read(tarfile.BLOCKSIZE)
        # EOF detection must remain correct even if no decoder sees this block.
        if buf == b"\0" * tarfile.BLOCKSIZE:
            raise tarfile.EOFHeaderError("End of tar archive")
        info = cls._frombuf(buf, archive.encoding, archive.errors, dircheck=True)
        info.offset = archive.fileobj.tell() - tarfile.BLOCKSIZE
        return info._proc_member(archive)

    with mock.patch.object(tarfile.TarInfo, "_frombuf", classmethod(private_decoder), create=True):
        with mock.patch.object(tarfile.TarInfo, "fromtarfile", classmethod(from_archive)):
            yield


class DirectoryTarStreamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = Path(self.temp.name) / "upload.tar"
        self.target = Path(self.temp.name) / "directory.zip"

    def convert(self, raw, limit=1024 * 1024):
        self.source.write_bytes(raw)
        return convert_tar_directory(self.source, self.target, limit)

    def test_tar_and_gzip_preserve_arbitrary_unicode_names_and_empty_dirs(self):
        entries = [("./工作资料/", b""), ("./工作资料/空目录/", b""), ("./工作资料/sub/结果.txt", "你好".encode())]
        for compressed in (False, True):
            summary = self.convert(tar_bytes(entries, compressed=compressed))
            self.assertEqual((summary.filename, summary.file_count), ("工作资料", 1))
            with zipfile.ZipFile(self.target) as archive:
                self.assertIn("工作资料/空目录/", archive.namelist())
                self.assertEqual(archive.read("工作资料/sub/结果.txt"), "你好".encode())

    def test_tar_paths_duplicates_links_and_special_files_are_rejected(self):
        invalid = [
            [("./../escape", b"x")], [("/root/a", b"x")], [("root/../a", b"x")],
            [("root\\a", b"x")], [("root/a", b"x"), ("other/b", b"x")],
            [("root/a", b"x"), ("root/a", b"x")],
        ]
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE, tarfile.GNUTYPE_SPARSE):
            info = tarfile.TarInfo("root/special")
            info.type = kind
            info.linkname = "../../secret"
            invalid.append([(info, b"")])
        for entries in invalid:
            with self.subTest(entries=str(entries)[:90]):
                with self.assertRaises(ValueError):
                    self.convert(tar_bytes(entries))

    def test_expanded_limit_is_enforced_before_copying_file(self):
        with self.assertRaises(OverflowError):
            self.convert(tar_bytes([("root/file", b"x" * 2048)], compressed=True), 1024)

    def test_tar_end_marker_and_gzip_footer_are_required(self):
        raw = tar_bytes([("root/file", b"abc")])
        bad_gzip_crc = bytearray(gzip.compress(raw))
        bad_gzip_crc[-8] ^= 1
        for broken in (raw[:1024], raw[:1536], gzip.compress(raw)[:-8], bad_gzip_crc):
            with self.subTest(length=len(broken)):
                with self.assertRaises(ValueError):
                    self.convert(broken)

    def test_nonzero_trailing_data_and_invalid_headers_are_rejected(self):
        raw = bytearray(tar_bytes([("root/file", b"abc")]))
        raw[2048] = 1
        with self.assertRaises(ValueError):
            self.convert(raw)
        raw = bytearray(tar_bytes([("root/file", b"abc"), ("root/file2", b"def")]))
        raw[1024] ^= 1
        with self.assertRaises(ValueError):
            self.convert(raw)

    def test_extended_headers_are_limited_before_tarfile_reads_them(self):
        for kind in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME):
            info = tarfile.TarInfo("metadata")
            info.type = kind
            info.size = 64 * 1024 + 1
            with self.assertRaisesRegex(ValueError, "extended header is too large"):
                self.convert(info.tobuf(format=tarfile.USTAR_FORMAT))

    def test_tar_record_limit_and_pax_sparse_are_rejected(self):
        with mock.patch("fluxdrop_tar.MAX_TAR_RECORDS", 1):
            with self.assertRaisesRegex(ValueError, "too many records"):
                self.convert(tar_bytes([("root/a", b"a"), ("root/b", b"b")]))
        info = tarfile.TarInfo("root/sparse")
        info.pax_headers = {"GNU.sparse.major": "1", "GNU.sparse.minor": "0"}
        with self.assertRaisesRegex(ValueError, "Sparse"):
            self.convert(tar_bytes([(info, b"")]))

    def test_pax_cannot_override_file_size_with_a_negative_value(self):
        info = tarfile.TarInfo("root/file")
        info.pax_headers = {"size": "-1"}
        with self.assertRaises(ValueError):
            self.convert(tar_bytes([(info, b"")]))

    def test_private_decoder_dispatch_keeps_end_markers_and_counts_each_header_once(self):
        with private_tar_decoder_dispatch(), mock.patch("fluxdrop_tar.MAX_TAR_RECORDS", 1):
            summary = self.convert(tar_bytes([("root/file.txt", b"data")], compressed=True))
        self.assertEqual((summary.filename, summary.file_count, summary.size), ("root", 1, 4))

    def test_private_decoder_dispatch_preserves_metadata_and_record_limits(self):
        info = tarfile.TarInfo("metadata")
        info.type = tarfile.XHDTYPE
        info.size = 64 * 1024 + 1
        with private_tar_decoder_dispatch():
            with self.assertRaisesRegex(ValueError, "extended header is too large"):
                self.convert(info.tobuf(format=tarfile.USTAR_FORMAT))
            with mock.patch("fluxdrop_tar.MAX_TAR_RECORDS", 1):
                with self.assertRaisesRegex(ValueError, "too many records"):
                    self.convert(tar_bytes([("root/a", b"a"), ("root/b", b"b")]))

    def test_private_decoder_dispatch_preserves_valid_pax_and_gnu_long_names(self):
        name = "任意目录/" + "segment" * 20 + "/结果.txt"
        for archive_format in (tarfile.PAX_FORMAT, tarfile.GNU_FORMAT):
            raw = io.BytesIO()
            with tarfile.open(fileobj=raw, mode="w", format=archive_format) as archive:
                info = tarfile.TarInfo(name)
                info.size = 4
                archive.addfile(info, io.BytesIO(b"data"))
            with self.subTest(format=archive_format):
                with private_tar_decoder_dispatch(), mock.patch("fluxdrop_tar.MAX_TAR_RECORDS", 2):
                    summary = self.convert(gzip.compress(raw.getvalue()))
                self.assertEqual((summary.filename, summary.file_count), ("任意目录", 1))
                with zipfile.ZipFile(self.target) as archive:
                    self.assertEqual(archive.read(name), b"data")

    def test_private_decoder_dispatch_rejects_truncated_and_nonzero_trailers(self):
        raw = tar_bytes([("root/file", b"abc")])
        nonzero = bytearray(raw)
        nonzero[2048] = 1
        with private_tar_decoder_dispatch():
            for broken in (raw[:1024], raw[:1536], nonzero, gzip.compress(raw)[:-8]):
                with self.subTest(length=len(broken)):
                    with self.assertRaises(ValueError):
                        self.convert(broken)


class ChunkedDirectoryBodyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "upload.tmp"

    def decode(self, raw, limit=1024):
        return read_chunked_to_file(io.BytesIO(raw), self.path, limit)

    def test_chunks_extensions_and_bounded_trailers(self):
        count = self.decode(b'3;name="hello;world"\r\nabc\r\n2\r\nde\r\n0\r\nX-Checksum: ignored\r\n\r\n')
        self.assertEqual(count, 5)
        self.assertEqual(self.path.read_bytes(), b"abcde")

    def test_invalid_framing_and_truncated_data(self):
        for raw in (
            b"1\na\r\n0\r\n\r\n", b"zz\r\n", b"-1\r\n", b"3\r\nab",
            b"1\r\naXX0\r\n\r\n", b"0\r\n", b"0\r\nContent-Length: 3\r\n\r\n",
            b"1;bad=\"unterminated\r\na\r\n0\r\n\r\n", b"0\r\nBad Header: x\r\n\r\n",
            b"1" * 8193 + b"\r\n", b"0\r\n" + b"X: a\r\n" * 101 + b"\r\n",
        ):
            with self.subTest(raw=repr(raw[:50])):
                with self.assertRaises(ValueError):
                    self.decode(raw)

    def test_decoded_bytes_are_limited_across_chunks(self):
        with self.assertRaises(OverflowError):
            self.decode(b"3\r\nabc\r\n3\r\ndef\r\n0\r\n\r\n", limit=5)


class DirectoryUploadCleanupTests(unittest.TestCase):
    def test_error_response_is_sent_only_after_all_temporary_files_are_removed(self):
        with tempfile.TemporaryDirectory() as temp:
            config = FluxDropConfig(Path(temp), None, None, 1024 * 1024)
            config.ensure_dirs()
            payload = tar_bytes([("root/a", b"a"), ("other/b", b"b")])
            encoded = f"{len(payload):x}\r\n".encode() + payload + b"\r\n0\r\n\r\n"
            responses = []

            def send_error(status, message):
                self.assertEqual(list(config.storage_dir.glob(".upload-*")), [])
                self.assertEqual(list(config.files_dir.iterdir()), [])
                self.assertEqual(list(config.meta_dir.iterdir()), [])
                responses.append(status)

            handler = SimpleNamespace(
                config=config, rfile=io.BytesIO(encoded), headers={},
                parse_content_length=lambda **kwargs: -1,
                send_error_json=send_error,
                send_upload_response=lambda stored: self.fail("Invalid archive was accepted"),
                log_error=lambda *args: None,
            )
            FluxDropHandler.handle_directory_upload(handler)
            self.assertEqual(responses, [400])


if __name__ == "__main__":
    unittest.main()
