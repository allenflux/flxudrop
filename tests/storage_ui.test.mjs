import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";

class Element {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.listeners = new Map();
    this.attributes = new Map();
    this.value = "";
    this.dataset = {};
    this.style = {};
    this.classes = new Set();
    this.open = false;
    this.classList = {
      add: (name) => this.classes.add(name), remove: (name) => this.classes.delete(name),
      toggle: (name, enabled) => enabled ? this.classes.add(name) : this.classes.delete(name),
    };
  }
  set textContent(value) { this.text = value; this.children = []; }
  get textContent() { return this.text ?? this.children.map((child) => child.textContent || "").join(""); }
  set href(value) { this.setAttribute("href", value); }
  get href() { return this.getAttribute("href"); }
  set src(value) { this.setAttribute("src", value); }
  get src() { return this.getAttribute("src"); }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  removeAttribute(name) { this.attributes.delete(name); }
  append(...children) { for (const child of children) this.children.push(...(child.fragment ? child.children : [child])); }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  querySelectorAll() { return []; }
  addEventListener(name, callback) {
    if (!this.listeners.has(name)) this.listeners.set(name, []);
    this.listeners.get(name).push(callback);
  }
  async dispatch(name, values = {}) {
    const event = { target: this, preventDefault() { this.defaultPrevented = true; }, stopPropagation() {}, ...values };
    for (const listener of this.listeners.get(name) || []) await listener(event);
    return event;
  }
  focus() {}
  select() {}
  showModal() { this.open = true; }
  close() { if (this.open) { this.open = false; void this.dispatch("close"); } }
}

let fixtureIndex = 0;
const tick = () => new Promise((resolve) => setImmediate(resolve));
const file = (storage_state = "hot", file_id = "file-id") => ({
  file_id, filename: `${file_id}.txt`, size: 12, created_at: 1700000000,
  download_url: `/f/${file_id}/${file_id}.txt`, preview_url: `/p/${file_id}/${file_id}.txt`, preview_type: "text",
  ...(storage_state ? { storage_state, storage_job: null } : {}),
});

async function harness(context, initial, responder = () => ({ ok: true }), { manualPoll = false } = {}) {
  const nodes = new Map();
  const $ = (id) => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const document = new Element();
  document.documentElement = new Element("html");
  document.getElementById = $;
  document.createElement = (tag) => new Element(tag);
  document.createDocumentFragment = () => Object.assign(new Element(), { fragment: true });
  const requests = [];
  const pollTimers = [];
  const completionTimers = [];
  const originalSetTimeout = globalThis.setTimeout;
  const originalClearTimeout = globalThis.clearTimeout;
  const globals = {
    document, navigator: { language: "zh-CN" }, localStorage: { getItem() { return null; }, setItem() {} },
    location: { href: "http://localhost/", origin: "http://localhost" },
    fetch: async (path, options) => {
      requests.push({ path, ...options });
      const data = path === "/api/files" ? initial : await responder(path, options);
      const status = data.httpStatus || 200;
      return { ok: status < 400, status, json: async () => data };
    },
    ...(manualPoll ? {
      setTimeout: (callback, milliseconds, ...args) => {
        if (![1500, 800].includes(milliseconds)) return originalSetTimeout(callback, milliseconds, ...args);
        const timer = { storagePoll: true, callback, cancelled: false };
        (milliseconds === 1500 ? pollTimers : completionTimers).push(timer);
        return timer;
      },
      clearTimeout: (timer) => timer?.storagePoll ? timer.cancelled = true : originalClearTimeout(timer),
    } : {}),
  };
  const previous = new Map(Object.keys(globals).map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  for (const [key, value] of Object.entries(globals)) Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  context.after(async () => {
    await $("logout").dispatch("click");
    for (const [key, descriptor] of previous) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  });
  let source = await readFile(new URL("../static/app.js", import.meta.url), "utf8");
  source = source.replace(/from "\/static\/([^"]+)"/g, (_match, name) => `from ${JSON.stringify(new URL(`../static/${name}`, import.meta.url).href)}`);
  source += `\n// isolated UI fixture ${fixtureIndex++}\n`;
  await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
  await tick();
  return { $, requests, rows: () => $("file-list").children, poll: async () => {
    const timer = pollTimers.shift();
    assert.ok(timer && !timer.cancelled, "an active storage poll is scheduled");
    await timer.callback();
  }, pendingPolls: () => pollTimers.filter((timer) => !timer.cancelled).length,
  completionTimers, finishAnimations: () => {
    for (const timer of completionTimers.splice(0)) if (!timer.cancelled) timer.callback();
  } };
}

test("cold files remain listed and hot rows expose a visible manual cold action", async (context) => {
  const ui = await harness(context, { ok: true, storage_enabled: true, files: [file("hot"), file("cold", "cold-id")] });
  assert.equal(ui.rows().length, 2);
  const hot = ui.rows()[0];
  const cold = ui.rows()[1];
  assert.equal(hot.children[3].children[0].children[0].textContent, "热文件");
  assert.equal(cold.children[3].children[0].children[0].textContent, "冷文件");
  assert.equal(hot.children[3].children[0].children[1].textContent, "设为冷文件");
  assert.equal(hot.children[3].children[0].children[1].disabled, false);
  assert.equal(hot.children[3].children[0].children[1].hidden, false);
  assert.match(hot.children[3].children[0].children[1].innerHTML, /<svg.*aria-hidden="true"/);
  assert.equal(hot.children[3].children[0].children[1].getAttribute("aria-label"), "将 file-id.txt 设为冷文件");
  assert.equal(hot.children[3].children[0].children[0].dataset.state, "hot");
  assert.equal(cold.children[3].children[0].children[0].dataset.state, "cold");
  assert.equal(cold.children[3].children[0].children[1].hidden, true);
  assert.equal(cold.children[3].children[0].children[1].disabled, true);
  assert.equal(hot.children[4].children[0].children.length, 4);
  assert.equal(ui.$("storage-config").hidden, true);
  assert.deepEqual(ui.requests.map((request) => request.path), ["/api/files"]);
});

test("disabled storage keeps manual controls visible with configuration guidance and legacy downloads", async (context) => {
  const ui = await harness(context, { ok: true, storage_enabled: false, files: [file(null)] });
  const actions = ui.rows()[0].children[4].children[0];
  const cold = ui.rows()[0].children[3].children[0].children[1];
  assert.equal(cold.textContent, "设为冷文件");
  assert.equal(cold.disabled, true);
  assert.equal(cold.hidden, false);
  assert.equal(ui.$("storage-config").hidden, false);
  assert.match(ui.$("storage-config").textContent, /S3.*加密主密钥/);
  const event = await actions.children[1].dispatch("click");
  assert.equal(event.defaultPrevented, undefined);
  assert.equal(ui.requests.length, 1);
});

test("manual archive displays its queue position without fake progress and cancellation refreshes the hot state", async (context) => {
  const job = { job_id: "archive-id", kind: "archive", state: "queued", progress: 12, queue_position: 3, error: null, cancellable: true };
  const ui = await harness(context, { ok: true, storage_enabled: true, files: [file()] }, (path) => {
    if (path.endsWith("/cold")) return { ok: true, storage_state: "archiving", storage_job: job };
    if (path.endsWith("/storage")) return { ok: true, storage_state: "hot", storage_job: { ...job, state: "cancelled", cancellable: false } };
    return { ok: true };
  });
  const row = ui.rows()[0];
  await row.children[3].children[0].children[1].dispatch("click");
  await tick();
  assert.equal(row.children[3].children[0].children[0].textContent, "等待转冷 · 排队第 3 个");
  assert.equal(row.children[3].children[1].getAttribute("aria-valuenow"), null);
  assert.equal(row.children[3].children[1].classes.has("is-indeterminate"), true);
  assert.equal(row.children[3].children[1].dataset.kind, "archive");
  assert.equal(row.children[3].children[0].children[1].hidden, true);
  const cancel = row.children[3].children[3].children[0];
  assert.equal(cancel.hidden, false);
  await cancel.dispatch("click");
  await tick();
  assert.equal(row.children[3].children[0].children[0].textContent, "热文件");
  assert.equal(cancel.hidden, true);
  assert.ok(ui.requests.some((request) => request.path === "/api/storage-jobs/archive-id/cancel" && request.method === "POST"));
});

test("running phases show reported progress while retries show their reason and delay", async (context) => {
  const verifying = { ...file("archiving", "verifying"), storage_job: { state: "running", kind: "archive", phase: "verifying", progress: 63, cancellable: true } };
  const retry = { ...file("archiving", "retry"), storage_job: { state: "retry", kind: "archive", progress: 45, retry_after: 12, error: "S3 temporarily unavailable", cancellable: true } };
  const queued = { ...file("restoring", "queued"), storage_job: { state: "queued", kind: "restore", progress: 0, cancellable: false } };
  const ui = await harness(context, { ok: true, storage_enabled: true, files: [verifying, retry, queued] }, undefined, { manualPoll: true });
  const [first, second, third] = ui.rows().map((row) => row.children[3]);
  assert.equal(first.children[0].children[0].textContent, "正在校验归档 · 63%");
  assert.equal(first.children[1].getAttribute("aria-valuenow"), "63");
  assert.equal(first.children[1].children[0].style.width, "63%");
  assert.equal(second.children[0].children[0].textContent, "将在 12 秒后自动重试");
  assert.equal(second.children[1].getAttribute("aria-valuenow"), null);
  assert.equal(second.children[2].hidden, false);
  assert.match(second.children[2].textContent, /S3 temporarily unavailable/);
  assert.equal(second.children[3].children[1].hidden, true);
  assert.equal(third.children[0].children[0].textContent, "等待恢复");
  assert.doesNotMatch(third.children[0].children[0].textContent, /0%/);
});

test("completed archive responses update a whole queue and remain correct across pagination", async (context) => {
  const files = Array.from({ length: 25 }, (_, index) => ({
    ...file("archiving", `file-${index}`),
    storage_job: { job_id: `archive-${index}`, state: "queued", kind: "archive", progress: 0, cancellable: true },
  }));
  const ui = await harness(context, { ok: true, storage_enabled: true, files }, () => ({
    ok: true, storage_state: "cold", storage_job: { state: "completed", kind: "archive", progress: 100, cancellable: false },
  }), { manualPoll: true });
  assert.equal(ui.rows()[0].children[3].children[0].children[0].textContent, "等待转冷");
  await ui.poll();
  assert.equal(ui.requests.length, 26);
  assert.ok(ui.requests.every((request) => request.cache === "no-store"));
  for (const row of ui.rows()) {
    assert.equal(row.children[3].children[0].children[0].textContent, "冷文件");
    assert.equal(row.children[3].children[1].hidden, false);
    assert.equal(row.children[3].children[1].getAttribute("aria-valuenow"), "100");
    assert.equal(row.children[3].children[1].children[0].style.width, "100%");
  }
  ui.finishAnimations();
  for (const row of ui.rows()) assert.equal(row.children[3].children[1].hidden, true);
  await ui.$("next-page").dispatch("click");
  assert.equal(ui.rows().length, 5);
  for (const row of ui.rows()) assert.equal(row.children[3].children[0].children[0].textContent, "冷文件");
  assert.equal(ui.pendingPolls(), 0);
});

test("an immediate completed POST animates completion, while historical completed jobs stay hidden", async (context) => {
  const historical = { job_id: "old-archive", state: "completed", kind: "archive", progress: 100, cancellable: false };
  const ui = await harness(context, { ok: true, storage_enabled: true, files: [{ ...file("hot_backed"), storage_job: historical }] }, () => ({
    ok: true, storage_state: "cold", storage_job: { ...historical, job_id: "new-archive" },
  }), { manualPoll: true });
  const row = ui.rows()[0];
  const bar = row.children[3].children[1];
  assert.equal(bar.hidden, true);
  assert.equal(ui.completionTimers.length, 0);
  await row.children[3].children[0].children[1].dispatch("click");
  await tick();
  assert.equal(row.children[3].children[0].children[0].textContent, "冷文件");
  assert.equal(bar.hidden, false);
  assert.equal(bar.children[0].style.width, "100%");
  assert.equal(row.children[3].children[0].children[1].hidden, true);
  assert.equal(ui.completionTimers.length, 1);
  ui.finishAnimations();
  assert.equal(bar.hidden, true);
});

test("a completion timer cannot hide a new restore or repopulate a locked page", async (context) => {
  const archived = { job_id: "archive", state: "completed", kind: "archive", progress: 100, cancellable: false };
  const ui = await harness(context, { ok: true, storage_enabled: true, files: [file()] }, (path) => {
    if (path.endsWith("/cold")) return { ok: true, storage_state: "cold", storage_job: archived };
    return { ok: true, storage_state: "restoring", storage_job: { job_id: "restore", state: "running", kind: "restore", progress: 0, cancellable: false } };
  }, { manualPoll: true });
  const row = ui.rows()[0];
  const bar = row.children[3].children[1];
  await row.children[3].children[0].children[1].dispatch("click");
  await tick();
  const previousTimer = ui.completionTimers[0];
  assert.ok(previousTimer);
  await row.children[4].children[0].children[1].dispatch("click");
  await tick();
  assert.equal(previousTimer.cancelled, true);
  previousTimer.callback(); // Even a callback already queued on the event loop is obsolete.
  assert.equal(bar.hidden, false);
  assert.equal(bar.classes.has("is-indeterminate"), true);
  assert.equal(bar.getAttribute("aria-valuenow"), null);
  assert.equal(bar.dataset.kind, "restore");
  assert.equal(row.children[3].children[0].children[0].textContent, "正在恢复");
  await ui.$("logout").dispatch("click");
  previousTimer.callback();
  assert.equal(ui.rows().length, 0);
});

test("refresh and logout cancel completion holds without replaying historical completion", async (context) => {
  const initial = { ok: true, storage_enabled: true, files: [file()] };
  let job = 0;
  const ui = await harness(context, initial, () => ({
    ok: true, storage_state: "hot_backed", storage_job: { job_id: `archive-${++job}`, state: "completed", kind: "archive", progress: 100, cancellable: false },
  }), { manualPoll: true });
  await ui.rows()[0].children[3].children[0].children[1].dispatch("click");
  await tick();
  const refreshTimer = ui.completionTimers[0];
  assert.equal(ui.rows()[0].children[3].children[1].hidden, false);
  await ui.$("refresh").dispatch("click");
  assert.equal(refreshTimer.cancelled, true);
  refreshTimer.callback();
  assert.equal(ui.rows()[0].children[3].children[1].hidden, true);
  await ui.rows()[0].children[3].children[0].children[1].dispatch("click");
  await tick();
  const logoutTimer = ui.completionTimers[1];
  assert.equal(ui.rows()[0].children[3].children[1].hidden, false);
  await ui.$("logout").dispatch("click");
  assert.equal(logoutTimer.cancelled, true);
  logoutTimer.callback();
  assert.equal(ui.rows().length, 0);
});

test("refresh reconstructs a queued task and continues polling the same job through completion", async (context) => {
  const job = { job_id: "durable-archive", state: "queued", kind: "archive", progress: 0, cancellable: true };
  const initial = { ok: true, storage_enabled: true, files: [{ ...file("archiving"), storage_job: job }] };
  let reads = 0;
  const ui = await harness(context, initial, () => {
    reads++;
    return reads === 1
      ? { ok: true, storage_state: "archiving", storage_job: { ...job, state: "running", phase: "uploading", progress: 45 } }
      : { ok: true, storage_state: "cold", storage_job: { ...job, state: "completed", progress: 100, cancellable: false } };
  }, { manualPoll: true });
  const beforeRefresh = ui.rows()[0];
  await ui.$("refresh").dispatch("click");
  assert.notEqual(ui.rows()[0], beforeRefresh);
  assert.equal(ui.pendingPolls(), 1);
  await ui.poll();
  assert.equal(ui.rows()[0].children[3].children[0].children[0].textContent, "正在上传 · 45%");
  await ui.$("refresh").dispatch("click");
  assert.equal(ui.rows()[0].children[3].children[0].children[0].textContent, "正在上传 · 45%");
  await ui.poll();
  assert.equal(ui.rows()[0].children[3].children[0].children[0].textContent, "冷文件");
  assert.equal(ui.rows()[0].children[3].children[1].getAttribute("aria-valuenow"), "100");
  assert.equal(ui.pendingPolls(), 0);
  assert.ok(ui.requests.filter((request) => request.path.endsWith("/storage")).every((request) => request.path === "/api/files/file-id/storage"));
  ui.finishAnimations();
  assert.equal(ui.rows()[0].children[3].children[1].hidden, true);
});

test("a late status error cannot overwrite a completed row supplied by refresh", async (context) => {
  const initial = { ok: true, storage_enabled: true, files: [{
    ...file("archiving"), storage_job: { job_id: "archive", state: "queued", kind: "archive", progress: 0, cancellable: true },
  }] };
  let failRead;
  const ui = await harness(context, initial, () => new Promise((_resolve, reject) => { failRead = reject; }), { manualPoll: true });
  const polling = ui.poll();
  await tick();
  initial.files = [{ ...file("cold"), storage_job: { job_id: "archive", state: "completed", kind: "archive", progress: 100, cancellable: false } }];
  await ui.$("refresh").dispatch("click");
  failRead(new Error("Old request failed"));
  await polling;
  assert.equal(ui.rows()[0].children[3].children[0].children[0].textContent, "冷文件");
  assert.equal(ui.rows()[0].children[3].children[2].hidden, true);
  assert.equal(ui.pendingPolls(), 0);
});

test("batch restore capacity failure gives a batching suggestion and never starts ZIP download", async (context) => {
  const ui = await harness(context, { ok: true, storage_enabled: true, files: [file("cold")] }, () => ({ ok: false, httpStatus: 507, error: "Disk full", error_code: "insufficient_storage" }));
  ui.$("select-all").checked = true;
  await ui.$("select-all").dispatch("change");
  await ui.$("bulk-download").dispatch("click");
  assert.match(ui.$("notice").textContent, /分批下载/);
  assert.equal(ui.$("download-frame").src, null);
  assert.ok(ui.requests.some((request) => request.path === "/api/files/file-id/restore"));
  assert.equal(ui.$("cancel-download-wait").hidden, true);
});
