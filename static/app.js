"use strict";

const $ = (id) => document.getElementById(id);
let token = "";
let files = [];
let selectedFile = null;
let revision = 0;
let deleting = false;

function notice(message = "", error = false) {
  $("notice").textContent = message;
  $("notice").classList.toggle("error", error);
  $("notice").hidden = !message;
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
  return `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 1 }).format(value)} ${units[unit]}`;
}

async function api(path, method = "GET") {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    const headers = token ? { "X-Upload-Token": token } : {};
    const response = await fetch(path, { method, headers, cache: "no-store", signal: controller.signal });
    const data = await response.json();
    if (!response.ok || !data.ok) {
      const error = new Error(data.error || "请求失败");
      error.status = response.status;
      throw error;
    }
    return data;
  } finally { clearTimeout(timer); }
}

function lock(unconfigured = false) {
  revision++;
  token = "";
  files = [];
  $("token").value = "";
  $("file-list").replaceChildren();
  $("table-wrap").hidden = true;
  $("empty-state").hidden = true;
  $("auth-panel").hidden = false;
  $("logout").hidden = true;
  $("refresh").disabled = true;
  $("refresh").textContent = "刷新列表";
  $("unlock").disabled = false;
  $("auth-form").hidden = unconfigured;
  $("setup-help").hidden = !unconfigured;
  $("auth-title").textContent = unconfigured ? "文件管理尚未启用" : "解锁文件管理";
  $("auth-description").textContent = unconfigured ? "配置管理令牌后，即可查看和删除已有文件。" : "使用服务器配置的上传令牌，查看和删除文件。";
  $("summary").textContent = "查看和管理已上传的文件";
  $("delete-dialog").close();
  selectedFile = null;
}

function renderFiles() {
  const fragment = document.createDocumentFragment();
  const dateFormat = new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" });
  for (const file of files) {
    const row = document.createElement("tr");
    const nameCell = document.createElement("td");
    const name = document.createElement("div");
    name.className = "file-name";
    const type = document.createElement("span");
    type.className = "file-type";
    type.setAttribute("aria-hidden", "true");
    const extension = file.filename.includes(".") ? file.filename.split(".").pop() : "FILE";
    type.textContent = extension.slice(0, 4).toUpperCase() || "FILE";
    const info = document.createElement("div");
    info.className = "file-info";
    const title = document.createElement("strong");
    title.textContent = file.filename;
    const id = document.createElement("span");
    id.className = "file-id";
    id.textContent = file.file_id;
    info.append(title, id);
    name.append(type, info);
    nameCell.append(name);
    const size = document.createElement("td");
    size.className = "file-size";
    size.textContent = formatSize(file.size);
    const created = document.createElement("td");
    const time = document.createElement("time");
    const date = new Date(file.created_at * 1000);
    time.dateTime = date.toISOString();
    time.textContent = dateFormat.format(date);
    created.append(time);
    const actionsCell = document.createElement("td");
    const actions = document.createElement("div");
    actions.className = "file-actions";
    const download = document.createElement("a");
    download.className = "button";
    download.href = file.download_url;
    download.download = file.filename;
    download.textContent = "下载";
    download.setAttribute("aria-label", `下载 ${file.filename}`);
    const remove = document.createElement("button");
    remove.className = "button delete";
    remove.textContent = "删除";
    remove.setAttribute("aria-label", `删除 ${file.filename}`);
    remove.addEventListener("click", () => {
      selectedFile = file;
      $("delete-filename").textContent = file.filename;
      $("delete-error").hidden = true;
      $("delete-dialog").showModal();
      $("cancel-delete").focus();
    });
    actions.append(download, remove);
    actionsCell.append(actions);
    row.append(nameCell, size, created, actionsCell);
    fragment.append(row);
  }
  $("file-list").replaceChildren(fragment);
  $("table-wrap").hidden = files.length === 0;
  $("empty-state").hidden = files.length !== 0;
  $("summary").textContent = `${files.length} 个文件 · 共 ${formatSize(files.reduce((sum, file) => sum + file.size, 0))}`;
}

async function refreshFiles(initial = false) {
  const current = ++revision;
  $("refresh").disabled = true;
  $("refresh").textContent = "正在加载…";
  $("unlock").disabled = true;
  notice();
  try {
    const data = await api("/api/files");
    if (current !== revision) return;
    files = data.files;
    $("auth-panel").hidden = true;
    $("logout").hidden = false;
    $("token").value = "";
    renderFiles();
  } catch (error) {
    if (current !== revision) return;
    if (error.status === 503) lock(true);
    else if (error.status === 401) {
      lock();
      if (!initial) { notice("令牌无效或已失效，请重新输入。", true); $("token").focus(); }
    } else notice("无法加载文件列表，请检查连接后重试。", true);
  } finally {
    if (current === revision) {
      $("refresh").textContent = "刷新列表";
      $("refresh").disabled = !token;
      $("unlock").disabled = false;
    }
  }
}

$("auth-form").addEventListener("submit", (event) => {
  event.preventDefault();
  token = $("token").value;
  refreshFiles();
});
$("refresh").addEventListener("click", () => refreshFiles());
$("logout").addEventListener("click", () => { lock(); notice(); $("token").focus(); });
$("cancel-delete").addEventListener("click", () => $("delete-dialog").close());
$("delete-dialog").addEventListener("cancel", (event) => { if (deleting) event.preventDefault(); });
$("confirm-delete").addEventListener("click", async () => {
  if (!selectedFile || deleting) return;
  const file = selectedFile;
  const current = ++revision;
  deleting = true;
  $("refresh").disabled = true;
  $("confirm-delete").disabled = true;
  $("cancel-delete").disabled = true;
  $("confirm-delete").textContent = "正在删除…";
  $("delete-error").hidden = true;
  try {
    await api(`/api/files/${encodeURIComponent(file.file_id)}`, "DELETE");
    if (current !== revision) return;
    files = files.filter((item) => item.file_id !== file.file_id);
    renderFiles();
    $("delete-dialog").close();
    selectedFile = null;
    notice(`已删除「${file.filename}」。`);
    $("refresh").focus();
  } catch (error) {
    if (current !== revision) return;
    if (error.status === 401 || error.status === 503) {
      lock(error.status === 503);
      notice("管理令牌已失效或管理功能已停用，请重新验证。", true);
    } else if (error.status === 404) {
      files = files.filter((item) => item.file_id !== file.file_id);
      renderFiles();
      $("delete-dialog").close();
      selectedFile = null;
      notice("文件已不存在，列表已更新。");
    } else {
      $("delete-error").textContent = "删除请求未完成，请重试或刷新列表确认文件状态。";
      $("delete-error").hidden = false;
    }
  } finally {
    deleting = false;
    $("confirm-delete").disabled = false;
    $("cancel-delete").disabled = false;
    $("confirm-delete").textContent = "确认删除";
    if (current === revision) {
      $("refresh").textContent = "刷新列表";
      $("refresh").disabled = !token;
    }
  }
});

$("upload-example").textContent = `curl -H 'Authorization: Bearer YOUR_TOKEN' -T ./file.log ${location.origin}/upload/file.log`;
refreshFiles(true);
