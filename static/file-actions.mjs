export const PAGE_SIZE = 20;
export const MAX_DOWNLOAD_FILES = 100;

export function pageFiles(files, requestedPage) {
  const pages = Math.max(1, Math.ceil(files.length / PAGE_SIZE));
  const page = Math.max(1, Math.min(requestedPage, pages));
  return { page, pages, items: files.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE) };
}

export function reconcileSelection(selected, files) {
  const available = new Set(files.map((file) => file.file_id));
  return new Set([...selected].filter((id) => available.has(id)));
}

export function bulkDownloadUrl(ids) {
  if (!ids.length || ids.length > MAX_DOWNLOAD_FILES) throw new RangeError("Invalid batch size");
  return "/api/files/download?" + new URLSearchParams(ids.map((id) => ["file_id", id]));
}

export async function deleteBatch(files, remove, progress = () => {}, isCurrent = () => true) {
  const result = { deleted: [], missing: [], failed: [], pending: [], authFailed: false, interrupted: false };
  for (let index = 0; index < files.length; index++) {
    if (!isCurrent()) {
      result.interrupted = true;
      result.pending = files.slice(index);
      break;
    }
    const file = files[index];
    try {
      await remove(file);
      result.deleted.push(file);
    } catch (error) {
      if (error.status === 404) result.missing.push(file);
      else if (error.status === 401) {
        result.authFailed = true;
        result.pending = files.slice(index);
        break;
      } else {
        result.failed.push(file);
        if (!error.status) {
          // Stop after a transport error; later files have not been attempted.
          result.pending = files.slice(index + 1);
          break;
        }
      }
    }
    if (isCurrent()) progress(index + 1, files.length);
  }
  return result;
}
