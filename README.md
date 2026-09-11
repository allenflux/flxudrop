# FluxDrop

FluxDrop is a tiny self-hosted file drop service for servers where uploading files is annoying.
Start it on any internet-reachable machine, then upload from another server with `curl`.
After each upload, FluxDrop returns a download URL.

It only uses the Python standard library.

## Start

```bash
python3 app.py
```

By default FluxDrop listens on port `8090`, stores files under `./data`, and returns download links using `http://allenflux.tech:8090`.

Explicit start command:

```bash
FLUXDROP_PUBLIC_URL=http://allenflux.tech:8090 python3 app.py --host 0.0.0.0 --port 8090
```

## Docker Compose Deploy

```bash
docker compose up -d --build
```

Check logs:

```bash
docker compose logs -f
```

Stop:

```bash
docker compose down
```

The compose file maps host port `8090` to container port `8090`, stores uploaded files in the Docker volume `fluxdrop-data`, and returns links under `http://allenflux.tech:8090`.

## Upload With Curl

Upload directly (streams to disk):

```bash
curl -T ./backup.tar.gz http://allenflux.tech:8090/upload
```

When no filename is sent, FluxDrop infers the suffix from the file content and returns names like `upload.log`, `upload.json`, `upload.zip`, or `upload.txt`.

If you want the download filename to be kept:

```bash
curl -H "X-Filename: backup.tar.gz" -T ./backup.tar.gz http://allenflux.tech:8090/upload
```

Multipart form uploads also stream to disk, including large files:

```bash
curl -F "file=@./backup.tar.gz" http://allenflux.tech:8090/upload
```

For a log that is still being appended to, use a direct upload and keep its filename:

```bash
curl --fail-with-body -T ./2026-09-11.log http://allenflux.tech:8090/upload/2026-09-11.log
```

This uploads the number of bytes present when curl measures the file; later appended lines are not included. If the file may be truncated, rotated, or rewritten during the transfer, upload a stable copy instead.

Avoid `curl -F` on a growing file: curl calculates the multipart `Content-Length` before reading it, so newly appended bytes can push the closing boundary past the declared request length. FluxDrop then returns HTTP 400 with `Multipart body is missing its closing boundary` and removes the incomplete upload. This error is separate from the upload size limit. To keep using `-F`, first make a copy and upload that copy after copying has finished.

The response looks like:

```json
{
  "ok": true,
  "file_id": "abc123...",
  "filename": "upload.log",
  "size": 12345,
  "download_url": "http://allenflux.tech:8090/f/abc123.../upload.log",
  "curl": "curl -L -o upload.log http://allenflux.tech:8090/f/abc123.../upload.log"
}
```

## Download

```bash
curl -L -O "http://allenflux.tech:8090/f/FILE_ID/backup.tar.gz"
```

## File Management UI

Open `/` in your browser to see the file manager. By default, uploads, file listing, downloads, and deletion work without a token. The page loads existing files immediately, using the same startup commands as before.

The page lists existing uploads with their filenames, sizes, and upload times, newest first. You can download files or delete them after confirming the filename. Deletion permanently removes the file and its metadata and invalidates its download link. Existing uploads appear automatically; no migration is needed. The directory is a flat list of FluxDrop uploads, not a browser for arbitrary server folders.

- Files are displayed 20 per page. Select individual files or the current page; selections carry across pages.
- Download selected files as one ZIP (up to 100 files per download). The server streams the archive without loading files into memory or creating a temporary ZIP. Duplicate names receive a numeric suffix. ZIP files are packaged without compression.
- Delete selected files after confirming the filenames. Failed files remain selected for retry. Deleted rows stay in place, turn gray and show a deletion status until you refresh the list.
- Switch between Chinese and English in the page header. The browser remembers your language choice.

Start with Docker Compose as usual:

```bash
docker compose up -d --build
```

Upload without an authorization header:

```bash
curl -T ./file.log http://allenflux.tech:8090/upload/file.log
```

The original optional `FLUXDROP_UPLOAD_TOKEN` setting remains available. Only when explicitly configured does it protect uploads and management APIs; the page then prompts for it and keeps it only in page memory. Such requests accept `Authorization: Bearer TOKEN` or `X-Upload-Token: TOKEN`.

Management APIs:

- `GET /api/files` returns `{ "ok": true, "files": [...] }`, including each file's ID, filename, size, upload timestamp (`created_at`), and a relative `download_url`.
- `DELETE /api/files/FILE_ID` deletes one file and returns `{ "ok": true, "file_id": "..." }`. Missing files return HTTP 404; storage failures return HTTP 500. A partially completed deletion can be retried.
- `GET /api/files/download?file_id=ID1&file_id=ID2` streams a ZIP of the selected files. Like individual download links, known file IDs allow downloading without a token. Invalid selections return HTTP 400; missing files return HTTP 404 before a ZIP is sent.

## Configuration

Environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `FLUXDROP_HOST` | `0.0.0.0` | Listen host |
| `FLUXDROP_PORT` | `8090` | Listen port |
| `FLUXDROP_STORAGE_DIR` | `./data` | Storage directory |
| `FLUXDROP_PUBLIC_URL` | `http://allenflux.tech:8090` | Public base URL returned in upload responses, useful behind nginx or a tunnel |
| `FLUXDROP_UPLOAD_TOKEN` | empty | Optional token for uploads, file listing, and deletion; leave unset for the default token-free mode |
| `FLUXDROP_MAX_UPLOAD_MB` | `8192` | Max request body size in MiB (default 8 GiB), including multipart headers and boundaries |

With upload protection:

```bash
export FLUXDROP_UPLOAD_TOKEN='change-me'
python3 app.py
curl -H 'Authorization: Bearer change-me' -T ./file.log http://allenflux.tech:8090/upload
```

## Run As A Systemd Service

Create `/etc/systemd/system/fluxdrop.service`:

```ini
[Unit]
Description=FluxDrop file upload service
After=network.target

[Service]
WorkingDirectory=/opt/fluxdrop
ExecStart=/usr/bin/python3 /opt/fluxdrop/app.py --host 0.0.0.0 --port 8090
Restart=always
Environment=FLUXDROP_STORAGE_DIR=/var/lib/fluxdrop
Environment=FLUXDROP_PUBLIC_URL=http://allenflux.tech:8090

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now fluxdrop
```

## Notes

- Download links are public if someone knows the URL.
- Use `FLUXDROP_UPLOAD_TOKEN` if the service is exposed to the internet.
- Both `curl -T` and `curl -F` stream uploads to disk with bounded memory use.
- Multipart part headers are limited to 16 KiB. File parts use raw bytes; Base64 and quoted-printable transfer encodings are rejected.
- Uploads require `Content-Length`; chunked transfer encoding is not supported. Curl supplies the length when uploading a regular file with either command above.
- Incomplete or malformed uploads are rejected and temporary files are removed. Storage failures are logged on the server and return HTTP 500.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Frontend logic tests (Node.js is only needed for these tests, not to run FluxDrop):

```bash
node --test tests/file_actions.test.mjs
```
