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
import json
import mimetypes
import os
import re
import secrets
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


DEFAULT_MAX_UPLOAD_MB = 8192
DEFAULT_PORT = 8090
DEFAULT_PUBLIC_URL = "http://allenflux.tech:8090"
CHUNK_SIZE = 1024 * 1024
MAX_BULK_DOWNLOAD_FILES = 100
SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")
FILE_ID_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")
STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/app.css": ("app.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/file-actions.mjs": ("file-actions.mjs", "text/javascript; charset=utf-8"),
    "/static/i18n.mjs": ("i18n.mjs", "text/javascript; charset=utf-8"),
    "/static/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
LOG_LINE_RE = re.compile(
    rb"(\b(ERROR|WARN|WARNING|INFO|DEBUG|TRACE|FATAL)\b|\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})"
)


@dataclass
class StoredFile:
    file_id: str
    filename: str
    size: int
    created_at: int


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


def archive_filename(filename: str, used_names: set[str]) -> str:
    name = sanitize_filename(filename)
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
        ):
            return None
        stored.filename.encode("utf-8")
        return stored
    except (OSError, TypeError, UnicodeError, json.JSONDecodeError):
        return None


def store_file(config: FluxDropConfig, filename: str, temp_path: Path, size: int) -> StoredFile:
    file_id = secrets.token_urlsafe(16)
    safe_name = sanitize_filename(filename)
    final_path = config.files_dir / file_id
    shutil.move(str(temp_path), final_path)
    stored = StoredFile(
        file_id=file_id,
        filename=safe_name,
        size=size,
        created_at=int(time.time()),
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
        if parsed.path.startswith("/f/"):
            self.send_download(parsed.path)
            return
        self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in STATIC_ROUTES:
            self.send_static(parsed.path)
            return
        if parsed.path.startswith("/f/"):
            self.send_download(parsed.path, head_only=True)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/upload":
            self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")
            return
        if not self.check_upload_auth():
            return

        if self.headers.get_content_type() == "multipart/form-data":
            self.handle_upload(multipart=True)
        else:
            query = parse_qs(parsed.query)
            filename = query.get("filename", [None])[0] or self.headers.get("X-Filename")
            self.handle_upload(filename)

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/upload":
            filename = self.headers.get("X-Filename")
        elif parsed.path.startswith("/upload/"):
            filename = parsed.path.removeprefix("/upload/")
        else:
            self.send_error_json(HTTPStatus.NOT_FOUND, "Use PUT /upload")
            return
        if not self.check_upload_auth():
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
                        archive_filename(stored.filename, used_names),
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
                    "download_url": f"/f/{file_id}/{quote(stored.filename, safe='')}",
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

    def handle_upload(self, filename: str | None = None, *, multipart: bool = False) -> None:
        length = self.parse_content_length()
        if length is None:
            return

        temp_path = self.config.storage_dir / f".upload-{secrets.token_hex(12)}.tmp"
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
            self.send_error_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, str(exc))
            return
        except ValueError as exc:
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))
            return
        except TimeoutError:
            self.send_error_json(HTTPStatus.REQUEST_TIMEOUT, "Upload timed out")
            return
        except ConnectionError as exc:
            self.log_error("Upload connection closed: %s", exc)
            return
        except OSError as exc:
            self.log_error("Could not store upload: %s", exc)
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "Could not store upload")
            return
        finally:
            temp_path.unlink(missing_ok=True)

        self.send_upload_response(stored)

    def parse_content_length(self) -> int | None:
        if self.headers.get("Transfer-Encoding") is not None:
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
            "size": stored.size,
            "download_url": download_url,
            "curl": f"curl -L -o {quote(stored.filename)} {download_url}",
        }
        self.send_json(HTTPStatus.CREATED, response)

    def build_download_url(self, stored: StoredFile) -> str:
        path = f"/f/{quote(stored.file_id)}/{quote(stored.filename)}"
        if self.config.public_base_url:
            return self.config.public_base_url + path

        scheme = "https" if self.headers.get("X-Forwarded-Proto") == "https" else "http"
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "localhost"
        return f"{scheme}://{host}{path}"

    def send_download(self, request_path: str, head_only: bool = False) -> None:
        parts = request_path.split("/", 3)
        if len(parts) < 3:
            self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")
            return

        file_id = parts[2]
        if not FILE_ID_RE.fullmatch(file_id):
            self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")
            return

        stored = load_metadata(self.config, file_id)
        file_path = self.config.files_dir / file_id
        if stored is None:
            self.send_error_json(HTTPStatus.NOT_FOUND, "File not found")
            return
        try:
            source = file_path.open("rb")
        except FileNotFoundError:
            self.send_error_json(HTTPStatus.NOT_FOUND, "File not found")
            return
        except OSError as exc:
            self.log_error("Could not read file %s: %s", file_id, exc)
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "Could not read file")
            return
        # An open descriptor remains readable if a management request deletes it.
        with source:
            content_type = mimetypes.guess_type(stored.filename)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(os.fstat(source.fileno()).st_size))
            self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%s" % quote(stored.filename))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            if not head_only:
                shutil.copyfileobj(source, self.wfile, length=CHUNK_SIZE)

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
