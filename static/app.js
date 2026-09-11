import { PAGE_SIZE, MAX_DOWNLOAD_FILES, pageFiles, reconcileSelection, bulkDownloadUrl, deleteBatch } from "/static/file-actions.mjs";
import { translate } from "/static/i18n.mjs";

const $ = (id) => document.getElementById(id);
let language = navigator.language?.toLowerCase().startsWith("zh") ? "zh" : "en";
try {
  const saved = localStorage.getItem("fluxdrop-language");
  if (["zh", "en"].includes(saved)) language = saved;
} catch { /* Preferences are optional when storage is unavailable. */ }
const t = (key, values) => translate(language, key, values);
let token = "";
let authRequired = false;
let files = [];
let selectedIds = new Set();
let page = 1;
let loaded = false;
let loading = false;
let revision = 0;
let deleting = false;
let pendingDeletion = [];
let deleteProgress = { done: 0, total: 0 };
let deleteReport = null;
let noticeState = null;
const fileStates = new Map();
const visibleRows = new Map();
const isRemoved = (id) => ["deleted", "missing"].includes(fileStates.get(id));
const liveFiles = () => files.filter((file) => !isRemoved(file.file_id));

function showNotice(key = null, values = {}, error = false) {
  noticeState = key ? { key, values, error } : null;
  $("notice").textContent = key ? t(key, values) : "";
  $("notice").classList.toggle("error", error);
  $("notice").hidden = !key;
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
  return `${new Intl.NumberFormat(language, { maximumFractionDigits: 1 }).format(value)} ${units[unit]}`;
}

async function api(path, method = "GET") {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    const headers = token ? { "X-Upload-Token": token } : {};
    const response = await fetch(path, { method, headers, cache: "no-store", signal: controller.signal });
    const data = await response.json();
    if (!response.ok || !data.ok) {
      const error = new Error(data.error || t("requestError"));
      error.status = response.status;
      throw error;
    }
    return data;
  } finally { clearTimeout(timer); }
}

function updateUploadExample() {
  const auth = authRequired ? " -H 'Authorization: Bearer YOUR_TOKEN'" : "";
  $("upload-example").textContent = `curl${auth} -T ./file.log ${location.origin}/upload/file.log`;
  $("multipart-example").textContent = `curl${auth} -F "file=@./file.log" ${location.origin}/upload`;
}

function updateControls() {
  const view = pageFiles(files, page);
  const selectable = view.items.filter((file) => !isRemoved(file.file_id));
  const checked = selectable.filter((file) => selectedIds.has(file.file_id)).length;
  $("select-all").checked = selectable.length > 0 && checked === selectable.length;
  $("select-all").indeterminate = checked > 0 && checked < selectable.length;
  $("select-all").disabled = deleting || !selectable.length;
  $("selection-count").textContent = t(selectedIds.size > MAX_DOWNLOAD_FILES ? "downloadLimit" : "selected", { count: selectedIds.size, limit: MAX_DOWNLOAD_FILES });
  const canDownload = selectedIds.size > 0 && selectedIds.size <= MAX_DOWNLOAD_FILES && !deleting;
  $("bulk-download").setAttribute("aria-disabled", String(!canDownload));
  $("bulk-download").href = canDownload ? bulkDownloadUrl([...selectedIds]) : "#";
  $("bulk-delete").disabled = deleting || !selectedIds.size;
  $("selection-bar").hidden = !loaded;
  $("pagination").hidden = !loaded || view.pages <= 1;
  $("page-info").textContent = t("pageInfo", { page: view.page, pages: view.pages, size: PAGE_SIZE });
  $("previous-page").disabled = deleting || view.page <= 1;
  $("next-page").disabled = deleting || view.page >= view.pages;
  $("refresh").textContent = t(loading ? "loading" : "refresh");
  $("refresh").disabled = loading || deleting || (authRequired && !token);
  $("unlock").disabled = loading;
  $("logout").disabled = deleting;
  const live = liveFiles();
  $("summary").textContent = loaded ? t("summary", { count: live.length, size: formatSize(live.reduce((sum, file) => sum + file.size, 0)) }) : t("summaryHint");
  const removed = files.length - live.length;
  $("removed-hint").hidden = removed === 0;
  $("removed-hint").textContent = t("removedHint", { count: removed });
}

function updateRow(file) {
  const controls = visibleRows.get(file.file_id);
  if (!controls) return;
  const state = fileStates.get(file.file_id);
  const removed = isRemoved(file.file_id);
  controls.row.classList.toggle("is-deleting", state === "deleting");
  controls.row.classList.toggle("is-deleted", removed);
  controls.checkbox.checked = selectedIds.has(file.file_id);
  controls.checkbox.disabled = removed || deleting;
  controls.remove.disabled = removed || deleting;
  controls.download.setAttribute("aria-disabled", String(removed));
  controls.download.tabIndex = removed ? -1 : 0;
  controls.status.classList.toggle("file-status", Boolean(state));
  controls.status.textContent = state ? t(`${state}Status`) : file.file_id;
}

function lock() {
  revision++;
  authRequired = true;
  token = "";
  loaded = false;
  loading = false;
  files = [];
  selectedIds.clear();
  fileStates.clear();
  visibleRows.clear();
  pendingDeletion = [];
  $("token").value = "";
  $("file-list").replaceChildren();
  $("table-wrap").hidden = true;
  $("empty-state").hidden = true;
  $("loading-state").hidden = true;
  $("auth-panel").hidden = false;
  $("logout").hidden = true;
  $("delete-dialog").close();
  updateUploadExample();
  updateControls();
}

function renderFiles() {
  const view = pageFiles(files, page);
  page = view.page;
  const fragment = document.createDocumentFragment();
  visibleRows.clear();
  const dateFormat = new Intl.DateTimeFormat(language, { dateStyle: "medium", timeStyle: "short" });
  for (const file of view.items) {
    const row = document.createElement("tr");
    const nameCell = document.createElement("td");
    const name = document.createElement("div");
    name.className = "file-name";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.setAttribute("aria-label", t("selectFile", { name: file.filename }));
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) selectedIds.add(file.file_id);
      else selectedIds.delete(file.file_id);
      updateControls();
    });
    const info = document.createElement("div");
    info.className = "file-info";
    const title = document.createElement("strong");
    title.textContent = file.filename;
    const id = document.createElement("span");
    id.className = "file-id";
    id.textContent = file.file_id;
    id.title = file.file_id;
    id.setAttribute("role", "status");
    info.append(title, id);
    name.append(checkbox, info);
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
    download.textContent = t("download");
    download.setAttribute("aria-label", t("downloadFile", { name: file.filename }));
    download.addEventListener("click", (event) => { if (isRemoved(file.file_id)) event.preventDefault(); });
    const remove = document.createElement("button");
    remove.className = "button";
    remove.textContent = t("remove");
    remove.setAttribute("aria-label", t("deleteFile", { name: file.filename }));
    remove.addEventListener("click", () => openDeleteDialog([file]));
    actions.append(download, remove);
    actionsCell.append(actions);
    row.append(nameCell, size, created, actionsCell);
    visibleRows.set(file.file_id, { row, checkbox, status: id, download, remove });
    updateRow(file);
    fragment.append(row);
  }
  $("file-list").replaceChildren(fragment);
  $("table-wrap").hidden = !loaded || !files.length;
  $("empty-state").hidden = !loaded || files.length !== 0;
  updateControls();
}

function renderDeleteDialog() {
  $("delete-title").textContent = t(pendingDeletion.length === 1 ? "deleteOne" : "deleteMany", { count: pendingDeletion.length });
  const list = document.createDocumentFragment();
  for (const file of pendingDeletion) {
    const item = document.createElement("li");
    item.textContent = file.filename;
    list.append(item);
  }
  $("delete-filename").replaceChildren(list);
  $("delete-error").hidden = !deleteReport;
  $("delete-error").textContent = deleteReport ? `${t("partial", deleteReport)} ${t("retryHint")}` : "";
  $("confirm-delete").textContent = deleting ? t("deleting", deleteProgress) : t(deleteReport ? "retry" : "confirm");
  $("confirm-delete").disabled = deleting || !pendingDeletion.length;
  $("cancel-delete").disabled = deleting;
}

function openDeleteDialog(targets) {
  if (deleting) return;
  pendingDeletion = targets.filter((file) => !isRemoved(file.file_id));
  if (!pendingDeletion.length) return;
  deleteReport = null;
  renderDeleteDialog();
  $("delete-dialog").showModal();
  $("cancel-delete").focus({ preventScroll: true });
}

async function refreshFiles(initial = false) {
  if (deleting) return;
  const current = ++revision;
  loading = true;
  $("loading-state").hidden = loaded || !$("auth-panel").hidden;
  updateControls();
  showNotice();
  try {
    const data = await api("/api/files");
    if (current !== revision) return;
    files = data.files;
    fileStates.clear();
    selectedIds = reconcileSelection(selectedIds, files);
    loaded = true;
    $("auth-panel").hidden = true;
    $("logout").hidden = !authRequired;
    $("token").value = "";
    renderFiles();
  } catch (error) {
    if (current !== revision) return;
    if (error.status === 401) {
      lock();
      if (!initial) { showNotice("invalidToken", {}, true); $("token").focus(); }
    } else showNotice("listError", {}, true);
  } finally {
    if (current === revision) {
      loading = false;
      $("loading-state").hidden = true;
      updateControls();
    }
  }
}

function applyLanguage() {
  document.documentElement.lang = language === "zh" ? "zh-CN" : "en";
  $("language").value = language;
  for (const element of document.querySelectorAll("[data-i18n]")) element.textContent = t(element.dataset.i18n);
  for (const element of document.querySelectorAll("[data-i18n-placeholder]")) element.placeholder = t(element.dataset.i18nPlaceholder);
  $("pagination").setAttribute("aria-label", t("pagination"));
  $("download-frame").title = t("downloadFrame");
  if (noticeState) showNotice(noticeState.key, noticeState.values, noticeState.error);
  renderFiles();
  renderDeleteDialog();
}

$("language").addEventListener("change", () => {
  language = $("language").value;
  try { localStorage.setItem("fluxdrop-language", language); } catch { /* Optional preference. */ }
  applyLanguage();
});
$("auth-form").addEventListener("submit", (event) => {
  event.preventDefault();
  token = $("token").value;
  refreshFiles();
});
$("refresh").addEventListener("click", () => refreshFiles());
$("logout").addEventListener("click", () => { lock(); showNotice(); $("token").focus(); });
$("previous-page").addEventListener("click", () => { page--; renderFiles(); });
$("next-page").addEventListener("click", () => { page++; renderFiles(); });
$("select-all").addEventListener("change", () => {
  for (const file of pageFiles(files, page).items) {
    if (isRemoved(file.file_id)) continue;
    if ($("select-all").checked) selectedIds.add(file.file_id);
    else selectedIds.delete(file.file_id);
    updateRow(file);
  }
  updateControls();
});
$("bulk-delete").addEventListener("click", () => openDeleteDialog(liveFiles().filter((file) => selectedIds.has(file.file_id))));
$("bulk-download").addEventListener("click", (event) => {
  if ($("bulk-download").getAttribute("aria-disabled") === "true") event.preventDefault();
  else showNotice("downloadStarted", { count: selectedIds.size });
});
$("download-frame").addEventListener("load", () => {
  try {
    const text = $("download-frame").contentDocument?.body?.textContent;
    if (text && JSON.parse(text).ok === false) showNotice("downloadError", {}, true);
  } catch { /* ZIP responses are handled by the browser's download manager. */ }
});
$("cancel-delete").addEventListener("click", () => $("delete-dialog").close());
$("delete-dialog").addEventListener("cancel", (event) => { if (deleting) event.preventDefault(); });
$("confirm-delete").addEventListener("click", async () => {
  if (!pendingDeletion.length || deleting) return;
  const targets = [...pendingDeletion];
  const current = ++revision;
  deleting = true;
  loading = false;
  deleteReport = null;
  deleteProgress = { done: 0, total: targets.length };
  updateControls();
  for (const file of files) updateRow(file);
  renderDeleteDialog();
  try {
    const result = await deleteBatch(targets, async (file) => {
      fileStates.set(file.file_id, "deleting");
      updateRow(file);
      try {
        await api(`/api/files/${encodeURIComponent(file.file_id)}`, "DELETE");
        if (current === revision) fileStates.set(file.file_id, "deleted");
      } catch (error) {
        if (current === revision) {
          if (error.status === 404) fileStates.set(file.file_id, "missing");
          else fileStates.delete(file.file_id);
        }
        throw error;
      } finally {
        if (current === revision) {
          if (isRemoved(file.file_id)) selectedIds.delete(file.file_id);
          updateRow(file);
          updateControls();
        }
      }
    }, (done, total) => {
      deleteProgress = { done, total };
      $("confirm-delete").textContent = t("deleting", deleteProgress);
    }, () => current === revision);
    if (current !== revision) return;
    if (result.authFailed) {
      lock();
      showNotice("deleteAuth", { count: result.deleted.length }, true);
      return;
    }
    pendingDeletion = [...result.failed, ...result.pending];
    if (pendingDeletion.length) {
      deleteReport = { deleted: result.deleted.length, missing: result.missing.length, failed: result.failed.length, pending: result.pending.length };
      showNotice("partial", deleteReport, true);
    } else {
      $("delete-dialog").close();
      if (result.missing.length && !result.deleted.length) showNotice("missing", { count: result.missing.length });
      else if (result.missing.length) showNotice("partial", { deleted: result.deleted.length, missing: result.missing.length, failed: 0, pending: 0 });
      else showNotice("deleted", { count: result.deleted.length });
    }
  } finally {
    deleting = false;
    updateControls();
    for (const file of files) updateRow(file);
    renderDeleteDialog();
    if (!$("delete-dialog").open) $("refresh").focus({ preventScroll: true });
  }
});

applyLanguage();
updateUploadExample();
refreshFiles(true);
