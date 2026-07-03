# FluxDrop

FluxDrop is a tiny self-hosted file drop service for servers where uploading files is annoying.
Start it on any internet-reachable machine, then upload from another server with `curl`.
After each upload, FluxDrop returns a download URL.

It only uses the Python standard library.

## Start

```bash
python3 app.py
```

By default FluxDrop listens on port `8090`, stores files under `./data`, and returns download links using `http://allenflux.tech`.

Explicit start command:

```bash
FLUXDROP_PUBLIC_URL=http://allenflux.tech python3 app.py --host 0.0.0.0 --port 8090
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

The compose file maps host port `8090` to container port `8090`, stores uploaded files in the Docker volume `fluxdrop-data`, and returns links under `http://allenflux.tech`.

## Upload With Curl

Best for large files:

```bash
curl -T ./backup.tar.gz http://allenflux.tech/upload/backup.tar.gz
```

Also supported:

```bash
curl -F "file=@./backup.tar.gz" http://allenflux.tech/upload
```

The response looks like:

```json
{
  "ok": true,
  "file_id": "abc123...",
  "filename": "backup.tar.gz",
  "size": 12345,
  "download_url": "http://allenflux.tech/f/abc123.../backup.tar.gz",
  "curl": "curl -L -o backup.tar.gz http://allenflux.tech/f/abc123.../backup.tar.gz"
}
```

## Download

```bash
curl -L -O "http://allenflux.tech/f/FILE_ID/backup.tar.gz"
```

## Configuration

Environment variables:

| Variable | Default | Description |
| --- | --- | --- |
| `FLUXDROP_HOST` | `0.0.0.0` | Listen host |
| `FLUXDROP_PORT` | `8090` | Listen port |
| `FLUXDROP_STORAGE_DIR` | `./data` | Storage directory |
| `FLUXDROP_PUBLIC_URL` | `http://allenflux.tech` | Public base URL returned in upload responses, useful behind nginx or a tunnel |
| `FLUXDROP_UPLOAD_TOKEN` | empty | Optional upload token |
| `FLUXDROP_MAX_UPLOAD_MB` | `1024` | Max upload size in MB |

With upload protection:

```bash
export FLUXDROP_UPLOAD_TOKEN='change-me'
python3 app.py
curl -H 'Authorization: Bearer change-me' -T ./file.log http://allenflux.tech/upload/file.log
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
Environment=FLUXDROP_PUBLIC_URL=http://allenflux.tech
Environment=FLUXDROP_UPLOAD_TOKEN=change-me

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
- Use `curl -T` for large files because it streams directly to disk.
# flxudrop
