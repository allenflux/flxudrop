export function textUpload(content, filename) {
  let name = filename.trim() || "note.txt";
  if (!/\.[^./\\]+$/.test(name)) name += ".txt";
  return { name, blob: new Blob([content], { type: "text/plain;charset=utf-8" }) };
}

export function hasFileTransfer(transfer) {
  return Boolean(transfer && (
    Array.from(transfer.types || []).includes("Files") ||
    Array.from(transfer.items || []).some((item) => item.kind === "file")
  ));
}

export const MAX_DIRECTORY_ENTRIES = 10000;

function checkedDirectoryPath(path) {
  if (typeof path !== "string" || /[\\\0\r\n]/.test(path)) throw uploadError("uploadDirectoryReadError");
  const parts = path.replace(/\/$/, "").split("/");
  if (parts.some((part) => !part || part === "." || part === "..")) throw uploadError("uploadDirectoryReadError");
  return parts;
}

function appendDirectoryEntry(directory, path, blob) {
  checkedDirectoryPath(path);
  if (directory.entries.length >= MAX_DIRECTORY_ENTRIES) throw uploadError("uploadDirectoryLimit");
  directory.entries.push({ path, blob });
}

export function selectedDirectories(files) {
  const directories = new Map();
  for (const file of files) {
    const path = file.webkitRelativePath;
    const parts = checkedDirectoryPath(path);
    if (parts.length < 2) throw uploadError("uploadDirectoryReadError");
    const name = parts[0];
    if (!directories.has(name)) directories.set(name, { name, kind: "directory", entries: [] });
    appendDirectoryEntry(directories.get(name), path, file);
  }
  return [...directories.values()];
}

async function readEntry(entry, path, directory) {
  if (entry.isFile) {
    const blob = await new Promise((resolve, reject) => entry.file(resolve, reject));
    appendDirectoryEntry(directory, path, blob);
    return;
  }
  if (!entry.isDirectory || !entry.createReader) throw uploadError("uploadDirectoryReadError");
  appendDirectoryEntry(directory, `${path}/`, new Blob([]));
  const reader = entry.createReader();
  // Chrome returns directory entries in batches; drain the reader completely.
  while (true) {
    const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
    if (!batch.length) break;
    for (const child of batch) await readEntry(child, `${path}/${child.name}`, directory);
  }
}

async function readHandle(handle, path, directory) {
  if (handle.kind === "file") {
    appendDirectoryEntry(directory, path, await handle.getFile());
    return;
  }
  if (handle.kind !== "directory" || !handle.values) throw uploadError("uploadDirectoryReadError");
  appendDirectoryEntry(directory, `${path}/`, new Blob([]));
  for await (const child of handle.values()) await readHandle(child, `${path}/${child.name}`, directory);
}

export async function droppedFiles(transfer) {
  const items = Array.from(transfer.items || []).filter((item) => item.kind === "file");
  if (!items.length) return { files: Array.from(transfer.files || []), directories: [] };
  // Capture entries and handle promises before the drop event releases access.
  const captured = items.map((item) => {
    const file = item.getAsFile();
    let entry;
    try { entry = item.webkitGetAsEntry?.(); } catch { /* Try the File System Access API. */ }
    let handle;
    try { handle = !entry && item.getAsFileSystemHandle?.(); } catch { /* Fall back to File. */ }
    return { file, entry, handle };
  });
  const files = [];
  const directories = [];
  for (const item of captured) {
    let handle;
    try { handle = await item.handle; } catch { /* Fall back to the captured File. */ }
    if (item.entry?.isDirectory || handle?.kind === "directory") {
      const source = item.entry || handle;
      const directory = { name: source.name, kind: "directory", entries: [] };
      checkedDirectoryPath(source.name);
      if (item.entry) await readEntry(item.entry, source.name, directory);
      else await readHandle(handle, source.name, directory);
      directories.push(directory);
    } else if (item.file) files.push(item.file);
  }
  return { files, directories };
}

export async function copyText(text, input = null) {
  try {
    if (globalThis.navigator?.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch { /* Clipboard API is unavailable on ordinary HTTP or permission was denied. */ }
  if (!globalThis.document) return false;
  const previousFocus = document.activeElement;
  const field = input || document.createElement("input");
  if (!input) {
    field.type = "text";
    field.readOnly = true;
    field.style.position = "fixed";
    field.style.opacity = "0";
    document.body.append(field);
  }
  field.value = text;
  let copied = false;
  try {
    field.focus();
    field.select();
    field.setSelectionRange(0, text.length);
    copied = Boolean(document.execCommand?.("copy"));
  } catch { /* Keep a visible field selected so the user can copy manually. */ }
  finally {
    if (!input) field.remove();
    if (!input || copied) previousFocus?.focus?.({ preventScroll: true });
  }
  return copied;
}

function uploadError(key, status = 0, detail = "") {
  return Object.assign(new Error(detail || key), { key, status, detail });
}

export function sendUpload({ blob, name, kind, entries, token = "", onProgress = () => {} }) {
  const xhr = new XMLHttpRequest();
  const promise = new Promise((resolve, reject) => {
    const directory = kind === "directory";
    let body = blob;
    if (directory) {
      if (!entries?.length || entries.length > MAX_DIRECTORY_ENTRIES) {
        reject(uploadError("uploadDirectoryLimit"));
        return;
      }
      body = new FormData();
      for (const entry of entries) body.append("file", entry.blob, entry.path);
    }
    xhr.open(directory ? "POST" : "PUT", directory ? "/upload" : `/upload/${encodeURIComponent(name)}`);
    if (token) xhr.setRequestHeader("X-Upload-Token", token);
    if (directory) xhr.setRequestHeader("X-FluxDrop-Directory", "files");
    if (!directory) xhr.setRequestHeader("Content-Type", blob.type || "application/octet-stream");
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.total > 0 ? Math.min(100, Math.round(event.loaded / event.total * 100)) : 100);
    };
    xhr.onload = () => {
      let data;
      try { data = JSON.parse(xhr.responseText); } catch { /* Report the HTTP status even without JSON. */ }
      if (xhr.status < 200 || xhr.status >= 300 || data?.ok === false) {
        reject(uploadError("uploadFailed", xhr.status, typeof data?.error === "string" ? data.error : `HTTP ${xhr.status}`));
      } else if (data?.ok !== true || typeof data.filename !== "string" || typeof data.download_url !== "string" || !data.download_url) {
        reject(uploadError("uploadInvalidResponse", xhr.status));
      } else resolve(data);
    };
    xhr.onerror = () => reject(uploadError("uploadNetworkError"));
    xhr.ontimeout = () => reject(uploadError("uploadNetworkError"));
    xhr.onabort = () => reject(uploadError("uploadNetworkError"));
    xhr.send(body);
  });
  return { promise, abort: () => xhr.abort() };
}

export function setupUploads({ t, getToken, canUpload, onAuthRequired, onUploaded, formatSize, onBusyChange = () => {} }) {
  const $ = (id) => document.getElementById(id);
  const zone = $("drop-zone");
  const input = $("file-input");
  const directoryInput = $("directory-input");
  const progress = $("upload-progress");
  const status = $("upload-status");
  const content = $("text-content");
  const filename = $("text-filename");
  const resultRows = [];
  let busy = false;
  let active = null;
  let revision = 0;
  let dragDepth = 0;
  let notice = null;

  function showNotice(key, values = {}, error = false) {
    notice = { key, values, error };
    renderNotice();
  }

  function renderNotice() {
    const current = notice || { key: canUpload() ? "uploadReady" : "uploadLocked" };
    status.textContent = t(current.key, current.values);
    status.classList.toggle("error", Boolean(current.error));
  }

  function render() {
    const disabled = busy || !canUpload();
    zone.setAttribute("aria-disabled", String(disabled));
    zone.setAttribute("aria-busy", String(busy));
    input.disabled = disabled;
    directoryInput.disabled = disabled;
    $("choose-files").disabled = disabled;
    $("choose-directory").disabled = disabled;
    $("save-text").disabled = disabled;
    $("save-text").textContent = t(busy ? "uploadSaving" : "saveText");
    $("text-size").textContent = t("uploadTextSize", { size: formatSize(new Blob([content.value]).size) });
    renderNotice();
    for (const row of resultRows) {
      row.copy.textContent = t("uploadCopy");
      row.link.setAttribute("aria-label", t("uploadLinkLabel", { name: row.name }));
      row.download.textContent = t(row.directory ? "downloadZip" : "download");
      row.download.setAttribute("aria-label", t("downloadFile", { name: row.name }));
    }
  }

  function appendResult(data) {
    const url = new URL(data.download_url, location.href);
    if (!["http:", "https:"].includes(url.protocol)) throw uploadError("uploadInvalidResponse");
    const row = document.createElement("li");
    row.className = "upload-result";
    const title = document.createElement("strong");
    title.className = "upload-result-name";
    title.textContent = data.filename;
    const controls = document.createElement("div");
    controls.className = "upload-result-controls";
    const link = document.createElement("input");
    link.className = "upload-result-link";
    link.type = "text";
    link.readOnly = true;
    link.value = url.href;
    link.addEventListener("click", () => link.select());
    const copy = document.createElement("button");
    copy.className = "button";
    copy.type = "button";
    const current = revision;
    copy.addEventListener("click", async () => {
      const copied = await copyText(url.href, link);
      if (current !== revision) return;
      showNotice(copied ? "uploadCopied" : "uploadCopyManual");
    });
    const download = document.createElement("a");
    download.className = "button";
    download.href = url.href;
    download.download = data.download_filename || data.filename;
    controls.append(link, copy, download);
    row.append(title, controls);
    $("upload-results").append(row);
    resultRows.push({ name: data.filename, directory: data.kind === "directory", link, copy, download });
  }

  async function uploadBatch(uploads, { onSaved = () => {} } = {}) {
    if (busy) return;
    if (!canUpload()) { showNotice("uploadLocked"); return; }
    if (!uploads.length) return;
    const current = revision;
    busy = true;
    progress.hidden = false;
    progress.max = 100;
    progress.value = 0;
    let saved = 0;
    const failures = [];
    render();
    onBusyChange(true);
    try {
      for (let index = 0; index < uploads.length; index++) {
        if (current !== revision) return;
        const upload = uploads[index];
        const updateProgress = (percent) => {
          if (current !== revision) return;
          progress.value = (index + percent / 100) / uploads.length * 100;
          showNotice("uploadProgress", { index: index + 1, total: uploads.length, name: upload.name, percent });
        };
        updateProgress(0);
        try {
          active = sendUpload({ ...upload, token: getToken(), onProgress: updateProgress });
          const data = await active.promise;
          if (current !== revision) return;
          appendResult(data);
          saved++;
          onSaved(upload, data);
          render();
        } catch (error) {
          if (current !== revision) return;
          if (error.status === 401) {
            reset();
            onAuthRequired();
            return;
          }
          failures.push(`${upload.name}: ${error.detail || t(error.key || "uploadNetworkError")}`);
        } finally {
          if (current === revision) active = null;
        }
      }
      progress.value = 100;
      if (failures.length) {
        showNotice("uploadPartial", { saved, total: uploads.length, failed: failures.length, detail: failures.join("; ") }, true);
      } else showNotice("uploadComplete", { count: saved });
    } finally {
      if (current === revision) {
        busy = false;
        progress.hidden = true;
        render();
        onBusyChange(false);
        if (saved) onUploaded();
      }
    }
  }

  async function prepareAndUpload(collect) {
    if (busy || !canUpload()) return;
    const current = revision;
    busy = true;
    showNotice("uploadReadingDirectory");
    render();
    onBusyChange(true);
    let uploads;
    try {
      uploads = await collect();
    } catch (error) {
      if (current === revision) showNotice(error.key || "uploadDirectoryReadError", {}, true);
    } finally {
      if (current === revision) {
        busy = false;
        render();
        onBusyChange(false);
      }
    }
    if (current === revision && uploads) void uploadBatch(uploads);
  }

  function chooseFiles() {
    if (!busy && canUpload()) input.click();
  }
  $("choose-files").addEventListener("click", (event) => { event.stopPropagation(); chooseFiles(); });
  $("choose-directory").addEventListener("click", () => {
    if (!busy && canUpload()) directoryInput.click();
  });
  directoryInput.addEventListener("change", () => {
    const files = Array.from(directoryInput.files || []);
    directoryInput.value = "";
    if (files.length) void prepareAndUpload(() => selectedDirectories(files));
    else showNotice("uploadEmptyDirectoryPicker", {}, true);
  });
  zone.addEventListener("click", (event) => { if (event.target !== input) chooseFiles(); });
  zone.addEventListener("keydown", (event) => {
    if (event.target === zone && ["Enter", " "].includes(event.key)) {
      event.preventDefault();
      chooseFiles();
    }
  });
  input.addEventListener("change", () => {
    const files = Array.from(input.files || []);
    input.value = "";
    void uploadBatch(files.map((file) => ({ name: file.name, blob: file })));
  });
  document.addEventListener("dragenter", (event) => {
    if (!hasFileTransfer(event.dataTransfer)) return;
    dragDepth++;
    if (canUpload() && !busy) zone.classList.add("is-dragging");
  });
  document.addEventListener("dragleave", (event) => {
    if (!hasFileTransfer(event.dataTransfer)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) zone.classList.remove("is-dragging");
  });
  document.addEventListener("dragover", (event) => {
    if (!hasFileTransfer(event.dataTransfer)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = busy || !canUpload() ? "none" : "copy";
  });
  document.addEventListener("drop", async (event) => {
    if (!hasFileTransfer(event.dataTransfer)) return;
    event.preventDefault();
    dragDepth = 0;
    zone.classList.remove("is-dragging");
    if (busy) { showNotice("uploadBusy"); return; }
    if (!canUpload()) { showNotice("uploadLocked"); return; }
    await prepareAndUpload(async () => {
      const { files, directories } = await droppedFiles(event.dataTransfer);
      return [...files.map((file) => ({ name: file.name, blob: file })), ...directories];
    });
  });
  content.addEventListener("input", render);
  $("text-form").addEventListener("submit", (event) => {
    event.preventDefault();
    if (busy) return;
    if (!content.value.length) { showNotice("uploadEmptyText", {}, true); content.focus(); return; }
    const draft = content.value;
    const draftName = filename.value;
    void uploadBatch([textUpload(draft, draftName)], {
      onSaved: () => {
        if (content.value === draft && filename.value === draftName) content.value = "";
      },
    });
  });

  function reset() {
    const wasBusy = busy;
    revision++;
    active?.abort();
    active = null;
    busy = false;
    notice = null;
    dragDepth = 0;
    input.value = "";
    directoryInput.value = "";
    progress.hidden = true;
    zone.classList.remove("is-dragging");
    resultRows.length = 0;
    $("upload-results").replaceChildren();
    render();
    if (wasBusy) onBusyChange(false);
  }
  render();
  return { render, reset, isBusy: () => busy };
}
