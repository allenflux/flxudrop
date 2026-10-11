import { PAGE_SIZE, MAX_DOWNLOAD_FILES, pageFiles, reconcileSelection, bulkDownloadUrl, deleteBatch } from "/static/file-actions.mjs";
import { translate } from "/static/i18n.mjs";
import { setupUploads, copyText } from "/static/browser-upload.mjs";
import { setupPreview, localFileUrl } from "/static/file-preview.mjs";
import { setupDirectoryBrowser } from "/static/directory-browser.mjs";
import { createStorageController, hasStorage, isHot, activeStorageJob, storageProgress, storageFileId, restoreBatch } from "/static/file-storage.mjs";

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
let storageEnabled = false;
let storageError = null;
let storageSession = 0;
let storageTimer = null;
let storagePollingSession = null;
let preparingDownload = false;
let downloadController = null;
const coldSubmitting = new Set();
const storageErrors = new Map();
const directoryCommandNotices = new Map();
const fileStates = new Map();
const visibleRows = new Map();
const isRemoved = (id) => ["deleted", "missing"].includes(fileStates.get(id));
const liveFiles = () => files.filter((file) => !isRemoved(file.file_id));
const storage = createStorageController({ request: storageRequest, onUpdate: updateStorage });
const preview = setupPreview({ t, formatSize, ensureAvailable, onDownload: downloadFile, storageErrorMessage });
const directoryBrowser = setupDirectoryBrowser({
  t, formatSize, api,
  onPreview: (file) => preview.open(file),
  onDownload: downloadFile,
  onAuthRequired: () => { lock(); showNotice("invalidToken", {}, true); $("token").focus(); },
});
const uploads = setupUploads({
  t, formatSize,
  getToken: () => token,
  canUpload: () => loaded && !deleting && !(authRequired && !token),
  onAuthRequired: () => { lock(); showNotice("invalidToken", {}, true); $("token").focus(); },
  onUploaded: async () => { page = 1; await refreshFiles(); },
  onBusyChange: () => { updateControls(); for (const file of files) updateRow(file); },
});

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

async function api(path, method = "GET", { signal } = {}) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  if (signal?.aborted) controller.abort();
  else signal?.addEventListener("abort", abort, { once: true });
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    const headers = token ? { "X-Upload-Token": token } : {};
    const response = await fetch(path, { method, headers, cache: "no-store", signal: controller.signal });
    const data = await response.json();
    if (!response.ok || !data.ok) {
      const error = new Error(data.error || t("requestError"));
      error.status = response.status;
      error.code = data.error_code;
      throw error;
    }
    return data;
  } finally { clearTimeout(timer); signal?.removeEventListener("abort", abort); }
}

function storageErrorMessage(error) {
  if (error.status === 507 || ["storage_capacity", "insufficient_storage", "disk_full", "capacity"].includes(error.code)) return t("storageCapacity");
  if (error.status === 409 && !["storage_configuration", "storage_disabled"].includes(error.code)) return t("storageCancelLate");
  if (error.code === "restore_timeout") return t("storageWaitTimeout");
  return t("storageOperationError", { detail: error.message || t("uploadNetworkError") });
}

async function storageRequest(...args) {
  const session = storageSession;
  const snapshot = await api(...args);
  if (session !== storageSession) throw new DOMException("Aborted", "AbortError");
  return snapshot;
}

function updateStorage(id, snapshot) {
  const file = files.find((item) => item.file_id === id);
  if (!file || isRemoved(id)) return;
  storageErrors.delete(id);
  for (const key of ["storage_state", "storage_job", "last_access_at", "last_content_access_at", "content_modified_at", "remote_verified"]) {
    if (Object.hasOwn(snapshot, key)) file[key] = snapshot[key];
  }
  updateRow(file);
  updateControls();
  scheduleStoragePoll();
}

function scheduleStoragePoll() {
  if (storageTimer !== null || storagePollingSession === storageSession || !loaded) return;
  const targets = liveFiles().filter((file) => activeStorageJob(file) || ["archiving", "evicting", "restoring"].includes(file.storage_state));
  if (!targets.length) return;
  const session = storageSession;
  storageTimer = setTimeout(async () => {
    storageTimer = null;
    storagePollingSession = session;
    try {
      for (const file of targets) {
        if (session !== storageSession) return;
        if (isRemoved(file.file_id)) continue;
        try { await storage.read(file); }
        catch (error) {
          if (session !== storageSession) return;
          storageErrors.set(file.file_id, error);
          updateRow(file);
        }
      }
    } finally {
      if (storagePollingSession === session) storagePollingSession = null;
      if (session === storageSession) scheduleStoragePoll();
    }
  }, 1500);
}

async function ensureAvailable(file, options = {}) {
  const parent = files.find((item) => item.file_id === storageFileId(file));
  // Child URLs keep their path while adopting the parent's latest storage state.
  const current = parent ? { ...file, storage_state: parent.storage_state, storage_job: parent.storage_job } : file;
  return storage.ensureHot(current, options);
}

function startDownload(url) { $("download-frame").src = url; }

async function downloadFile(file, event) {
  if (event && (isRemoved(storageFileId(file)) || !localFileUrl(file.download_url, "/f/"))) { event.preventDefault(); return; }
  if (!hasStorage(file)) return;
  event?.preventDefault();
  const session = storageSession;
  showNotice("storagePreparing", { name: file.filename });
  try {
    await ensureAvailable(file, { onProgress: (snapshot) => {
      if (session === storageSession && !isHot(snapshot)) showNotice("storageRestoringNamed", { name: file.filename, percent: storageProgress(snapshot.storage_job) ?? 0 });
    } });
    if (session !== storageSession || isRemoved(storageFileId(file))) return;
    const url = localFileUrl(file.download_url, "/f/");
    if (url) { startDownload(url); showNotice(); }
  } catch (error) {
    if (session === storageSession && error.name !== "AbortError") showNotice("storageMessage", { detail: storageErrorMessage(error) }, true);
  }
}

async function setCold(file) {
  if (!storageEnabled || coldSubmitting.has(file.file_id) || isRemoved(file.file_id) || !isHot(file) || activeStorageJob(file)) return false;
  const session = storageSession;
  coldSubmitting.add(file.file_id);
  storageErrors.delete(file.file_id);
  updateRow(file);
  updateControls();
  try {
    const snapshot = await storage.setCold(file);
    if (session !== storageSession) return false;
    // A minimal 202 response may carry only job_id until the first status read.
    if (!snapshot.storage_job && snapshot.job_id && !["cold"].includes(snapshot.storage_state)) {
      file.storage_job = { job_id: snapshot.job_id, kind: "archive", state: "queued", progress: 0, error: null, cancellable: true };
    }
    updateRow(file);
    scheduleStoragePoll();
    return true;
  } catch (error) {
    if (session !== storageSession) return false;
    if (error.status === 401) { lock(); showNotice("invalidToken", {}, true); return false; }
    storageErrors.set(file.file_id, error);
    updateRow(file);
    return false;
  } finally {
    if (session === storageSession) {
      coldSubmitting.delete(file.file_id);
      updateRow(file);
      updateControls();
    }
  }
}

async function cancelStorage(file) {
  try { await storage.cancel(file); storageErrors.delete(file.file_id); updateRow(file); }
  catch (error) {
    if (error.status === 401) { lock(); showNotice("invalidToken", {}, true); return; }
    storageErrors.set(file.file_id, error);
    updateRow(file);
  }
}

async function retryStorage(file) {
  storageErrors.delete(file.file_id);
  if (file.storage_job?.kind === "restore" || ["cold", "restoring"].includes(file.storage_state)) {
    const session = storageSession;
    try { await ensureAvailable(file); }
    catch (error) { if (session === storageSession) { storageErrors.set(file.file_id, error); updateRow(file); } }
  } else await setCold(file);
}

function updateUploadExample() {
  const auth = authRequired ? " -H 'Authorization: Bearer YOUR_TOKEN'" : "";
  $("upload-example").textContent = `curl${auth} -T ./file.log ${location.origin}/upload/file.log`;
  $("multipart-example").textContent = `curl${auth} -F "file=@./file.log" ${location.origin}/upload`;
  $("directory-example").textContent = `(set -o pipefail;\n  COPYFILE_DISABLE=1 tar -C "$(dirname "$target")" \\\n    -czf - -- "$(basename "$target")" \\\n    | curl${auth} --fail -T - ${location.origin}/upload-directory)`;
}

function updateControls() {
  const busy = deleting || uploads.isBusy();
  const view = pageFiles(files, page);
  const selectable = view.items.filter((file) => !isRemoved(file.file_id));
  const checked = selectable.filter((file) => selectedIds.has(file.file_id)).length;
  $("select-all").checked = selectable.length > 0 && checked === selectable.length;
  $("select-all").indeterminate = checked > 0 && checked < selectable.length;
  $("select-all").disabled = busy || !selectable.length;
  $("selection-count").textContent = t(selectedIds.size > MAX_DOWNLOAD_FILES ? "downloadLimit" : "selected", { count: selectedIds.size, limit: MAX_DOWNLOAD_FILES });
  const canDownload = selectedIds.size > 0 && selectedIds.size <= MAX_DOWNLOAD_FILES && !deleting && !preparingDownload;
  $("bulk-download").setAttribute("aria-disabled", String(!canDownload));
  $("bulk-download").href = canDownload ? bulkDownloadUrl([...selectedIds]) : "#";
  $("bulk-delete").disabled = busy || !selectedIds.size;
  $("bulk-cold").disabled = busy || !storageEnabled || !liveFiles().some((file) => selectedIds.has(file.file_id) && isHot(file) && !activeStorageJob(file) && !coldSubmitting.has(file.file_id));
  $("bulk-cold").title = storageEnabled ? t("setCold") : t("storageDisabled");
  $("storage-config").hidden = !loaded || storageEnabled;
  $("storage-config").textContent = storageError ? t("storageConfigError", { detail: storageError }) : t("storageDisabled");
  $("cancel-download-wait").hidden = !preparingDownload;
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
  uploads.render();
}

function updateRow(file) {
  const controls = visibleRows.get(file.file_id);
  if (!controls) return;
  const state = fileStates.get(file.file_id);
  const removed = isRemoved(file.file_id);
  controls.row.classList.toggle("is-deleting", state === "deleting");
  controls.row.classList.toggle("is-deleted", removed);
  controls.checkbox.checked = selectedIds.has(file.file_id);
  controls.checkbox.disabled = removed || deleting || uploads.isBusy();
  controls.remove.disabled = removed || deleting || uploads.isBusy();
  controls.preview.disabled = removed || state === "deleting";
  controls.copy.disabled = removed || state === "deleting";
  controls.download.setAttribute("aria-disabled", String(removed));
  controls.download.tabIndex = removed ? -1 : 0;
  controls.status.classList.toggle("file-status", Boolean(state));
  controls.status.textContent = state ? t(`${state}Status`) : file.file_id;
  updateStorageRow(file, controls, removed);
}

function updateStorageRow(file, controls, removed) {
  const job = file.storage_job;
  const active = activeStorageJob(file);
  const current = file.storage_state || "hot";
  const submitting = coldSubmitting.has(file.file_id);
  const error = storageErrors.get(file.file_id) || (job?.state === "failed" ? Object.assign(new Error(job.error || t("requestError")), { code: job.error_code }) : null);
  const labelKey = { hot: "storageHot", hot_backed: "storageHotBacked", archiving: "storageArchiving", evicting: "storageEvicting", cold: "storageCold", restoring: "storageRestoring" }[current] || "storageHot";
  let label = t(labelKey);
  if (submitting || (active && job.state === "queued")) label = t(job?.kind === "restore" ? "storageRestoreQueued" : "storageArchiveQueued");
  else if (active && job.state === "retry") label = t("storageRetrying");
  if (active && storageProgress(job) !== null) label += ` · ${storageProgress(job)}%`;
  controls.storageStatus.textContent = label;
  controls.storageProgress.hidden = !active;
  if (active) {
    const percent = storageProgress(job);
    if (percent === null) controls.storageProgress.removeAttribute("value");
    else controls.storageProgress.value = percent;
    controls.storageProgress.setAttribute("aria-label", label);
  }
  controls.storageError.hidden = !error || removed;
  controls.storageError.textContent = error ? storageErrorMessage(error) : "";
  controls.storageRetry.hidden = !error || active || removed;
  controls.storageRetry.disabled = deleting || (!storageEnabled && isHot(file));
  controls.storageCancel.hidden = !active || !job.cancellable || removed;
  controls.storageCancel.disabled = deleting;
  controls.cold.textContent = t(current === "cold" ? "alreadyCold" : "setCold");
  controls.cold.disabled = removed || deleting || uploads.isBusy() || !storageEnabled || submitting || active || !isHot(file);
  controls.cold.title = storageEnabled ? t("setColdNamed", { name: file.filename }) : t("storageDisabled");
}

function lock() {
  revision++;
  storageSession++;
  storage.abortAll();
  downloadController?.abort();
  downloadController = null;
  preparingDownload = false;
  if (storageTimer !== null) clearTimeout(storageTimer);
  storageTimer = null;
  storageErrors.clear();
  coldSubmitting.clear();
  preview.close();
  directoryBrowser.close();
  $("link-dialog").close();
  $("share-link").value = "";
  authRequired = true;
  token = "";
  loaded = false;
  loading = false;
  files = [];
  selectedIds.clear();
  fileStates.clear();
  visibleRows.clear();
  pendingDeletion = [];
  uploads.reset();
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

const actionIcons = {
  preview: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
  folder: '<path d="M3 8V5a1 1 0 0 1 1-1h5l2 3h9a1 1 0 0 1 1 1v2M3 8v11a1 1 0 0 0 1 1h15l3-10H6L3 20"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4"/>',
  copy: '<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h3"/>',
  remove: '<path d="M3 6h18M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2M5 6l1 14a1 1 0 0 0 1 1h10a1 1 0 0 0 1-1l1-14M10 10v7m4-7v7"/>',
};

function decorateRowAction(control, icon, label) {
  control.className = `button row-action row-action-${icon}`;
  control.title = label;
  // Only the fixed icon markup enters HTML; filenames remain in text/attributes.
  control.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${actionIcons[icon]}</svg>`;
  const tooltip = document.createElement("span");
  tooltip.className = "action-tooltip";
  tooltip.setAttribute("aria-hidden", "true");
  tooltip.textContent = label;
  control.append(tooltip);
}

function renderFiles() {
  const view = pageFiles(files, page);
  page = view.page;
  const fragment = document.createDocumentFragment();
  visibleRows.clear();
  const dateFormat = new Intl.DateTimeFormat(language, { dateStyle: "medium", timeStyle: "short" });
  for (const file of view.items) {
    const isDirectory = file.kind === "directory";
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
    info.append(title);
    if (isDirectory) {
      const badge = document.createElement("span");
      badge.className = "folder-badge";
      badge.textContent = t("folderCount", { count: file.file_count ?? 0 });
      info.append(badge);
    }
    info.append(id);
    name.append(checkbox, info);
    nameCell.append(name);
    const size = document.createElement("td");
    size.className = "file-size";
    size.textContent = isDirectory ? t("folderZipSize", { size: formatSize(file.size) }) : formatSize(file.size);
    const created = document.createElement("td");
    const time = document.createElement("time");
    const date = new Date(file.created_at * 1000);
    time.dateTime = date.toISOString();
    time.textContent = dateFormat.format(date);
    created.append(time);
    const storageCell = document.createElement("td");
    storageCell.className = "storage-cell";
    const storageStatus = document.createElement("span");
    storageStatus.className = "storage-status";
    storageStatus.setAttribute("role", "status");
    const storageProgressElement = document.createElement("progress");
    storageProgressElement.className = "storage-progress";
    storageProgressElement.max = 100;
    storageProgressElement.hidden = true;
    const storageErrorElement = document.createElement("span");
    storageErrorElement.className = "storage-error field-help";
    const storageTaskActions = document.createElement("div");
    storageTaskActions.className = "storage-task-actions";
    const storageCancel = document.createElement("button");
    storageCancel.className = "button";
    storageCancel.type = "button";
    storageCancel.textContent = t("cancelStorage");
    storageCancel.addEventListener("click", () => void cancelStorage(file));
    const storageRetry = document.createElement("button");
    storageRetry.className = "button";
    storageRetry.type = "button";
    storageRetry.textContent = t("retryStorage");
    storageRetry.addEventListener("click", () => void retryStorage(file));
    storageTaskActions.append(storageCancel, storageRetry);
    storageCell.append(storageStatus, storageProgressElement, storageErrorElement, storageTaskActions);
    const actionsCell = document.createElement("td");
    const actions = document.createElement("div");
    actions.className = "file-actions row-actions";
    actions.setAttribute("role", "group");
    actions.setAttribute("aria-label", t("actions"));
    const previewButton = document.createElement("button");
    decorateRowAction(previewButton, isDirectory ? "folder" : "preview", t(isDirectory ? "openDirectory" : "preview"));
    previewButton.setAttribute("aria-label", t(isDirectory ? "openDirectoryNamed" : "previewFile", { name: file.filename }));
    previewButton.addEventListener("click", () => isDirectory ? directoryBrowser.open(file) : preview.open(file));
    const copy = document.createElement("button");
    decorateRowAction(copy, "copy", t("uploadCopy"));
    copy.setAttribute("aria-label", `${t("copyLink")} · ${file.filename}`);
    copy.addEventListener("click", async () => {
      const current = revision;
      const url = new URL(file.download_url, location.origin).href;
      const copied = await copyText(url);
      if (current !== revision) return;
      if (copied) showNotice("uploadCopied");
      else {
        $("share-link").value = url;
        $("link-dialog").showModal();
        $("share-link").focus();
        $("share-link").select();
      }
    });
    const download = document.createElement("a");
    decorateRowAction(download, "download", t(isDirectory ? "downloadZip" : "download"));
    download.href = file.download_url;
    download.download = file.download_filename || file.filename;
    download.setAttribute("aria-label", isDirectory ? `${t("downloadDirectory")} · ${file.filename}` : t("downloadFile", { name: file.filename }));
    download.addEventListener("click", (event) => void downloadFile(file, event));
    const remove = document.createElement("button");
    decorateRowAction(remove, "remove", t("remove"));
    remove.setAttribute("aria-label", t("deleteFile", { name: file.filename }));
    remove.addEventListener("click", () => openDeleteDialog([file]));
    const cold = document.createElement("button");
    cold.type = "button";
    cold.className = "button set-cold";
    cold.textContent = t("setCold");
    cold.setAttribute("aria-label", t("setColdNamed", { name: file.filename }));
    cold.addEventListener("click", () => void setCold(file));
    actions.append(previewButton, download, copy, remove, cold);
    actionsCell.append(actions);
    row.append(nameCell, size, created, storageCell, actionsCell);
    visibleRows.set(file.file_id, { row, checkbox, status: id, download, remove, preview: previewButton, copy, cold, storageStatus, storageProgress: storageProgressElement, storageError: storageErrorElement, storageCancel, storageRetry });
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
  if (deleting || uploads.isBusy()) return;
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
    storageEnabled = data.storage_enabled === true;
    storageError = data.storage_error || null;
    storageErrors.clear();
    fileStates.clear();
    selectedIds = reconcileSelection(selectedIds, files);
    loaded = true;
    $("auth-panel").hidden = true;
    $("logout").hidden = !authRequired;
    $("token").value = "";
    renderFiles();
    scheduleStoragePoll();
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
  $("language").setAttribute("aria-label", t("language"));
  for (const button of $("language").querySelectorAll("[data-language]")) {
    button.setAttribute("aria-pressed", String(button.dataset.language === language));
  }
  for (const element of document.querySelectorAll("[data-i18n]")) element.textContent = t(element.dataset.i18n);
  for (const element of document.querySelectorAll("[data-i18n-placeholder]")) element.placeholder = t(element.dataset.i18nPlaceholder);
  $("pagination").setAttribute("aria-label", t("pagination"));
  $("download-frame").title = t("downloadFrame");
  $("upload-feedback").setAttribute("aria-label", t("uploadResults"));
  $("upload-progress").setAttribute("aria-label", t("uploadProgressLabel"));
  $("share-link").setAttribute("aria-label", t("downloadLink"));
  if (noticeState) showNotice(noticeState.key, noticeState.values, noticeState.error);
  $("directory-command-manual").setAttribute("aria-label", t("allDirectoryCommands"));
  for (const [id, key] of directoryCommandNotices) $(id).textContent = t(key);
  renderFiles();
  renderDeleteDialog();
  preview.render();
  directoryBrowser.render();
}

for (const button of $("language").querySelectorAll("[data-language]")) {
  button.addEventListener("click", () => {
    if (language === button.dataset.language) return;
    language = button.dataset.language;
    try { localStorage.setItem("fluxdrop-language", language); } catch { /* Optional preference. */ }
    applyLanguage();
  });
}
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
$("bulk-cold").addEventListener("click", async () => {
  if ($("bulk-cold").disabled) return;
  const session = storageSession;
  const targets = liveFiles().filter((file) => selectedIds.has(file.file_id) && isHot(file) && !activeStorageJob(file));
  let accepted = 0;
  let failed = 0;
  for (const file of targets) {
    if (session !== storageSession) return;
    if (await setCold(file)) accepted++;
    else failed++;
    if (session === storageSession) showNotice("storageBatchResult", { accepted, failed, total: targets.length }, failed > 0);
  }
});
$("bulk-download").addEventListener("click", async (event) => {
  if ($("bulk-download").getAttribute("aria-disabled") === "true") { event.preventDefault(); return; }
  const targets = liveFiles().filter((file) => selectedIds.has(file.file_id));
  if (!targets.some(hasStorage)) { showNotice("downloadStarted", { count: targets.length }); return; }
  event.preventDefault();
  const session = storageSession;
  const controller = new AbortController();
  downloadController = controller;
  preparingDownload = true;
  updateControls();
  try {
    await restoreBatch(targets, ensureAvailable, (done, total, file) => {
      if (session === storageSession) showNotice("storageBatchRestoring", { done, total, name: file?.filename || "" });
    }, controller.signal);
    if (session !== storageSession) return;
    startDownload(bulkDownloadUrl(targets.map((file) => file.file_id)));
    showNotice("downloadStarted", { count: targets.length });
  } catch (error) {
    if (session === storageSession) showNotice(error.name === "AbortError" ? "storageWaitCancelled" : "storageMessage", { detail: storageErrorMessage(error) }, error.name !== "AbortError");
  } finally {
    if (downloadController === controller) { downloadController = null; preparingDownload = false; updateControls(); }
  }
});
$("cancel-download-wait").addEventListener("click", () => downloadController?.abort());
$("download-frame").addEventListener("load", () => {
  try {
    const text = $("download-frame").contentDocument?.body?.textContent;
    if (text) {
      const data = JSON.parse(text);
      if (data.ok === false) {
        if (data.error_code === "storage_capacity") showNotice("storageCapacity", {}, true);
        else if (data.error_code) showNotice("storageMessage", { detail: storageErrorMessage(Object.assign(new Error(data.error), { code: data.error_code })) }, true);
        else showNotice("downloadError", {}, true);
      }
    }
  } catch { /* ZIP responses are handled by the browser's download manager. */ }
});
$("cancel-delete").addEventListener("click", () => $("delete-dialog").close());
$("close-link").addEventListener("click", () => $("link-dialog").close());
async function copyDirectoryCommand(commandIds, statusId) {
  const commandText = commandIds.map((id) => $(id).textContent).join("\n");
  const copied = await copyText(commandText);
  const key = copied ? "commandCopied" : "commandCopyManual";
  directoryCommandNotices.set(statusId, key);
  $(statusId).textContent = t(key);
  $(statusId).hidden = false;
  const manual = $("directory-command-manual");
  if (commandIds.length > 1) {
    manual.hidden = copied;
    manual.value = copied ? "" : commandText;
    if (!copied) { manual.focus(); manual.select(); }
    return;
  }
  if (!copied) {
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents($(commandIds[0]));
    selection?.removeAllRanges();
    selection?.addRange(range);
  }
}
$("copy-directory-command").addEventListener("click", () => copyDirectoryCommand(["directory-target-example", "directory-example"], "directory-command-status"));
$("copy-directory-target").addEventListener("click", () => copyDirectoryCommand(["directory-target-example"], "directory-target-status"));
$("copy-directory-upload").addEventListener("click", () => copyDirectoryCommand(["directory-example"], "directory-upload-status"));
$("delete-dialog").addEventListener("cancel", (event) => { if (deleting) event.preventDefault(); });
$("confirm-delete").addEventListener("click", async () => {
  if (!pendingDeletion.length || deleting || uploads.isBusy()) return;
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
