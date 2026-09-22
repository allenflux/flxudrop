const PREVIEW_TIMEOUT_MS = 15000;

function localFileUrl(value, prefix) {
  if (typeof value !== "string" || !value.startsWith(prefix) || /[\\\r\n]/.test(value)) return null;
  try {
    const url = new URL(value, location.href);
    if (url.origin !== location.origin || !url.pathname.startsWith(prefix) || url.hash) return null;
    return url.pathname + url.search;
  } catch { return null; }
}

export function setupPreview({ t, formatSize }) {
  const $ = (id) => document.getElementById(id);
  const dialog = $("preview-dialog");
  const content = $("preview-content");
  const status = $("preview-status");
  const download = $("preview-download");
  let current = null;
  let statusKey = "";
  let revision = 0;
  let controller = null;
  let activeTimer = null;
  let media = null;

  function render() {
    if (!current) return;
    $("preview-title").textContent = current.filename;
    $("preview-meta").textContent = formatSize(current.size);
    $("close-preview").textContent = t("closePreview");
    $("close-preview").setAttribute("aria-label", t("closePreview"));
    download.textContent = t("download");
    download.setAttribute("aria-label", t("downloadFile", { name: current.filename }));
    status.textContent = statusKey ? t(statusKey) : "";
    status.hidden = !statusKey;
    status.classList.toggle("error", ["previewError", "previewMissing"].includes(statusKey));
    if (media?.tagName === "IFRAME") media.title = t("previewFile", { name: current.filename });
  }

  function setStatus(key) {
    statusKey = key;
    render();
  }

  function cleanup() {
    revision++;
    controller?.abort();
    controller = null;
    if (activeTimer !== null) clearTimeout(activeTimer);
    activeTimer = null;
    if (media) {
      if (media.tagName === "AUDIO" || media.tagName === "VIDEO") {
        media.pause();
        media.removeAttribute("src");
        media.load();
      } else media.removeAttribute("src");
    }
    media = null;
    content.replaceChildren();
    current = null;
    statusKey = "";
    download.removeAttribute("href");
  }

  function close() {
    cleanup();
    if (dialog.open) dialog.close();
  }

  async function open(file) {
    cleanup();
    const version = revision;
    current = { ...file };
    const downloadUrl = localFileUrl(current.download_url, "/f/");
    download.setAttribute("aria-disabled", String(!downloadUrl));
    download.tabIndex = downloadUrl ? 0 : -1;
    if (downloadUrl) download.href = downloadUrl;
    download.download = current.filename;
    setStatus("previewLoading");
    if (!dialog.open) dialog.showModal();

    const kind = current.preview_type;
    if (!["text", "image", "pdf", "audio", "video"].includes(kind)) {
      setStatus("previewUnsupported");
      return;
    }
    const previewUrl = localFileUrl(current.preview_url, "/p/");
    if (!previewUrl) {
      setStatus("previewError");
      return;
    }

    const requestController = new AbortController();
    controller = requestController;
    const timer = setTimeout(() => requestController.abort(), PREVIEW_TIMEOUT_MS);
    activeTimer = timer;
    try {
      // HEAD catches deleted media before handing the URL to a browser viewer.
      const response = await fetch(previewUrl, {
        method: kind === "text" ? "GET" : "HEAD",
        cache: "no-store",
        signal: requestController.signal,
      });
      if (version !== revision) return;
      if (!response.ok) {
        setStatus(response.status === 404 ? "previewMissing" : response.status === 415 ? "previewUnsupported" : "previewError");
        return;
      }
      if (kind === "text") {
        const text = await response.text();
        if (version !== revision) return;
        const pre = document.createElement("pre");
        pre.className = "preview-text";
        pre.tabIndex = 0;
        // HTML, SVG and Markdown uploads are deliberately rendered as plain text.
        pre.textContent = text;
        content.replaceChildren(pre);
        setStatus(response.headers.get("X-Preview-Truncated") === "true" ? "previewTruncated" : text ? "" : "previewEmpty");
        return;
      }

      media = document.createElement(kind === "image" ? "img" : kind === "pdf" ? "iframe" : kind);
      const element = media;
      element.className = kind === "image" ? "preview-image" : kind === "pdf" ? "preview-pdf" : "preview-media";
      element.addEventListener("error", () => {
        if (version === revision) setStatus("previewError");
      });
      element.addEventListener(kind === "audio" || kind === "video" ? "loadedmetadata" : "load", () => {
        if (version === revision) setStatus("");
      });
      if (kind === "image") element.alt = current.filename;
      else if (kind === "pdf") {
        // Sandboxed frames block native PDF viewers. The preview endpoint
        // supplies a fixed PDF MIME type, nosniff and a content policy.
        element.setAttribute("referrerpolicy", "no-referrer");
        element.title = t("previewFile", { name: current.filename });
      } else {
        element.controls = true;
        element.preload = "metadata";
        if (kind === "video") element.playsInline = true;
      }
      element.src = previewUrl;
      content.replaceChildren(element);
    } catch {
      if (version === revision) setStatus("previewError");
    } finally {
      clearTimeout(timer);
      if (activeTimer === timer) activeTimer = null;
      if (controller === requestController) controller = null;
    }
  }

  $("close-preview").addEventListener("click", close);
  dialog.addEventListener("cancel", (event) => { event.preventDefault(); close(); });
  dialog.addEventListener("close", () => { if (!dialog.open) cleanup(); });
  download.addEventListener("click", (event) => {
    if (!download.getAttribute("href")) event.preventDefault();
  });
  return { open, close, render };
}
