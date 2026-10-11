import assert from "node:assert/strict";
import test from "node:test";
import { createStorageController, restoreBatch, restorePath, activeStorageJob, storageProgress } from "../static/file-storage.mjs";

const cold = { file_id: "file-id", storage_state: "cold", download_url: "/f/file-id/data.bin", preview_url: "/p/file-id/data.bin" };
const status = (storage_state, state = "running", progress = 0, extra = {}) => ({
  ok: true, storage_state,
  storage_job: { job_id: "job-id", kind: "restore", state, progress, error: null, cancellable: true, ...extra },
});
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { resolve, promise };
};
const tick = () => new Promise((resolve) => setImmediate(resolve));

test("older file responses do not invoke storage APIs, while hot content clicks do", async () => {
  const calls = [];
  const controller = createStorageController({ request: async (...args) => { calls.push(args); return status("hot_backed", "completed", 100); } });
  const legacy = { file_id: "legacy" };
  assert.equal(await controller.ensureHot(legacy), legacy);
  assert.equal(calls.length, 0);
  await controller.ensureHot({ ...cold, storage_state: "hot" });
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "/api/files/file-id/restore");
  assert.equal(calls[0][1], "POST");
});

test("restoration posts once and polls until the complete hot file is published", async () => {
  const snapshots = [status("restoring", "queued"), status("restoring", "running", 50), status("hot_backed", "completed", 100)];
  const calls = [];
  const updates = [];
  const progress = [];
  const controller = createStorageController({
    request: async (path, method) => { calls.push([path, method]); return snapshots.shift(); },
    onUpdate: (id, snapshot) => updates.push([id, snapshot.storage_state]), delay: async () => {},
  });
  const result = await controller.ensureHot(cold, { onProgress: (snapshot) => progress.push(snapshot.storage_job.progress) });
  assert.equal(result.storage_state, "hot_backed");
  assert.deepEqual(calls, [["/api/files/file-id/restore", "POST"], ["/api/files/file-id/storage", "GET"], ["/api/files/file-id/storage", "GET"]]);
  assert.deepEqual(progress, [0, 50, 100]);
  assert.deepEqual(updates.map(([id]) => id), ["file-id", "file-id", "file-id"]);
});

test("same-content waiters share a restore request and closing one leaves the other active", async () => {
  const pending = deferred();
  const calls = [];
  const controller = createStorageController({ request: async (path, method, options) => {
    calls.push({ path, method, signal: options.signal });
    return pending.promise;
  } });
  const firstController = new AbortController();
  const first = controller.ensureHot(cold, { signal: firstController.signal });
  const second = controller.ensureHot(cold);
  await tick();
  assert.equal(calls.length, 1);
  firstController.abort();
  await assert.rejects(first, { name: "AbortError" });
  assert.equal(calls[0].signal.aborted, false);
  pending.resolve(status("hot", "completed", 100));
  assert.equal((await second).storage_state, "hot");
});

test("closing the last waiter aborts polling but never sends a server task cancellation", async () => {
  const waiting = deferred();
  const calls = [];
  const controller = createStorageController({ request: async (path, method, options) => {
    calls.push({ path, method, signal: options.signal });
    return waiting.promise;
  } });
  const waiter = new AbortController();
  const restoring = controller.ensureHot(cold, { signal: waiter.signal });
  await tick();
  waiter.abort();
  await assert.rejects(restoring, { name: "AbortError" });
  assert.equal(calls[0].signal.aborted, true);
  waiting.resolve(status("hot", "completed"));
  await tick();
  assert.equal(calls.length, 1);
  assert.ok(!calls.some((call) => call.path.includes("/cancel")));
});

test("failed restoration stays cold, surfaces its error, and can be retried", async () => {
  let attempts = 0;
  const controller = createStorageController({ request: async () => {
    attempts++;
    return attempts === 1 ? status("cold", "failed", 20, { error: "S3 is unavailable" }) : status("hot_backed", "completed", 100);
  } });
  await assert.rejects(controller.ensureHot(cold), /S3 is unavailable/);
  assert.equal(cold.storage_state, "cold");
  assert.equal((await controller.ensureHot(cold)).storage_state, "hot_backed");
  assert.equal(attempts, 2);
});

test("failed storage jobs preserve the core's capacity error code for batching guidance", async () => {
  const controller = createStorageController({ request: async () => status("cold", "failed", 0, { error: "No space", error_code: "storage_capacity" }) });
  await assert.rejects(controller.ensureHot(cold), (error) => error.code === "storage_capacity" && error.status === 507);
});

test("directory child validation uses its parent's ID and original path before restoring", () => {
  const child = { ...cold, file_id: undefined, storage_file_id: "parent-id", preview_url: "/p/parent-id/a.txt?path=nested%2Fa.txt", download_url: "/f/parent-id/a.txt?path=nested%2Fa.txt" };
  assert.equal(restorePath(child, true), "/api/files/parent-id/restore?path=nested%2Fa.txt&preview=1");
  assert.equal(restorePath(child), "/api/files/parent-id/restore?path=nested%2Fa.txt");
  assert.equal(restorePath(cold, true), "/api/files/file-id/restore?preview=1");
});

test("batch restores run sequentially and stop before requesting a ZIP on capacity failure", async () => {
  const files = [cold, { ...cold, file_id: "two" }, { ...cold, file_id: "three" }];
  const calls = [];
  let active = 0;
  let maxActive = 0;
  await assert.rejects(restoreBatch(files, async (file) => {
    calls.push(file.file_id);
    active++;
    maxActive = Math.max(maxActive, active);
    await tick();
    active--;
    if (file.file_id === "two") throw Object.assign(new Error("No space"), { status: 507 });
  }), (error) => error.status === 507 && error.completed === 1 && error.file.file_id === "two");
  assert.equal(maxActive, 1);
  assert.deepEqual(calls, ["file-id", "two"]);
});

test("manual archive and cancellation use the management APIs and refresh status", async () => {
  const calls = [];
  const controller = createStorageController({ request: async (path, method) => {
    calls.push([path, method]);
    return { ...status("hot", "cancelled"), storage_job: { job_id: "archive-id", kind: "archive", state: "queued", cancellable: true } };
  } });
  const requested = await controller.setCold(cold);
  await controller.cancel({ ...cold, storage_job: requested.storage_job });
  assert.deepEqual(calls, [["/api/files/file-id/cold", "POST"], ["/api/storage-jobs/archive-id/cancel", "POST"], ["/api/files/file-id/storage", "GET"]]);
  await assert.rejects(controller.cancel({ ...cold, storage_job: { job_id: "done", cancellable: false } }), (error) => error.status === 409);
});

test("progress is bounded and only queued/running/retry jobs are active", () => {
  assert.equal(storageProgress({ progress: 120 }), 100);
  assert.equal(storageProgress({ progress: -5 }), 0);
  assert.equal(storageProgress({ progress: 32.6 }), 33);
  assert.equal(storageProgress({ progress: NaN }), null);
  for (const state of ["queued", "running", "retry"]) assert.equal(activeStorageJob({ storage_job: { state } }), true);
  for (const state of ["completed", "failed", "cancelled"]) assert.equal(activeStorageJob({ storage_job: { state } }), false);
});
