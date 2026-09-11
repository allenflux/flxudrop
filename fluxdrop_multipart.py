"""Bounded-memory multipart/form-data uploads using only the standard library."""

from __future__ import annotations

import email.parser
import email.policy
import re
from pathlib import Path
from typing import BinaryIO


CHUNK_SIZE = 64 * 1024
MAX_HEADER_BYTES = 16 * 1024
_BOUNDARY_RE = re.compile(rb"[0-9A-Za-z'()+_,./:=? -]{1,70}")


class _MultipartReader:
    def __init__(self, source: BinaryIO, content_length: int, boundary: bytes) -> None:
        self.source = source
        self.remaining = content_length
        self.marker = b"\r\n--" + boundary
        # A virtual CRLF lets the initial delimiter use the same scanner as parts.
        self.buffer = bytearray(b"\r\n")

    def _fill(self) -> None:
        chunk = self.source.read(min(CHUNK_SIZE, self.remaining))
        if not chunk:
            raise ValueError("Upload ended before Content-Length bytes were received")
        self.remaining -= len(chunk)
        self.buffer.extend(chunk)

    def _ensure(self, count: int) -> bool:
        while len(self.buffer) < count and self.remaining:
            self._fill()
        return len(self.buffer) >= count

    def _emit(self, count: int, target: BinaryIO | None) -> int:
        if target is not None:
            target.write(self.buffer[:count])
        del self.buffer[:count]
        return count

    def read_to_boundary(self, target: BinaryIO | None = None) -> tuple[bool, int]:
        """Copy or discard a part, returning (closing delimiter, payload size)."""
        written = 0
        while True:
            offset = self.buffer.find(self.marker)
            if offset < 0:
                # Keep only bytes that might start a delimiter in the next chunk.
                count = max(0, len(self.buffer) - len(self.marker) + 1)
                written += self._emit(count, target)
                if not self.remaining:
                    raise ValueError("Multipart body is missing its closing boundary")
                self._fill()
                continue

            written += self._emit(offset, target)
            candidate_size = self._emit(len(self.marker), target)
            self._ensure(2)
            closing = self.buffer.startswith(b"--")
            if closing:
                candidate_size += self._emit(2, target)

            # Boundary lines allow transport padding. Tentatively write it so
            # even an arbitrarily long false delimiter uses bounded memory.
            while True:
                padding = 0
                while padding < len(self.buffer) and self.buffer[padding] in b" \t":
                    padding += 1
                candidate_size += self._emit(padding, target)
                if self.buffer or not self.remaining:
                    break
                self._fill()

            self._ensure(2)
            if self.buffer.startswith(b"\r\n"):
                del self.buffer[:2]
                valid = True
            else:
                valid = closing and not self.buffer and not self.remaining

            if valid:
                if target is not None:
                    # Remove the tentative delimiter, leaving the exact payload.
                    target.seek(-candidate_size, 1)
                    target.truncate()
                return closing, written

            # The boundary prefix occurred inside binary data; preserve it and
            # leave the non-matching suffix available for the next scan.
            written += candidate_size

    def read_headers(self) -> bytes:
        while True:
            if self.buffer.startswith(b"\r\n"):
                del self.buffer[:2]
                return b""
            end = self.buffer.find(b"\r\n\r\n")
            if end >= 0:
                if end > MAX_HEADER_BYTES:
                    raise ValueError("Multipart part headers are too large")
                headers = bytes(self.buffer[:end])
                del self.buffer[:end + 4]
                return headers + b"\r\n\r\n"
            if len(self.buffer) > MAX_HEADER_BYTES + 3:
                raise ValueError("Multipart part headers are too large")
            if not self.remaining:
                raise ValueError("Multipart part headers are incomplete")
            self._fill()

    def finish(self) -> None:
        """Discard any MIME epilogue while checking the entire declared length."""
        self.buffer.clear()
        while self.remaining:
            self._fill()
            self.buffer.clear()


def read_multipart_to_file(
    source: BinaryIO,
    target_path: Path,
    content_length: int,
    content_type: str,
) -> tuple[str | None, str | None, int]:
    """Save the first file part and return its filename, content type and size.

    A part qualifies when it has a nonempty filename or its field name is
    ``file``. Other parts, the preamble and the epilogue are streamed away.
    Malformed/truncated bodies raise ValueError; callers must remove the target
    on failure and enforce their request-size limit before calling this helper.
    Only unencoded form-data payloads (binary, 8bit or 7bit) are accepted.
    """
    if content_length < 0:
        raise ValueError("Invalid Content-Length")
    if "\r" in content_type or "\n" in content_type:
        raise ValueError("Invalid multipart Content-Type")
    parser = email.parser.BytesHeaderParser(policy=email.policy.default)
    envelope = parser.parsebytes(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
    boundary_text = envelope.get_boundary()
    if envelope.get_content_type() != "multipart/form-data" or not boundary_text:
        raise ValueError("Multipart boundary is missing or invalid")
    try:
        boundary = boundary_text.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("Multipart boundary is invalid") from exc
    if not _BOUNDARY_RE.fullmatch(boundary) or boundary.endswith(b" "):
        raise ValueError("Multipart boundary is invalid")

    reader = _MultipartReader(source, content_length, boundary)
    closing, _ = reader.read_to_boundary()
    selected: tuple[str | None, str | None, int] | None = None
    with target_path.open("wb") as target:
        while not closing:
            headers = parser.parsebytes(reader.read_headers())
            if headers.defects:
                raise ValueError("Multipart part headers are malformed")
            filename = headers.get_filename()
            is_file = filename or headers.get_param("name", header="content-disposition") == "file"
            if selected is None and is_file:
                encoding = str(headers.get("Content-Transfer-Encoding", "binary")).strip().lower()
                if encoding not in {"binary", "8bit", "7bit"}:
                    raise ValueError("Encoded multipart file parts are not supported")
                closing, size = reader.read_to_boundary(target)
                selected = (filename, headers.get_content_type(), size)
            else:
                closing, _ = reader.read_to_boundary()
        reader.finish()

    if selected is None:
        raise ValueError("Multipart field 'file' was not found")
    return selected
