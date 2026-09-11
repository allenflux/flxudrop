from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from fluxdrop_multipart import CHUNK_SIZE, MAX_HEADER_BYTES, read_multipart_to_file


class ShortReader(io.BytesIO):
    def __init__(self, value: bytes, chunk_size: int) -> None:
        super().__init__(value)
        self.chunk_size = chunk_size
        self.largest_request = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            raise AssertionError("The parser must not read the whole request")
        self.largest_request = max(self.largest_request, size)
        return super().read(min(size, self.chunk_size))


class MultipartTests(unittest.TestCase):
    boundary = b"fluxdrop-boundary"
    content_type = 'multipart/form-data; boundary="fluxdrop-boundary"'

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.target = Path(self.tempdir.name) / "upload.tmp"

    def body(self, parts: list[tuple[bytes, bytes]]) -> bytes:
        return b"".join(
            b"--" + self.boundary + b"\r\n" + headers + b"\r\n\r\n" + payload + b"\r\n"
            for headers, payload in parts
        ) + b"--" + self.boundary + b"--\r\n"

    def parse(self, body: bytes, chunk_size: int = CHUNK_SIZE):
        source = ShortReader(body, chunk_size)
        result = read_multipart_to_file(source, self.target, len(body), self.content_type)
        self.assertEqual(source.tell(), len(body))
        self.assertLessEqual(source.largest_request, CHUNK_SIZE)
        return result

    def test_streams_binary_first_file_and_ignores_other_parts(self) -> None:
        payload = bytes(range(256)) * 1000
        body = self.body([
            (b'Content-Disposition: form-data; name="description"', b"ignored" * 20000),
            (b'Content-Disposition: form-data; name="attachment"; filename="sample.bin"\r\n'
             b'Content-Type: application/octet-stream', payload),
            (b'Content-Disposition: form-data; name="file"; filename="later.bin"', b"later" * 20000),
        ])
        self.assertEqual(self.parse(body), ("sample.bin", "application/octet-stream", len(payload)))
        self.assertEqual(self.target.read_bytes(), payload)

    def test_all_short_read_sizes_preserve_binary_boundary_prefixes(self) -> None:
        marker = b"\r\n--" + self.boundary
        payload = (b"\x00" + marker + b"X" + marker + b"--X" + marker + b" \tX"
                   + marker + b"-\r" + marker + b"\rX" + b"\xff\r\n")
        body = self.body([(b'Content-Disposition: form-data; name="file"', payload)])
        for chunk_size in range(1, len(marker) + 8):
            with self.subTest(chunk_size=chunk_size):
                self.assertEqual(self.parse(body, chunk_size), (None, "text/plain", len(payload)))
                self.assertEqual(self.target.read_bytes(), payload)

    def test_empty_file_and_empty_filename(self) -> None:
        body = self.body([(b'Content-Disposition: form-data; name="file"; filename=""', b"")])
        self.assertEqual(self.parse(body, 1), ("", "text/plain", 0))
        self.assertEqual(self.target.read_bytes(), b"")

    def test_preamble_epilogue_and_final_boundary_without_crlf(self) -> None:
        body = self.body([(b'Content-Disposition: form-data; name="file"', b"hello")])
        self.assertEqual(self.parse(b"preamble\r\n" + body + b"epilogue"), (None, "text/plain", 5))
        self.assertEqual(self.parse(body[:-2], 1), (None, "text/plain", 5))

    def test_boundary_padding_is_streamed_and_false_padding_preserved(self) -> None:
        padding = b" \t" * CHUNK_SIZE
        payload = b"data\r\n--" + self.boundary + padding + b"Xmore data"
        body = self.body([(b'Content-Disposition: form-data; name="file"', payload)])
        body = body.replace(b"--" + self.boundary + b"--\r\n", b"--" + self.boundary + b"--" + padding + b"\r\n")
        self.assertEqual(self.parse(body), (None, "text/plain", len(payload)))
        self.assertEqual(self.target.read_bytes(), payload)

    def test_stops_exactly_at_content_length(self) -> None:
        body = self.body([(b'Content-Disposition: form-data; name="file"', b"hello")])
        source = io.BytesIO(body + b"next request")
        read_multipart_to_file(source, self.target, len(body), self.content_type)
        self.assertEqual(source.read(), b"next request")

    def test_truncated_content_length_even_after_valid_final_boundary(self) -> None:
        body = self.body([(b'Content-Disposition: form-data; name="file"', b"hello")])
        with self.assertRaisesRegex(ValueError, "Content-Length"):
            read_multipart_to_file(io.BytesIO(body), self.target, len(body) + 1, self.content_type)

    def test_missing_final_boundary_or_truncated_headers(self) -> None:
        body = self.body([(b'Content-Disposition: form-data; name="file"', b"hello")])
        for truncated in (body[:-5], body[:40], body[:20], b""):
            with self.subTest(body=truncated), self.assertRaises(ValueError):
                self.parse(truncated, 3)

    def test_rejects_missing_file_and_invalid_following_part(self) -> None:
        for body in (
            self.body([(b'Content-Disposition: form-data; name="other"', b"value")]),
            self.body([(b'Content-Disposition: form-data; name="file"', b"hello"),
                       (b"malformed header", b"ignored")]),
        ):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.parse(body)

    def test_caps_headers_in_selected_and_ignored_parts(self) -> None:
        for name in (b"file", b"other"):
            headers = b'Content-Disposition: form-data; name="' + name + b'"\r\nX-Large: ' + b"x" * MAX_HEADER_BYTES
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "headers are too large"):
                self.parse(self.body([(headers, b"data")]))

    def test_rejects_invalid_boundary_and_encoded_payload(self) -> None:
        body = self.body([(b'Content-Disposition: form-data; name="file"', b"hello")])
        for content_type in ("multipart/form-data", "text/plain; boundary=abc",
                             "multipart/form-data; boundary=" + "a" * 71,
                             'multipart/form-data; boundary="bad@boundary"'):
            with self.subTest(content_type=content_type), self.assertRaises(ValueError):
                read_multipart_to_file(io.BytesIO(body), self.target, len(body), content_type)
        encoded = self.body([(b'Content-Disposition: form-data; name="file"\r\n'
                              b'Content-Transfer-Encoding: base64', b"aGVsbG8=")])
        with self.assertRaisesRegex(ValueError, "Encoded multipart"):
            self.parse(encoded)


if __name__ == "__main__":
    unittest.main()
