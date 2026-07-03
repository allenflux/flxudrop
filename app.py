#!/usr/bin/env python3
"""
FluxDrop: tiny curl-friendly file drop service.

Run:
    python3 app.py

Upload:
    curl -T ./backup.tar.gz http://allenflux.tech:8090/upload/backup.tar.gz
    curl -F "file=@./backup.tar.gz" http://allenflux.tech:8090/upload
"""

from __future__ import annotations

import argparse
import email.parser
import email.policy
import html
import json
import mimetypes
import os
import re
import secrets
import shutil
import sys
import time
from dataclasses import asdict, dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO
from urllib.parse import parse_qs, quote, unquote, urlparse


DEFAULT_MAX_UPLOAD_MB = 1024
DEFAULT_PORT = 8090
DEFAULT_PUBLIC_URL = "http://allenflux.tech:8090"
CHUNK_SIZE = 1024 * 1024
SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._ -]+")


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
    meta_path.write_text(json.dumps(asdict(stored), ensure_ascii=False, indent=2), encoding="utf-8")


def load_metadata(config: FluxDropConfig, file_id: str) -> StoredFile | None:
    meta_path = config.meta_dir / f"{file_id}.json"
    if not meta_path.exists():
        return None
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        return StoredFile(**data)
    except (OSError, TypeError, json.JSONDecodeError):
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
    save_metadata(config, stored)
    return stored


class FluxDropHandler(BaseHTTPRequestHandler):
    server_version = "FluxDrop/1.0"
    config: FluxDropConfig

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_home()
            return
        if parsed.path.startswith("/f/"):
            self.send_download(parsed.path)
            return
        self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")

    def do_HEAD(self) -> None:
        parsed = urlparse(self.path)
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

        content_type = self.headers.get("Content-Type", "")
        if content_type.startswith("multipart/form-data"):
            self.handle_multipart_upload()
        else:
            query = parse_qs(parsed.query)
            filename = query.get("filename", [None])[0] or self.headers.get("X-Filename")
            self.handle_stream_upload(filename)

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/upload/"):
            self.send_error_json(HTTPStatus.NOT_FOUND, "Use PUT /upload/<filename>")
            return
        if not self.check_upload_auth():
            return
        filename = parsed.path.removeprefix("/upload/")
        self.handle_stream_upload(filename)

    def check_upload_auth(self) -> bool:
        token = self.config.upload_token
        if not token:
            return True

        auth = self.headers.get("Authorization", "")
        header_token = self.headers.get("X-Upload-Token", "")
        if auth == f"Bearer {token}" or header_token == token:
            return True
        self.send_error_json(HTTPStatus.UNAUTHORIZED, "Missing or invalid upload token")
        return False

    def handle_stream_upload(self, filename: str | None) -> None:
        length = self.parse_content_length()
        if length is None:
            return

        temp_path = self.config.storage_dir / f".upload-{secrets.token_hex(12)}.tmp"
        try:
            size = read_exactly_to_file(
                self.rfile,
                temp_path,
                length,
                self.config.max_upload_bytes,
            )
            stored = store_file(self.config, sanitize_filename(filename), temp_path, size)
            self.send_upload_response(stored)
        except OverflowError as exc:
            temp_path.unlink(missing_ok=True)
            self.send_error_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, str(exc))
        except (OSError, ValueError) as exc:
            temp_path.unlink(missing_ok=True)
            self.send_error_json(HTTPStatus.BAD_REQUEST, str(exc))

    def handle_multipart_upload(self) -> None:
        length = self.parse_content_length()
        if length is None:
            return
        if length > self.config.max_upload_bytes:
            self.send_error_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "File is larger than the configured upload limit",
            )
            return

        # Multipart uploads are parsed in memory for compatibility with curl -F.
        # Use PUT /upload/<filename> for large files; that path streams to disk.
        body = self.rfile.read(length)
        headers = f"Content-Type: {self.headers.get('Content-Type')}\r\nMIME-Version: 1.0\r\n\r\n"
        message = email.parser.BytesParser(policy=email.policy.default).parsebytes(
            headers.encode("utf-8") + body
        )

        part = None
        for candidate in message.iter_parts():
            if candidate.get_filename() or candidate.get_param("name", header="content-disposition") == "file":
                part = candidate
                break
        if part is None:
            self.send_error_json(HTTPStatus.BAD_REQUEST, "Multipart field 'file' was not found")
            return

        filename = sanitize_filename(part.get_filename())
        payload = part.get_payload(decode=True) or b""
        temp_path = self.config.storage_dir / f".upload-{secrets.token_hex(12)}.tmp"
        try:
            temp_path.write_bytes(payload)
            stored = store_file(self.config, filename, temp_path, len(payload))
            self.send_upload_response(stored)
        except OSError as exc:
            temp_path.unlink(missing_ok=True)
            self.send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def parse_content_length(self) -> int | None:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            self.send_error_json(HTTPStatus.LENGTH_REQUIRED, "Content-Length is required")
            return None
        try:
            return int(raw_length)
        except ValueError:
            self.send_error_json(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
            return None

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
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", file_id):
            self.send_error_json(HTTPStatus.NOT_FOUND, "Not found")
            return

        stored = load_metadata(self.config, file_id)
        file_path = self.config.files_dir / file_id
        if stored is None or not file_path.exists():
            self.send_error_json(HTTPStatus.NOT_FOUND, "File not found")
            return

        content_type = mimetypes.guess_type(stored.filename)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(stored.size))
        self.send_header(
            "Content-Disposition",
            "attachment; filename*=UTF-8''%s" % quote(stored.filename),
        )
        self.end_headers()
        if not head_only:
            with file_path.open("rb") as source:
                shutil.copyfileobj(source, self.wfile, length=CHUNK_SIZE)

    def send_home(self) -> None:
        token_hint = ""
        if self.config.upload_token:
            token_hint = " -H 'Authorization: Bearer YOUR_TOKEN'"
        body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FluxDrop</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 760px; margin: 48px auto; padding: 0 20px; line-height: 1.55; }}
    code, pre {{ background: #f4f4f5; border-radius: 6px; }}
    code {{ padding: 2px 5px; }}
    pre {{ padding: 16px; overflow: auto; }}
  </style>
</head>
<body>
  <h1>FluxDrop</h1>
  <p>Upload files with curl and get a download link back.</p>
  <pre>curl{html.escape(token_hint)} -T ./file.tar.gz {html.escape(self.base_url())}/upload/file.tar.gz</pre>
  <pre>curl{html.escape(token_hint)} -F "file=@./file.tar.gz" {html.escape(self.base_url())}/upload</pre>
</body>
</html>
"""
        payload = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def base_url(self) -> str:
        if self.config.public_base_url:
            return self.config.public_base_url
        host = self.headers.get("Host") or "localhost"
        return f"http://{host}"

    def send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
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
