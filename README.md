# FluxDrop

FluxDrop is a tiny self-hosted file drop service for servers where uploading files is annoying.
Start it on any internet-reachable machine, then upload from another server with `curl`.
After each upload, FluxDrop returns a download URL.

The default local-only mode uses the Python standard library. Optional S3 cold storage uses Boto3 and cryptography.

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

## Upload a Folder

Choose **Choose folder** in the web UI to upload any folder with its nested files. Folder names are arbitrary; no name is reserved or hardcoded. Existing single-file curl commands are unchanged, and no curl client modifications or wrappers are required.

For command-line directory uploads, use the server's existing `tar` and `curl`. Change only `target` and stream the folder straight to FluxDrop; no extra program, client wrapper, or local temporary archive is needed:

```bash
target="./your-folder"
(set -o pipefail; COPYFILE_DISABLE=1 tar -C "$(dirname "$target")" -czf - -- "$(basename "$target")" | curl --fail -T - http://allenflux.tech:8090/upload-directory)
```

Run this in Bash or Zsh. The quoted path supports spaces and Unicode names. The subshell reports a failure if tar or curl fails without changing your shell settings. COPYFILE_DISABLE disables macOS metadata sidecars; it has no effect on Linux tar. Nested files, hidden files and empty directories are preserved. The same copyable command appears on the web page and uses that page's server address. If authentication is enabled, add your usual `-H 'Authorization: Bearer TOKEN'` to curl.

`/upload-directory` also accepts an existing ZIP, tar, or tar.gz containing one root folder with `curl --fail -T ./your-folder.zip URL/upload-directory`. Uploading an archive to the existing `/upload` endpoint without a directory header still stores it as an ordinary file.

`curl -F "file=@./your-folder"` cannot read a directory as a file and may fail locally with curl error 43. A server change cannot make that exact command recursively upload a folder. Curl's `@` form syntax attaches a file; see the [curl manual](https://curl.se/docs/manpage.html#-F).

Folders appear alongside files in the web UI. Open a folder to browse its children and subfolders, preview supported files, or download an individual file. The main folder download is a ZIP containing the original root folder and its contents. Deleting a folder removes the whole upload and invalidates its child links. Batch downloads contain each selected folder as a named ZIP inside the batch archive.

Folder uploads are atomic and stored as ZIP archives without extracting their paths onto the server. Streamed tar/tar.gz uploads are converted to ZIP on the server. ZIP stored/deflate compression and ZIP64 are supported. Each upload accepts up to 10,000 entries, including implicit directories, and up to 64 MiB of ZIP directory metadata. Both the request body and total expanded file content must fit `FLUXDROP_MAX_UPLOAD_MB`. Unsafe paths, duplicate entries, conflicting file/folder paths, symbolic links, special files and corrupt archives are rejected. Temporary archive construction requires available server disk space; uploading from a stable directory is recommended.

## File Management UI

Open `/` in your browser to see the file manager. By default, uploads, file listing, downloads, and deletion work without a token. The page loads existing files immediately, using the same startup commands as before.

The page lists existing file and folder uploads with their names, sizes, and upload times, newest first. You can open folders, preview files, copy their download links, download them, or delete them after confirming the name. Deletion permanently removes the upload and its metadata and invalidates its links. Existing uploads appear automatically; no migration is needed. Folder sizes in the main list are stored ZIP sizes; child file sizes are their uncompressed sizes. Browsing is limited to uploaded folders.

- Drop local files or folders onto the page, or use **Choose files** / **Choose folder**. Each top-level folder uploads as one item with its hierarchy preserved. Multiple uploads run one at a time with progress and individual download links; the list refreshes automatically. Folder selection uses the browser's directory picker, which cannot include empty directories; drag and drop in browsers supporting directory entries, or the command-line directory upload, preserves empty directories.
- Write or paste content into **Write or paste text**, choose a filename, then click **Save and create link**. Text is stored as UTF-8; blank names use `note.txt`, and names without an extension get `.txt`. Saved files use the same storage, preview, download and deletion features as other uploads. Failed saves preserve the draft.
- **Preview** opens text, images, PDFs and browser-supported audio/video in a dialog. Text previews show up to the first 256 KiB; download the file for its complete contents. HTML, SVG, Markdown and source code are displayed as plain text. Formats without a browser preview (such as Office documents and archives) retain their download action. Media support depends on the browser's codecs and PDF viewer.
- **Copy link** works for both new and existing files. If automatic copying is unavailable, the page exposes a selectable link for manual copying.
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

- `GET /api/files` returns `{ "ok": true, "files": [...] }`, including each file's ID, filename, size, upload timestamp (`created_at`), relative `download_url`, `preview_type`, and relative `preview_url`. Upload responses also include the preview fields.
- `GET /p/FILE_ID/FILENAME` and `HEAD /p/FILE_ID/FILENAME` serve an inline preview. Preview links, like download links, work without a token when the file ID is known. Text responses include `X-Preview-Truncated: true` when limited to 256 KiB; supported media accepts single byte ranges for seeking. Unsupported formats return HTTP 415, missing files return HTTP 404, and invalid or unsatisfiable media ranges return HTTP 416.
- `DELETE /api/files/FILE_ID` deletes one file and returns `{ "ok": true, "file_id": "..." }`. Missing files return HTTP 404; storage failures return HTTP 500. A partially completed deletion can be retried.
- `GET /api/files/download?file_id=ID1&file_id=ID2` streams a ZIP of the selected files. Like individual download links, known file IDs allow downloading without a token. Invalid selections return HTTP 400; missing files return HTTP 404 before a ZIP is sent.
- `POST /upload` with `X-FluxDrop-Directory: files` accepts multipart file parts with root-relative filenames such as `your-folder/sub/data.json`; empty-directory parts have a trailing `/` and an empty body. This is the web folder uploader's request. `POST /upload-directory` is an alias.
- `PUT /upload-directory` accepts ZIP, tar, or tar.gz containing one root directory, including chunked streams from `curl -T -`. The existing `/upload` also accepts directory ZIPs when explicitly marked with `X-FluxDrop-Directory: zip` (raw PUT or a ZIP file in multipart POST). All directory uploads use the same optional upload token and size limits as regular uploads. Unmarked `/upload` requests keep their original file behavior.
- Folder list/upload results include `kind: "directory"`, `filename` (the root folder name), `download_filename` (the ZIP name), `file_count`, and `browse_url`. Existing file records remain compatible.
- `GET /api/directories/FILE_ID?path=subfolder` returns immediate `entries` with names, relative paths, kinds, sizes, and child file preview/download links. Omit `path` to browse the root. Listing requires the optional management token. Known child preview/download URLs, like other download links, work without a token. Child files support the same preview types and media byte ranges as ordinary files. Deletion operates on the entire uploaded folder.

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

## 冷热存储

冷热存储默认关闭，现有启动方式和文件链接继续可用。启用后，距最近内容访问、内容修改和上传时间均超过七天的文件会自动迁冷；页面的每行操作和选择工具栏提供 **设为冷文件**，可手动或批量提前迁冷。手动操作仅跳过时间门槛，上传、回读验证与安全删除规则相同。页面显示冷热状态、任务进度、失败原因，并支持取消或重试。

文件按原始块独立压缩、AES-GCM 加密后保存到私有 S3 归档。全部块回读验证完成后才释放本地原件。下载和有效在线预览会合并触发一次完整恢复；恢复后保留远端副本，下次迁冷可复用。文件列表、目录浏览和 HEAD 不会触发恢复。目录按整个上传 ZIP 归档，读取一个子文件会恢复整个 ZIP。具体方案见 [冷热存储设计文档](docs/cold-storage-design.md)。

S3 超时或暂时故障会退避重试；迁冷失败保留本地原件。冷文件恢复未能在等待时间内完成时，公开内容接口返回 `503` 与 `Retry-After`，容量不足返回 `507`，不会返回残缺文件。已有热文件仍可读取。冷归档仅使用 S3 Standard；静态域名不参与加密归档的读取。

启用时先校验本地配置、可选依赖和加密主密钥；缺失或无效会拒绝启动。S3 客户端与连接在后台任务首次使用时初始化，网络故障不会阻止已有热文件的 HTTP 服务启动。冷存储的进程锁使用 Linux/macOS 的文件锁，Docker 部署运行在 Linux。

删除会先持久化墓碑并隐藏项目，再清理远端对象。若远端清理因权限错误停止，修复 IAM 权限后重启服务即可继续，也可以使用原文件 ID 再次调用删除接口；重复删除墓碑项目会安全重试清理。

启用后的接口：

- `POST /api/files/FILE_ID/cold` 提交手动迁冷，返回 `202`、任务 ID 和存储状态；重复提交复用现有任务。沿用管理令牌权限。
- `GET /api/files/FILE_ID/storage` 返回 `storage_state`、`storage_job` 和最后内容访问时间，不触发回热。已知文件 ID 可公开查询，响应不包含凭据、主密钥或远端对象地址。
- `POST /api/files/FILE_ID/restore` 提交回热，供已知分享链接和前端等待恢复；可用 `preview=1` 检查预览格式，目录子文件另带 URL 编码的 `path`。无效路径或预览请求不回热。
- `POST /api/storage-jobs/JOB_ID/cancel` 取消尚未驱逐的迁冷任务，沿用管理令牌权限。重复取消保持幂等；驱逐已经提交时返回 `409` 和 `storage_cancel_committed`。

### 部署与密钥

先轮换曾公开粘贴的 AWS 访问密钥。使用标准 [Boto3 凭据来源](https://docs.aws.amazon.com/boto3/latest/guide/credentials.html)：IAM 角色、环境变量或共享凭据文件。服务需要归档前缀内对象的 `GetObject`、`PutObject`、`DeleteObject`，以及对应前缀的 `ListBucket` 权限。归档 bucket 保持私有，凭据和加密主密钥都不写入源码或前端。

生成独立的 32 字节加密主密钥；以下命令拒绝覆盖已有密钥文件：

```bash
mkdir -p secrets
chmod 700 secrets
python3 - <<'PY'
import base64
import json
import os

with open("secrets/keys.json", "x", encoding="utf-8") as target:
    os.chmod(target.name, 0o600)
    json.dump({"active": "v1", "keys": {"v1": base64.b64encode(os.urandom(32)).decode("ascii")}}, target)
PY
cp .env.example .env
```

编辑 `.env` 的 bucket、region 和 endpoint，并设置 `FLUXDROP_COLD_STORAGE_ENABLED=1`。Docker Compose 自动读取 `.env`，把 `secrets/` 只读挂载到容器内；加密 keyring 固定读取 `secrets/keys.json`。在 `secrets/aws-credentials` 保存 AWS 共享凭据格式（或使用 `.env` 中标准 `AWS_ACCESS_KEY_ID`、`AWS_SECRET_ACCESS_KEY`，临时凭据还需 `AWS_SESSION_TOKEN`）：

```ini
[default]
aws_access_key_id = replace-with-rotated-access-key
aws_secret_access_key = replace-with-rotated-secret-key
```

```bash
chmod 600 .env secrets/aws-credentials
docker compose up -d --build
```

原生 Python 部署先安装可选依赖，并显式加载环境变量；应用不会自行读取 `.env`：

```bash
python3 -m pip install -r requirements.txt
set -a
. ./.env
set +a
export AWS_SHARED_CREDENTIALS_FILE="$PWD/secrets/aws-credentials"
python3 app.py
```

把 keyring 与整个数据目录（包括元数据数据库）分别备份到 VPS 之外。AWS 凭据可以轮换；丢失归档使用的加密主密钥则无法恢复文件。keyring 的 `active` 决定新归档的主密钥 ID；轮换时添加新 ID 并切换 `active`，保留旧 ID 与旧密钥以恢复已有归档。不要在仍有冷文件时关闭功能或移除其数据库、keyring。

### 冷存储配置

| 环境变量 | 默认值 | 含义 |
| --- | --- | --- |
| `FLUXDROP_COLD_STORAGE_ENABLED` | `0` | 显式启用冷热存储 |
| `FLUXDROP_S3_BUCKET` | 空 | 私有 bucket；启用时必填 |
| `FLUXDROP_S3_REGION` | `ap-northeast-3` | S3 区域 |
| `FLUXDROP_S3_ENDPOINT` | 空 | 可选 S3 API endpoint；空值使用 SDK 默认地址 |
| `FLUXDROP_S3_PREFIX` | `fluxdrop-cold/` | 归档对象的专用前缀 |
| `FLUXDROP_ENCRYPTION_KEY_FILE` | 空 | UTF-8 JSON keyring 路径；启用时必填 |
| `FLUXDROP_ENCRYPTION_KEY_ID` | keyring 的 `active` | 新归档使用的主密钥 ID |
| `FLUXDROP_COLD_IDLE_DAYS` | `7` | 自动迁冷的闲置天数 |
| `FLUXDROP_COLD_BLOCK_MB` | `32` | 每块原始数据 MiB；最大 64 |
| `FLUXDROP_COLD_MAX_OBJECT_MB` | `100` | 最终密文对象上限 MiB |
| `FLUXDROP_COLD_DISK_RESERVE_MB` | `512` | 上传和恢复不得占用的磁盘余量 MiB |
| `FLUXDROP_COLD_WAIT_SECONDS` | `15` | 内容请求等待恢复的上限秒数 |
| `FLUXDROP_COLD_SCAN_SECONDS` | `300` | 自动迁冷检查间隔秒数 |
| `FLUXDROP_COLD_RETRY_BASE_SECONDS` | `2` | 后台重试的初始退避秒数 |
| `FLUXDROP_COLD_RETRY_MAX_SECONDS` | `300` | 后台重试退避上限秒数 |
| `FLUXDROP_S3_CONNECT_TIMEOUT` | `5` | 单次 S3 连接超时秒数 |
| `FLUXDROP_S3_READ_TIMEOUT` | `30` | 单次 S3 读取超时秒数 |

Docker 的 `FLUXDROP_SECRETS_DIR` 是宿主机密钥目录，默认 `./secrets`。原生部署用 `FLUXDROP_ENCRYPTION_KEY_FILE` 指定实际路径。大小配置按 MiB（1,048,576 字节）计算；恢复前需要预约完整原文件空间，单个文件大于可分配磁盘空间时不能完整回热。冷热存储会释放 VPS 副本，不等同于立即备份所有新上传文件。

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

- Download and preview links are public if someone knows the URL.
- Use `FLUXDROP_UPLOAD_TOKEN` if the service is exposed to the internet.
- Both `curl -T` and `curl -F` stream uploads to disk with bounded memory use.
- Multipart part headers are limited to 16 KiB. File parts use raw bytes; Base64 and quoted-printable transfer encodings are rejected.
- Regular file uploads require `Content-Length`; curl supplies it when uploading a regular file. Directory uploads also accept chunked transfer encoding for the `tar | curl -T -` pipeline, enforcing the upload limit as bytes arrive. Conflicting length headers, malformed chunks and truncated streams are rejected.
- Incomplete or malformed uploads are rejected and temporary files are removed. Storage failures are logged on the server and return HTTP 500.

## Tests

Install the optional dependencies to run the complete suite, including cold-storage tests. These tests use an in-memory remote store and need no AWS credentials.

```bash
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -v
```

Frontend logic tests (Node.js is only needed for these tests, not to run FluxDrop):

```bash
node --test tests/*.test.mjs
```
