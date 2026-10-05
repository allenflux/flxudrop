import { localFileUrl } from "./file-preview.mjs";

export function directoryBreadcrumbs(name, path) {
  const crumbs = [{ name, path: "" }];
  const parts = path ? path.split("/") : [];
  for (let index = 0; index < parts.length; index++) {
    crumbs.push({ name: parts[index], path: parts.slice(0, index + 1).join("/") });
  }
  return crumbs;
}

export function setupDirectoryBrowser({ t, formatSize, api, onPreview, onAuthRequired }) {
  const $ = (id) => document.getElementById(id);
  const dialog = $("directory-dialog");
  const download = $("directory-download");
  let current = null;
  let path = "";
  let entries = [];
  let revision = 0;
  let loading = false;
  let statusKey = "";

  function render() {
    if (!current) return;
    $("directory-title").textContent = current.filename;
    $("directory-meta").textContent = t("directoryMeta", { count: current.file_count ?? 0, size: formatSize(current.size) });
    $("close-directory").textContent = t("closeDirectory");
    $("directory-back").textContent = t("parentDirectory");
    $("directory-back").disabled = !path;
    $("directory-retry").textContent = t("refresh");
    $("directory-retry").disabled = loading;
    download.textContent = t("downloadDirectory");
    download.setAttribute("aria-label", `${t("downloadDirectory")} · ${current.filename}`);
    const status = $("directory-status");
    status.textContent = statusKey ? t(statusKey) : "";
    status.hidden = !statusKey;
    status.classList.toggle("error", ["directoryError", "directoryMissing"].includes(statusKey));
    const breadcrumbs = $("directory-breadcrumbs");
    breadcrumbs.setAttribute("aria-label", t("directoryPath"));
    breadcrumbs.replaceChildren();
    for (const crumb of directoryBreadcrumbs(current.filename, path)) {
      const button = document.createElement("button");
      button.className = "button breadcrumb";
      button.type = "button";
      button.textContent = crumb.name;
      button.disabled = crumb.path === path;
      if (crumb.path === path) button.setAttribute("aria-current", "location");
      button.addEventListener("click", () => void navigate(crumb.path));
      breadcrumbs.append(button);
    }
    const list = $("directory-entries");
    list.setAttribute("aria-busy", String(loading));
    list.replaceChildren();
    for (const entry of entries) {
      const folder = entry.kind === "directory";
      const row = document.createElement("li");
      row.className = "directory-entry";
      const info = document.createElement("div");
      info.className = "directory-entry-info";
      const title = document.createElement("button");
      title.type = "button";
      title.className = "entry-name";
      title.textContent = entry.filename;
      title.setAttribute("aria-label", t(folder ? "openDirectoryNamed" : "previewFile", { name: entry.filename }));
      title.addEventListener("click", () => folder ? void navigate(entry.path) : onPreview(entry));
      const meta = document.createElement("span");
      meta.className = folder ? "folder-badge" : "field-help";
      meta.textContent = folder ? t("folder") : formatSize(entry.size);
      info.append(title, meta);
      const actions = document.createElement("div");
      actions.className = "file-actions";
      const open = document.createElement("button");
      open.type = "button";
      open.className = "button";
      open.textContent = t(folder ? "openDirectory" : "preview");
      open.setAttribute("aria-label", t(folder ? "openDirectoryNamed" : "previewFile", { name: entry.filename }));
      open.addEventListener("click", () => folder ? void navigate(entry.path) : onPreview(entry));
      actions.append(open);
      if (!folder) {
        const link = document.createElement("a");
        link.className = "button";
        link.textContent = t("download");
        link.setAttribute("aria-label", t("downloadFile", { name: entry.filename }));
        const url = localFileUrl(entry.download_url, "/f/");
        if (url) link.href = url;
        else link.setAttribute("aria-disabled", "true");
        link.download = entry.filename;
        actions.append(link);
      }
      row.append(info, actions);
      list.append(row);
    }
  }

  async function navigate(nextPath) {
    if (!current) return;
    const version = ++revision;
    path = nextPath;
    entries = [];
    loading = true;
    statusKey = "directoryLoading";
    render();
    try {
      const query = new URLSearchParams({ path });
      const data = await api(`/api/directories/${encodeURIComponent(current.file_id)}?${query}`);
      if (version !== revision) return;
      if (!Array.isArray(data.entries)) throw new Error("Invalid directory response");
      entries = data.entries;
      statusKey = entries.length ? "" : "directoryEmpty";
    } catch (error) {
      if (version !== revision) return;
      if (error.status === 401) { close(); onAuthRequired(); return; }
      statusKey = error.status === 404 ? "directoryMissing" : "directoryError";
    } finally {
      if (version === revision) { loading = false; render(); }
    }
  }

  async function open(file) {
    current = { ...file };
    const url = localFileUrl(file.download_url, "/f/");
    download.removeAttribute("href");
    if (url) download.href = url;
    download.setAttribute("aria-disabled", String(!url));
    download.tabIndex = url ? 0 : -1;
    download.download = file.download_filename || `${file.filename}.zip`;
    if (!dialog.open) dialog.showModal();
    await navigate("");
  }

  function cleanup() {
    revision++;
    current = null;
    entries = [];
    path = "";
    loading = false;
    statusKey = "";
    $("directory-entries").replaceChildren();
    $("directory-breadcrumbs").replaceChildren();
    download.removeAttribute("href");
  }

  function close() {
    cleanup();
    if (dialog.open) dialog.close();
  }

  $("close-directory").addEventListener("click", close);
  $("directory-back").addEventListener("click", () => void navigate(path.split("/").slice(0, -1).join("/")));
  $("directory-retry").addEventListener("click", () => void navigate(path));
  dialog.addEventListener("cancel", (event) => { event.preventDefault(); close(); });
  dialog.addEventListener("close", () => { if (!dialog.open) cleanup(); });
  download.addEventListener("click", (event) => { if (!download.getAttribute("href")) event.preventDefault(); });
  return { open, close, render };
}
