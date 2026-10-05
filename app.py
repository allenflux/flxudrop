#!/usr/bin/env python3
"""
FluxDrop: tiny curl-friendly file drop service.

Run:
    python3 app.py

Upload:
    curl -T ./backup.tar.gz http://allenflux.tech:8090/upload
    curl -F "file=@./backup.tar.gz" http://allenflux.tech:8090/upload
"""

from __future__ import annotations

import argparse
import codecs
import json
import mimetypes
import os
import re
import secrets
import shlex
import shutil
import sys
import tempfile
import time
import zipfile
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO
from urllib.parse import parse_qs, quote, unquote, urlparse

from fluxdrop_multipart import read_multipart_to_file
from fluxdrop_directories import (
    read_multipart_directory,
    validate_relative_path,
)
from fluxdrop_tar import prepare_directory_archive


DEFAULT_MAX_UPLOAD_MB = 8192
DEFAULT_PORT = 8090
DEFAULT_PUBLIC_URL = "http://allenflux.tech:8090"
CHUNK_SIZE = 1024 * 1024
MAX_CHUNK_HEADER_BYTES = 8192
MAX_CHUNK_TRAILER_BYTES = 16 * 1024
_HTTP_TOKEN = rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+"
_CHUNK_HEADER_RE = re.compile(
    rb"([0-9A-Fa-f]+)(?:[ \t]*;[ \t]*" + _HTTP_TOKEN
    + rb'(?:[ \t]*=[ \t]*(?:' + _HTTP_TOKEN + rb'|"(?:[\t !#-\[\]-~]|\\[\t -~])*"))?)*'
)
PREVIEW_SAMPLE_BYTES = 8192
MAX_TEXT_PREVIEW_BYTES = 256 * 1024
MAX_BULK_DOWNLOAD_FILES = 100
SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")
FILE_ID_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/app.css": ("app.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/file-actions.mjs": ("file-actions.mjs", "text/javascript; charset=utf-8"),
    "/static/browser-upload.mjs": ("browser-upload.mjs", "text/javascript; charset=utf-8"),
    "/static/file-preview.mjs": ("file-preview.mjs", "text/javascript; charset=utf-8"),
    "/static/directory-browser.mjs": ("directory-browser.mjs", "text/javascript; charset=utf-8"),
    "/static/i18n.mjs": ("i18n.mjs", "text/javascript; charset=utf-8"),
    "/static/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
LOG_LINE_RE = re.compile(
    rb"(\b(ERROR|WARN|WARNING|INFO|DEBUG|TRACE|FATAL)\b|\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})"
)
PREVIEW_MEDIA_TYPES = {
    ".png": ("image", "image/png"),
    ".jpg": ("image", "image/jpeg"),
    ".jpeg": ("image", "image/jpeg"),
    ".gif": ("image", "image/gif"),
    ".webp": ("image", "image/webp"),
    ".avif": ("image", "image/avif"),
    ".bmp": ("image", "image/bmp"),
    ".ico": ("image", "image/x-icon"),
    ".pdf": ("pdf", "application/pdf"),
    ".mp3": ("audio", "audio/mpeg"),
    ".wav": ("audio", "audio/wav"),
    ".ogg": ("audio", "audio/ogg"),
    ".oga": ("audio", "audio/ogg"),
    ".opus": ("audio", "audio/ogg"),
    ".m4a": ("audio", "audio/mp4"),
    ".aac": ("audio", "audio/aac"),
    ".flac": ("audio", "audio/flac"),
    ".mp4": ("video", "video/mp4"),
    ".m4v": ("video", "video/mp4"),
    ".webm": ("video", "video/webm"),
    ".ogv": ("video", "video/ogg"),
    ".mov": ("video", "video/quicktime"),
}
PREVIEW_TEXT_EXTENSIONS = {
    ".txt", ".text", ".log", ".md", ".markdown", ".csv", ".tsv",
    ".json", ".jsonl", ".ndjson", ".yaml", ".yml", ".toml", ".ini",
    ".conf", ".cfg", ".env", ".xml", ".svg", ".html", ".htm",
    ".xhtml", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".css",
    ".scss", ".less", ".py", ".sh", ".bash", ".zsh", ".sql", ".go",
    ".rs", ".java", ".c", ".h", ".cpp", ".hpp", ".rb", ".php",
    ".swift", ".kt", ".vue", ".svelte", ".ipynb", ".patch", ".diff",
}


def classify_preview(filename: str, sample: bytes) -> tuple[str, str]:
    suffix = Path(filename).suffix.lower()
    if suffix in PREVIEW_MEDIA_TYPES:
        return PREVIEW_MEDIA_TYPES[suffix]
    if suffix in PREVIEW_TEXT_EXTENSIONS:
        return "text", "text/plain; charset=utf-8"
    try:
        # A fixed-size sample can end in the middle of a UTF-8 character.
        text = codecs.getincrementaldecoder("utf-8")("strict").decode(sample, final=False)
    except UnicodeDecodeError:
        return "unsupported", "application/octet-stream"
    if not sample or (text and all(char in "\n\r\t" or (ord(char) >= 32 and not 127 <= ord(char) < 160) for char in text)):
        return "text", "text/plain; charset=utf-8"
    return "unsupported", "application/octet-stream"


def preview_metadata(config: FluxDropConfig, stored: StoredFile) -> dict[str, str]:
    if stored.kind == "directory":
        return {
            "preview_type": "directory",
            "preview_url": "",
            "browse_url": f"/api/directories/{stored.file_id}",
        }
    try:
        with (config.files_dir / stored.file_id).open("rb") as source:
            preview_type, _ = classify_preview(stored.filename, source.read(PREVIEW_SAMPLE_BYTES))
    except OSError:
        preview_type = "unsupported"
    return {
        "preview_type": preview_type,
        "preview_url": f"/p/{stored.file_id}/{quote(stored.filename, safe='')}",
    }


def parse_byte_range(value: str, size: int) -> tuple[int, int]:
    match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", value.strip())
    if not match or not any(match.groups()) or size == 0:
        raise ValueError("Invalid byte range")
    first, last = match.groups()
    if not first:
        suffix = int(last)
        if suffix == 0:
            raise ValueError("Invalid byte range")
        return max(0, size - suffix), size - 1
    start = int(first)
    end = min(int(last), size - 1) if last else size - 1
    if start >= size or end < start:
        raise ValueError("Unsatisfiable byte range")
    return start, end


@dataclass
class StoredFile:
    file_id: str
    filename: str
    size: int
    created_at: int
    kind: str = "file"
    file_count: int = 0

    @property
    def download_filename(self) -> str:
        return self.filename + ".zip" if self.kind == "directory" else self.filename


class FluxDropConfig:
    def __init__(
        self,
        storage_dir: Path,
        public_base_url: str | None,
        upload_token: str | None,
        max_upload_bytes: int,
    ) -> None:
        self.storage_dir = storage_dir
        self.public_base_url = public_base_url.rstrip("/") if public_base_url else None
        self.upload_token = upload_token
        self.max_upload_bytes = max_upload_bytes
        self.files_dir = storage_dir / "files"
        self.meta_dir = storage_dir / "meta"

    def ensure_dirs(self) -> None:
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.meta_dir.mkdir(parents=True, exist_ok=True)


def sanitize_filename(name: str | None) -> str:
    if not name:
        return "upload.bin"

    name = unquote(name).replace("\\", "/").split("/")[-1].strip()
    name = SAFE_FILENAME_RE.sub("_", name)
    name = name.strip(" .")
    return name[:180] or "upload.bin"


def is_probably_text(sample: bytes) -> bool:
    if not sample:
        return True
    if b"\x00" in sample:
        return False
    printable = sum(1 for byte in sample if byte in b"\n\r\t" or 32 <= byte <= 126)
    return printable / len(sample) > 0.85


def archive_filename(filename: str, used_names: set[str], *, preserve_unicode: bool = False) -> str:
    name = filename if preserve_unicode else sanitize_filename(filename)
    stem, suffix = Path(name).stem, Path(name).suffix
    candidate = name
    number = 2
    while candidate.casefold() in used_names:
        candidate = f"{stem} ({number}){suffix}"
        number += 1
    used_names.add(candidate.casefold())
    return candidate


def infer_extension_from_sample(sample: bytes, content_type: str | None = None) -> str:
    content_type = (content_type or "").split(";", 1)[0].strip().lower()
    if content_type and content_type not in {"application/octet-stream", "binary/octet-stream"}:
        guessed = mimetypes.guess_extension(content_type)
        if guessed:
            return guessed.lstrip(".").replace("jpe", "jpg")

    if sample.startswith(b"\x1f\x8b"):
        return "gz"
    if sample.startswith(b"PK\x03\x04"):
        return "zip"
    if sample.startswith(b"%PDF-"):
        return "pdf"
    if sample.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if sample.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if sample.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if sample.startswith(b"Rar!\x1a\x07"):
        return "rar"
    if sample.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z"

    stripped = sample.lstrip()
    if stripped.startswith((b"{", b"[")):
        return "json"
    if stripped.startswith((b"<?xml", b"<root", b"<xml")):
        return "xml"
    if stripped.lower().startswith((b"<!doctype html", b"<html")):
        return "html"
    if is_probably_text(sample):
        if LOG_LINE_RE.search(sample):
            return "log"
        if b"," in sample and b"\n" in sample:
            return "csv"
        return "txt"
    return "bin"


def filename_or_inferred(filename: str | None, path: Path, content_type: str | None = None) -> str:
    if filename:
        return sanitize_filename(filename)
    with path.open("rb") as source:
        sample = source.read(8192)
    return f"upload.{infer_extension_from_sample(sample, content_type)}"


def get_config() -> FluxDropConfig:
    max_mb = int(os.environ.get("FLUXDROP_MAX_UPLOAD_MB", DEFAULT_MAX_UPLOAD_MB))
    return FluxDropConfig(
        storage_dir=Path(os.environ.get("FLUXDROP_STORAGE_DIR", "data")).resolve(),
        public_base_url=os.environ.get("FLUXDROP_PUBLIC_URL", DEFAULT_PUBLIC_URL),
        upload_token=os.environ.get("FLUXDROP_UPLOAD_TOKEN"),
        max_upload_bytes=max_mb * 1024 * 1024,
    )


def read_exactly_to_file(
    source: BinaryIO,
    target_path: Path,
    content_length: int,
    max_upload_bytes: int,
) -> int:
    if content_length < 0:
        raise ValueError("Content-Length is required")
    if content_length > max_upload_bytes:
        raise OverflowError("File is larger than the configured upload limit")

    remaining = content_length
    written = 0
    with target_path.open("wb") as target:
        while remaining:
            chunk = source.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                raise ValueError("Upload ended before Content-Length bytes were received")
            target.write(chunk)
            written += len(chunk)
            remaining -= len(chunk)
    return written


def read_chunked_to_file(source: BinaryIO, target_path: Path, max_upload_bytes: int) -> int:
    """Decode a bounded HTTP chunked body, validating framing and trailers."""
    written = framing_bytes = chunks = 0

    def line() -> bytes:
        raw = source.readline(MAX_CHUNK_HEADER_BYTES + 1)
        if len(raw) > MAX_CHUNK_HEADER_BYTES or not raw.endswith(b"\r\n"):
            raise ValueError("Invalid or truncated chunked upload framing")
        return raw[:-2]

    with target_path.open("wb") as target:
        while True:
            header = line()
            chunks += 1
            framing_bytes += len(header) + 2
            if chunks > 1_000_000 or framing_bytes > 64 * 1024 * 1024:
                raise ValueError("Chunked upload framing is too large")
            match = _CHUNK_HEADER_RE.fullmatch(header)
            if not match:
                raise ValueError("Invalid chunked upload chunk header")
            size = int(match.group(1), 16)
            if not size:
                trailer_bytes = trailer_count = 0
                while True:
                    trailer = line()
                    trailer_bytes += len(trailer) + 2
                    trailer_count += 1
                    if trailer_bytes > MAX_CHUNK_TRAILER_BYTES or trailer_count > 100:
                        raise ValueError("Chunked upload trailers are too large")
                    if not trailer:
                        return written
                    name, separator, value = trailer.partition(b":")
                    if (
                        not separator or not re.fullmatch(_HTTP_TOKEN, name)
                        or any(byte < 32 and byte != 9 or byte == 127 for byte in value)
                        or name.lower() in {b"content-length", b"transfer-encoding", b"host", b"trailer"}
                    ):
                        raise ValueError("Invalid chunked upload trailer")
            if size > max_upload_bytes - written:
                raise OverflowError("File is larger than the configured upload limit")
            remaining = size
            while remaining:
                chunk = source.read(min(CHUNK_SIZE, remaining))
                if not chunk:
                    raise ValueError("Chunked upload ended before all bytes were received")
                target.write(chunk)
                written += len(chunk)
                remaining -= len(chunk)
            if source.read(2) != b"\r\n":
                raise ValueError("Invalid or truncated chunked upload data terminator")


def save_metadata(config: FluxDropConfig, stored: StoredFile) -> None:
    meta_path = config.meta_dir / f"{stored.file_id}.json"
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=config.meta_dir,
            prefix=f".{stored.file_id}.",
            suffix=".tmp",
            delete=False,
        ) as target:
            temp_path = Path(target.name)
            json.dump(asdict(stored), target, ensure_ascii=False, indent=2)
        os.replace(temp_path, meta_path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def load_metadata(config: FluxDropConfig, file_id: str) -> StoredFile | None:
    meta_path = config.meta_dir / f"{file_id}.json"
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        stored = StoredFile(**data)
        if (
            stored.file_id != file_id
            or not isinstance(stored.filename, str)
            or not stored.filename
            or type(stored.size) is not int
            or stored.size < 0
            or type(stored.created_at) is not int
            or not 0 <= stored.created_at <= 253402300799
            or stored.kind not in {"file", "directory"}
            or type(stored.file_count) is not int
            or stored.file_count < 0
        ):
            return None
        stored.filename.encode("utf-8")
        return stored
    except (OSError, TypeError, UnicodeError, json.JSONDecodeError):
        return None


def store_file(
    config: FluxDropConfig, filename: str, temp_path: Path, size: int,
    *, kind: str = "file", file_count: int = 0,
) -> StoredFile:
    file_id = secrets.token_urlsafe(16)
    safe_name = filename if kind == "directory" else sanitize_filename(filename)
    final_path = config.files_dir / file_id
    shutil.move(str(temp_path), final_path)
    stored = StoredFile(
        file_id=file_id,
        filename=safe_name,
        size=size,
        created_at=int(time.time()),
        kind=kind,
        file_count=file_count,
    )
    try:
        save_metadata(config, stored)
    except Exception:
        final_path.unlink(missing_ok=True)
        raise
    return stored


class FluxDropHandler(BaseHTTPRequestHandler):
    server_version = "FluxDrop/1.0"
    config: FluxDropConfig

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in STATIC_ROUTES:
            self.send_static(parsed.path)
            return
        if parsed.path == "/api/files":
            if self.check_management_auth():
                self.send_file_list()
            return
        if parsed.path == "/api/files/download":
            self.send_bulk_download(parsed.query)
            return
        if parsed.path.startswith("/api/directories/"):
            if self.check_management_auth():
                self.send_directory_list(parsed.path.removeprefix("/api/directories/"), parsed.query)
            return
        if parsed.path.startswith("/f/"):
            self.send_download(parsed.path, query=parsed.query)
            return
        if parsed.path.startswith("/p/"):
            self.send_preview(parsed.path, query=parsed.query)
            return
        self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in STATIC_ROUTES:
            self.send_static(parsed.path)
            return
        if parsed.path.startswith("/f/"):
            self.send_download(parsed.path, head_only=True, query=parsed.query)
            return
        if parsed.path.startswith("/p/"):
            self.send_preview(parsed.path, head_only=True, query=parsed.query)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/upload-directory":
            if self.check_upload_auth():
                if self.headers.get_content_type() != "multipart/form-data":
                    self.send_error_json(HTTPStatus.BAD_REQUEST, "Use multipart POST or upload a ZIP with PUT /upload-directory")
                else:
                    self.handle_directory_upload(multipart=True)
            return
        if parsed.path != "/upload":
            self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")
            return
        if not self.check_upload_auth():
            return

        multipart = self.headers.get_content_type() == "multipart/form-data"
        directory_mode = self.headers.get("X-FluxDrop-Directory", "").strip().lower()
        if directory_mode:
            if directory_mode == "zip":
                self.handle_directory_upload(multipart_zip=multipart)
            elif directory_mode == "files" and multipart:
                self.handle_directory_upload(multipart=True)
            else:
                self.send_error_json(HTTPStatus.BAD_REQUEST, "Directory uploads require 'zip', or multipart 'files', in X-FluxDrop-Directory")
            return

        if multipart:
            self.handle_upload(multipart=True)
        else:
            query = parse_qs(parsed.query)
            filename = query.get("filename", [None])[0] or self.headers.get("X-Filename")
            self.handle_upload(filename)

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/upload-directory":
            if self.check_upload_auth():
                self.handle_directory_upload()
            return
        if parsed.path == "/upload":
            filename = self.headers.get("X-Filename")
        elif parsed.path.startswith("/upload/"):
            filename = parsed.path.removeprefix("/upload/")
        else:
            self.send_error_json(HTTPStatus.NOT_FOUND, "Use PUT /upload")
            return
        if not self.check_upload_auth():
            return
        directory_mode = self.headers.get("X-FluxDrop-Directory", "").strip().lower()
        if directory_mode:
            if directory_mode == "zip":
                self.handle_directory_upload()
            else:
                self.send_error_json(HTTPStatus.BAD_REQUEST, "PUT directory uploads require X-FluxDrop-Directory: zip")
            return
        self.handle_upload(filename)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/files/"):
            self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")
            return
        if not self.check_management_auth():
            return
        file_id = parsed.path.removeprefix("/api/files/")
        if not FILE_ID_RE.fullmatch(file_id):
            self.send_error_json(HTTPStatus.NOT_FOUND, "File not found")
            return
        if load_metadata(self.config, file_id) is None:
            self.send_error_json(HTTPStatus.NOT_FOUND, "File not found")
            return
        try:
            # Remove data first so a failed unlink leaves a usable metadata record.
            # A retry can also clean up metadata left by a partial deletion.
            (self.config.files_dir / file_id).unlink(missing_ok=True)
            (self.config.meta_dir / f"{file_id}.json").unlink(missing_ok=True)
        except OSError as exc:
            self.log_error("Could not delete file %s: %s", file_id, exc)
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "Could not complete file deletion")
            return
        self.send_json(HTTPStatus.OK, {"ok": True, "file_id": file_id})

    def check_management_auth(self) -> bool:
        return self.check_upload_auth()

    def send_bulk_download(self, query: str) -> None:
        try:
            params = parse_qs(query, keep_blank_values=True, max_num_fields=MAX_BULK_DOWNLOAD_FILES)
            file_ids = list(dict.fromkeys(params.get("file_id", [])))
            if set(params) != {"file_id"} or not file_ids or any(not FILE_ID_RE.fullmatch(file_id) for file_id in file_ids):
                raise ValueError("Invalid file selection")
        except ValueError:
            self.send_error_json(HTTPStatus.BAD_REQUEST, "Select between 1 and 100 valid file IDs")
            return

        # Like individual download links, this endpoint only needs known file IDs.
        # Open all files before sending headers so deletion cannot truncate a ZIP.
        with ExitStack() as stack:
            entries = []
            used_names: set[str] = set()
            try:
                for file_id in file_ids:
                    stored = load_metadata(self.config, file_id)
                    if stored is None:
                        raise FileNotFoundError(file_id)
                    source = stack.enter_context((self.config.files_dir / file_id).open("rb"))
                    info = zipfile.ZipInfo(
                        archive_filename(stored.download_filename, used_names, preserve_unicode=stored.kind == "directory"),
                        time.gmtime(max(315532800, min(stored.created_at, 4354819198)))[:6],
                    )
                    info.file_size = os.fstat(source.fileno()).st_size
                    info.external_attr = 0o100600 << 16
                    entries.append((info, source))
            except FileNotFoundError:
                self.send_error_json(HTTPStatus.NOT_FOUND, "One or more selected files no longer exist; refresh the file list")
                return
            except OSError as exc:
                self.log_error("Could not open selected files: %s", exc)
                self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "Could not read selected files")
                return

            self.close_connection = True
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="fluxdrop-files.zip"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                # ZIP_STORED avoids compression overhead and buffering large files.
                with zipfile.ZipFile(self.wfile, "w", compression=zipfile.ZIP_STORED) as archive:
                    for info, source in entries:
                        with archive.open(info, "w", force_zip64=True) as target:
                            shutil.copyfileobj(source, target, length=CHUNK_SIZE)
            except OSError as exc:
                self.log_error("Bulk download interrupted: %s", exc)

    def send_file_list(self) -> None:
        files = []
        try:
            for meta_path in self.config.meta_dir.glob("*.json"):
                file_id = meta_path.stem
                if not FILE_ID_RE.fullmatch(file_id):
                    continue
                stored = load_metadata(self.config, file_id)
                if stored is None or not (self.config.files_dir / file_id).is_file():
                    continue
                files.append({
                    **asdict(stored),
                    "download_filename": stored.download_filename,
                    "download_url": f"/f/{file_id}/{quote(stored.download_filename, safe='')}",
                    **preview_metadata(self.config, stored),
                })
        except OSError as exc:
            self.log_error("Could not list files: %s", exc)
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "Could not list files")
            return
        files.sort(key=lambda item: (item["created_at"], item["file_id"]), reverse=True)
        self.send_json(HTTPStatus.OK, {"ok": True, "files": files})

    def check_upload_auth(self) -> bool:
        token = self.config.upload_token
        if not token:
            return True

        auth = self.headers.get("Authorization", "")
        header_token = self.headers.get("X-Upload-Token", "")
        if (
            secrets.compare_digest(auth.encode("utf-8"), f"Bearer {token}".encode("utf-8"))
            or secrets.compare_digest(header_token.encode("utf-8"), token.encode("utf-8"))
        ):
            return True
        self.send_error_json(HTTPStatus.UNAUTHORIZED, "Missing or invalid upload token")
        return False

    def handle_directory_upload(self, *, multipart: bool = False, multipart_zip: bool = False) -> None:
        length = self.parse_content_length(allow_chunked=True)
        if length is None:
            return
        temp_path = self.config.storage_dir / f".upload-{secrets.token_hex(12)}.tmp"
        body_path = temp_path.with_suffix(".body.tmp")
        converted_path = temp_path.with_suffix(".zip.tmp")
        error: tuple[HTTPStatus, str] | None = None
        try:
            with ExitStack() as stack:
                source = self.rfile
                if length == -1 and (multipart or multipart_zip):
                    length = read_chunked_to_file(source, body_path, self.config.max_upload_bytes)
                    source = stack.enter_context(body_path.open("rb"))
                if multipart_zip:
                    read_multipart_to_file(source, temp_path, length, self.headers.get("Content-Type", ""))
                elif multipart:
                    summary = read_multipart_directory(
                        source, temp_path, length,
                        self.headers.get("Content-Type", ""), self.config.max_upload_bytes,
                    )
                elif length == -1:
                    read_chunked_to_file(source, temp_path, self.config.max_upload_bytes)
                else:
                    read_exactly_to_file(source, temp_path, length, self.config.max_upload_bytes)
                if not multipart:
                    summary = prepare_directory_archive(temp_path, converted_path, self.config.max_upload_bytes)
            stored = store_file(
                self.config, summary.filename, temp_path, temp_path.stat().st_size,
                kind="directory", file_count=summary.file_count,
            )
        except OverflowError as exc:
            error = (HTTPStatus.REQUEST_ENTITY_TOO_LARGE, str(exc))
        except ValueError as exc:
            error = (HTTPStatus.BAD_REQUEST, str(exc))
        except TimeoutError:
            error = (HTTPStatus.REQUEST_TIMEOUT, "Upload timed out")
        except ConnectionError as exc:
            self.log_error("Directory upload connection closed: %s", exc)
            return
        except OSError as exc:
            self.log_error("Could not store directory upload: %s", exc)
            error = (HTTPStatus.INTERNAL_SERVER_ERROR, "Could not store directory upload")
        finally:
            temp_path.unlink(missing_ok=True)
            body_path.unlink(missing_ok=True)
            converted_path.unlink(missing_ok=True)
        if error is not None:
            self.send_error_json(*error)
            return
        self.send_upload_response(stored)

    def handle_upload(self, filename: str | None = None, *, multipart: bool = False) -> None:
        length = self.parse_content_length()
        if length is None:
            return

        temp_path = self.config.storage_dir / f".upload-{secrets.token_hex(12)}.tmp"
        error: tuple[HTTPStatus, str] | None = None
        try:
            content_type = self.headers.get("Content-Type")
            if multipart:
                filename, content_type, size = read_multipart_to_file(
                    self.rfile, temp_path, length, content_type or ""
                )
            else:
                size = read_exactly_to_file(
                    self.rfile, temp_path, length, self.config.max_upload_bytes
                )
            guessed_filename = filename_or_inferred(filename, temp_path, content_type)
            stored = store_file(self.config, guessed_filename, temp_path, size)
        except OverflowError as exc:
            error = (HTTPStatus.REQUEST_ENTITY_TOO_LARGE, str(exc))
        except ValueError as exc:
            error = (HTTPStatus.BAD_REQUEST, str(exc))
        except TimeoutError:
            error = (HTTPStatus.REQUEST_TIMEOUT, "Upload timed out")
        except ConnectionError as exc:
            self.log_error("Upload connection closed: %s", exc)
            return
        except OSError as exc:
            self.log_error("Could not store upload: %s", exc)
            error = (HTTPStatus.INTERNAL_SERVER_ERROR, "Could not store upload")
        finally:
            temp_path.unlink(missing_ok=True)

        if error is not None:
            self.send_error_json(*error)
            return
        self.send_upload_response(stored)

    def parse_content_length(self, *, allow_chunked: bool = False) -> int | None:
        encodings = self.headers.get_all("Transfer-Encoding", [])
        if encodings:
            if allow_chunked:
                if self.headers.get_all("Content-Length", []):
                    self.send_error_json(HTTPStatus.BAD_REQUEST, "Transfer-Encoding and Content-Length cannot be combined")
                    return None
                if len(encodings) == 1 and encodings[0].strip().lower() == "chunked":
                    return -1
            self.send_error_json(HTTPStatus.NOT_IMPLEMENTED, "Transfer-Encoding is not supported; use Content-Length")
            return None
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths:
            self.send_error_json(HTTPStatus.LENGTH_REQUIRED, "Content-Length is required")
            return None
        raw_length = lengths[0].strip(" \t")
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]+", raw_length):
            self.send_error_json(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
            return None
        try:
            length = int(raw_length)
        except ValueError:
            self.send_error_json(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
            return None
        if length > self.config.max_upload_bytes:
            self.send_error_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "File is larger than the configured upload limit")
            return None
        return length

    def send_upload_response(self, stored: StoredFile) -> None:
        download_url = self.build_download_url(stored)
        response = {
            "ok": True,
            "file_id": stored.file_id,
            "filename": stored.filename,
            "kind": stored.kind,
            "file_count": stored.file_count,
            "download_filename": stored.download_filename,
            "size": stored.size,
            "download_url": download_url,
            "curl": f"curl -L -o {shlex.quote(stored.download_filename)} {shlex.quote(download_url)}",
            **preview_metadata(self.config, stored),
        }
        self.send_json(HTTPStatus.CREATED, response)

    def build_download_url(self, stored: StoredFile) -> str:
        path = f"/f/{quote(stored.file_id)}/{quote(stored.download_filename, safe='')}"
        if self.config.public_base_url:
            return self.config.public_base_url + path

        scheme = "https" if self.headers.get("X-Forwarded-Proto") == "https" else "http"
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "localhost"
        return f"{scheme}://{host}{path}"

    def directory_query_path(self, query: str) -> str:
        params = parse_qs(query, keep_blank_values=True, max_num_fields=1, errors="strict")
        if set(params) - {"path"}:
            raise ValueError("Invalid directory path query")
        return validate_relative_path(params.get("path", [""])[0], allow_empty=True, directory=True)

    def open_stored_content(
        self, stored: StoredFile, query: str, stack: ExitStack,
    ) -> tuple[BinaryIO, str, int]:
        """Open a stable file descriptor, or a member stream backed by one."""
        source = stack.enter_context((self.config.files_dir / stored.file_id).open("rb"))
        if stored.kind == "directory":
            relative_path = self.directory_query_path(query)
            if relative_path:
                archive = stack.enter_context(zipfile.ZipFile(source))
                try:
                    info = archive.getinfo(f"{stored.filename}/{relative_path}")
                except KeyError as exc:
                    raise FileNotFoundError(relative_path) from exc
                if info.is_dir():
                    raise FileNotFoundError(relative_path)
                entry = stack.enter_context(archive.open(info))
                return entry, relative_path.rsplit("/", 1)[-1], info.file_size
        return source, stored.download_filename, os.fstat(source.fileno()).st_size

    def send_directory_list(self, file_id: str, query: str) -> None:
        stored = load_metadata(self.config, file_id) if FILE_ID_RE.fullmatch(file_id) else None
        if stored is None or stored.kind != "directory":
            self.send_error_json(HTTPStatus.NOT_FOUND, "Directory not found")
            return
        try:
            relative_path = self.directory_query_path(query)
            prefix = stored.filename + "/" + (relative_path + "/" if relative_path else "")
            with zipfile.ZipFile(self.config.files_dir / file_id) as archive:
                entries: dict[str, dict] = {}
                exists = not relative_path
                for info in archive.infolist():
                    if not info.filename.startswith(prefix):
                        continue
                    exists = True
                    remainder = info.filename[len(prefix):]
                    if not remainder:
                        continue
                    filename, separator, _ = remainder.partition("/")
                    kind = "directory" if separator else "file"
                    entry_path = f"{relative_path}/{filename}" if relative_path else filename
                    if filename in entries:
                        entries[filename]["size"] += info.file_size
                        continue
                    entry = {
                        "filename": filename, "path": entry_path, "kind": kind,
                        "size": info.file_size,
                        "preview_type": "directory", "preview_url": "", "download_url": "",
                    }
                    encoded_path = quote(entry_path, safe="")
                    if kind == "directory":
                        entry["browse_url"] = f"/api/directories/{file_id}?path={encoded_path}"
                    else:
                        with archive.open(info) as source:
                            preview_type, _ = classify_preview(filename, source.read(PREVIEW_SAMPLE_BYTES))
                        entry.update({
                            "preview_type": preview_type,
                            "preview_url": f"/p/{file_id}/{quote(filename, safe='')}?path={encoded_path}",
                            "download_url": f"/f/{file_id}/{quote(filename, safe='')}?path={encoded_path}",
                            "download_filename": filename,
                        })
                    entries[filename] = entry
                if not exists:
                    self.send_error_json(HTTPStatus.NOT_FOUND, "Directory not found")
                    return
        except (ValueError, UnicodeError) as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return
        except FileNotFoundError:
            self.send_error_json(HTTPStatus.NOT_FOUND, "Directory not found")
            return
        except (OSError, zipfile.BadZipFile) as exc:
            self.log_error("Could not read directory %s: %s", file_id, exc)
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "Could not read directory")
            return
        self.send_json(HTTPStatus.OK, {
            "ok": True, "file_id": file_id, "filename": stored.filename,
            "path": relative_path,
            "entries": sorted(entries.values(), key=lambda entry: (entry["kind"] != "directory", entry["filename"].casefold())),
        })

    def send_download(self, request_path: str, head_only: bool = False, *, query: str = "") -> None:
        parts = request_path.split("/", 3)
        if len(parts) < 3:
            self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")
            return

        file_id = parts[2]
        if not FILE_ID_RE.fullmatch(file_id):
            self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")
            return

        stored = load_metadata(self.config, file_id)
        if stored is None:
            self.send_error_json(HTTPStatus.NOT_FOUND, "File not found")
            return
        with ExitStack() as stack:
            try:
                source, filename, size = self.open_stored_content(stored, query, stack)
            except (ValueError, UnicodeError) as exc:
                self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
                return
            except FileNotFoundError:
                self.send_error_json(HTTPStatus.NOT_FOUND, "File not found")
                return
            except (OSError, zipfile.BadZipFile) as exc:
                self.log_error("Could not read file %s: %s", file_id, exc)
                self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "Could not read file")
                return
            content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%s" % quote(filename, safe=""))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if not head_only:
                shutil.copyfileobj(source, self.wfile, length=CHUNK_SIZE)

    def send_preview_security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "sandbox; default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'")

    def send_preview_error(self, status: HTTPStatus, message: str, size: int | None = None) -> None:
        raw = json.dumps({"ok": False, "error": message}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        if size is not None:
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Accept-Ranges", "bytes")
        self.send_preview_security_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def send_preview(self, request_path: str, head_only: bool = False, *, query: str = "") -> None:
        parts = request_path.split("/", 3)
        file_id = parts[2] if len(parts) >= 3 else ""
        if not FILE_ID_RE.fullmatch(file_id):
            self.send_preview_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        stored = load_metadata(self.config, file_id)
        if stored is None:
            self.send_preview_error(HTTPStatus.NOT_FOUND, "File not found")
            return
        with ExitStack() as stack:
            try:
                source, filename, size = self.open_stored_content(stored, query, stack)
                preview_type, content_type = classify_preview(filename, source.read(PREVIEW_SAMPLE_BYTES))
                source.seek(0)
                text_payload = None
                truncated = False
                if preview_type == "text":
                    sample = source.read(MAX_TEXT_PREVIEW_BYTES + 1)
                    truncated = len(sample) > MAX_TEXT_PREVIEW_BYTES
                    text = codecs.getincrementaldecoder("utf-8")("replace").decode(
                        sample[:MAX_TEXT_PREVIEW_BYTES], final=not truncated
                    )
                    text_payload = text.encode("utf-8")
                    if len(text_payload) > MAX_TEXT_PREVIEW_BYTES:
                        truncated = True
                        text_payload = text_payload[:MAX_TEXT_PREVIEW_BYTES].decode("utf-8", errors="ignore").encode("utf-8")
            except (ValueError, UnicodeError) as exc:
                self.send_preview_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            except FileNotFoundError:
                self.send_preview_error(HTTPStatus.NOT_FOUND, "File not found")
                return
            except (OSError, zipfile.BadZipFile) as exc:
                self.log_error("Could not read preview %s: %s", file_id, exc)
                self.send_preview_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Could not read file")
                return

            if preview_type == "unsupported":
                self.send_preview_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "This file type cannot be previewed; download the file instead")
                return

            start, end = 0, size - 1
            status = HTTPStatus.OK
            range_headers = self.headers.get_all("Range", [])
            if text_payload is None and range_headers:
                try:
                    if len(range_headers) != 1:
                        raise ValueError("Multiple ranges are not supported")
                    start, end = parse_byte_range(range_headers[0], size)
                except ValueError:
                    self.send_preview_error(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, "Invalid or unsatisfiable byte range", size)
                    return
                status = HTTPStatus.PARTIAL_CONTENT

            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(text_payload) if text_payload is not None else end - start + 1))
            self.send_header("Content-Disposition", "inline; filename*=UTF-8''%s" % quote(filename, safe=""))
            self.send_preview_security_headers()
            if text_payload is not None:
                self.send_header("X-Preview-Truncated", "true" if truncated else "false")
            else:
                self.send_header("Accept-Ranges", "bytes")
                if status == HTTPStatus.PARTIAL_CONTENT:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if head_only:
                return
            try:
                if text_payload is not None:
                    self.wfile.write(text_payload)
                else:
                    source.seek(start)
                    remaining = end - start + 1
                    while remaining:
                        chunk = source.read(min(CHUNK_SIZE, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except OSError as exc:
                self.log_error("Preview interrupted: %s", exc)

    def send_static(self, request_path: str) -> None:
        filename, content_type = STATIC_ROUTES[request_path]
        try:
            payload = (STATIC_DIR / filename).read_bytes()
        except OSError:
            self.send_error_json(HTTPStatus.NOT_FOUND, "Page asset not found")
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    def send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def send_error_json(self, status: HTTPStatus, message: str) -> None:
        self.send_json(status, {"ok": False, "error": message})


def make_handler(config: FluxDropConfig) -> type[FluxDropHandler]:
    class ConfiguredFluxDropHandler(FluxDropHandler):
        pass

    ConfiguredFluxDropHandler.config = config
    return ConfiguredFluxDropHandler


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Curl-friendly temporary file drop service")
    parser.add_argument("--host", default=os.environ.get("FLUXDROP_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("FLUXDROP_PORT", DEFAULT_PORT)))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = get_config()
    config.ensure_dirs()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(config))

    print(f"FluxDrop listening on http://{args.host}:{args.port}")
    print(f"Public URL: {config.public_base_url}")
    print(f"Storage: {config.storage_dir}")
    if config.upload_token:
        print("Upload protection: enabled")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
