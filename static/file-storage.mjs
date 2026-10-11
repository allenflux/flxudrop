const ACTIVE_JOB_STATES = new Set(["queued", "running", "retry"]);
const HOT_STATES = new Set(["hot", "hot_backed"]);

export const hasStorage = (file) => typeof file?.storage_state === "string";
export const storageFileId = (file) => file.storage_file_id || file.file_id;
export const isHot = (file) => HOT_STATES.has(file?.storage_state || "hot");
export const activeStorageJob = (file) => ACTIVE_JOB_STATES.has(file?.storage_job?.state);

export function storageProgress(job) {
  return typeof job?.progress === "number" && Number.isFinite(job.progress)
    ? Math.round(Math.max(0, Math.min(100, job.progress))) : null;
}

// Status reads are independent: one slow response must not hold every later row.
// Keep a small pool so a large archive queue does not flood a small VPS.
export async function pollStorageFiles(files, read, { concurrency = 4, isCurrent = () => true, onError = () => {} } = {}) {
  let next = 0;
  const count = Math.min(files.length, Math.max(1, Math.trunc(concurrency) || 1));
  async function worker() {
    while (isCurrent() && next < files.length) {
      const file = files[next++];
      try { await read(file); }
      catch (error) { if (isCurrent()) onError(file, error); }
    }
  }
  await Promise.all(Array.from({ length: count }, worker));
}

export function restorePath(file, preview = false) {
  const id = storageFileId(file);
  if (!id) throw new Error("Missing file ID");
  const query = new URLSearchParams();
  const contentUrl = preview ? file.preview_url : file.download_url;
  if (contentUrl) {
    const path = new URL(contentUrl, "http://localhost").searchParams.get("path");
    if (path) query.set("path", path);
  }
  if (preview) query.set("preview", "1");
  return `/api/files/${encodeURIComponent(id)}/restore${query.size ? `?${query}` : ""}`;
}

function aborted() { return new DOMException("Aborted", "AbortError"); }

export function storageDelay(milliseconds, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) { reject(aborted()); return; }
    const timer = setTimeout(done, milliseconds);
    function done() { signal?.removeEventListener("abort", cancel); resolve(); }
    function cancel() { clearTimeout(timer); signal?.removeEventListener("abort", cancel); reject(aborted()); }
    signal?.addEventListener("abort", cancel, { once: true });
  });
}

function restoreError(snapshot) {
  const error = new Error(snapshot.storage_job?.error || "File restoration failed");
  error.code = snapshot.error_code || snapshot.storage_job?.error_code;
  if (["storage_capacity", "insufficient_storage", "disk_full", "capacity"].includes(error.code)) error.status = 507;
  return error;
}

// A waiter owns its polling subscription, not the durable server task. Closing a
// preview stops its requests without cancelling another reader's restoration.
export function createStorageController({ request, onUpdate = () => {}, delay = storageDelay, pollInterval = 1200, maxWait = 600000 }) {
  const pending = new Map();
  const snapshots = new Map();

  async function snapshotRequest(id, path, method, options = {}, publish = true) {
    let order = snapshots.get(id);
    if (!order) {
      order = { epoch: 0, sequence: 0, published: 0, mutations: 0 };
      snapshots.set(id, order);
    }
    const mutation = method !== "GET";
    const epoch = mutation ? ++order.epoch : order.epoch;
    const sequence = ++order.sequence;
    // A read admitted before a write, or while that write is still pending,
    // cannot describe the acknowledged result of the write.
    const readable = order.mutations === 0;
    if (mutation) order.mutations++;
    let snapshot;
    try { snapshot = await request(path, method, options); }
    finally { if (mutation) order.mutations--; }
    if (options.signal?.aborted) throw aborted();
    const previous = order.snapshot?.storage_job;
    const job = snapshot.storage_job;
    const terminalRollback = !mutation && order.acceptedEpoch === epoch && previous?.job_id && previous.job_id === job?.job_id
      && ["completed", "cancelled", "failed"].includes(previous.state) && ACTIVE_JOB_STATES.has(job.state);
    if (publish && !terminalRollback && epoch === order.epoch && (mutation || (readable && sequence >= order.published))) {
      order.published = sequence;
      order.acceptedEpoch = epoch;
      order.snapshot = snapshot;
      onUpdate(id, snapshot);
    }
    return snapshot;
  }

  async function read(file, options = {}) {
    const id = storageFileId(file);
    return snapshotRequest(id, `/api/files/${encodeURIComponent(id)}/storage`, "GET", options);
  }

  async function setCold(file) {
    const id = storageFileId(file);
    return snapshotRequest(id, `/api/files/${encodeURIComponent(id)}/cold`, "POST");
  }

  async function cancel(file) {
    const job = file.storage_job;
    if (!job?.job_id || !job.cancellable) throw Object.assign(new Error("This task can no longer be cancelled"), { status: 409 });
    await snapshotRequest(storageFileId(file), `/api/storage-jobs/${encodeURIComponent(job.job_id)}/cancel`, "POST", {}, false);
    return read(file);
  }

  function ensureHot(file, { signal, onProgress = () => {}, preview = false } = {}) {
    if (signal?.aborted) return Promise.reject(aborted());
    // Responses from an older server have no storage fields or restore API.
    if (!hasStorage(file)) return Promise.resolve(file);
    const path = restorePath(file, preview);
    let run = pending.get(path);
    if (!run || run.controller.signal.aborted) {
      run = { controller: new AbortController(), listeners: new Set(), latest: null };
      pending.set(path, run);
      const task = run;
      task.promise = Promise.resolve().then(async () => {
        const started = Date.now();
        let snapshot = await snapshotRequest(storageFileId(file), path, "POST", { signal: task.controller.signal });
        for (;;) {
          if (task.controller.signal.aborted) throw aborted();
          if (!hasStorage(snapshot)) throw new Error("Invalid storage status response");
          task.latest = snapshot;
          for (const listener of task.listeners) listener.progress(snapshot);
          if (isHot(snapshot)) return snapshot;
          if (["failed", "cancelled"].includes(snapshot.storage_job?.state)) throw restoreError(snapshot);
          if (Date.now() - started >= maxWait) throw Object.assign(new Error("Restoration is still pending. Please retry later."), { code: "restore_timeout" });
          await delay(pollInterval, task.controller.signal);
          snapshot = await snapshotRequest(storageFileId(file), `/api/files/${encodeURIComponent(storageFileId(file))}/storage`, "GET", { signal: task.controller.signal });
        }
      }).finally(() => { if (pending.get(path) === task) pending.delete(path); });
    }
    return new Promise((resolve, reject) => {
      const listener = { progress: onProgress };
      run.listeners.add(listener);
      function release() {
        signal?.removeEventListener("abort", cancelWait);
        run.listeners.delete(listener);
      }
      function cancelWait() {
        release();
        if (!run.listeners.size) run.controller.abort();
        reject(aborted());
      }
      signal?.addEventListener("abort", cancelWait, { once: true });
      if (signal?.aborted) cancelWait();
      else if (run.latest) onProgress(run.latest);
      run.promise.then((snapshot) => { release(); resolve(snapshot); }, (error) => { release(); reject(error); });
    });
  }

  function abortAll() { for (const run of pending.values()) run.controller.abort(); pending.clear(); }
  return { read, setCold, cancel, ensureHot, abortAll };
}

export async function restoreBatch(files, ensureHot, progress = () => {}, signal) {
  for (let index = 0; index < files.length; index++) {
    if (signal?.aborted) throw aborted();
    progress(index, files.length, files[index]);
    try { await ensureHot(files[index], { signal }); }
    catch (error) { error.file = files[index]; error.completed = index; throw error; }
  }
  progress(files.length, files.length, null);
}
