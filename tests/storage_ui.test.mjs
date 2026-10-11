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
  get textContent() { return this.text || ""; }
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

async function harness(context, initial, responder = () => ({ ok: true })) {
  const nodes = new Map();
  const $ = (id) => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const document = new Element();
  document.documentElement = new Element("html");
  document.getElementById = $;
  document.createElement = (tag) => new Element(tag);
  document.createDocumentFragment = () => Object.assign(new Element(), { fragment: true });
  const requests = [];
  const globals = {
    document, navigator: { language: "zh-CN" }, localStorage: { getItem() { return null; }, setItem() {} },
    location: { href: "http://localhost/", origin: "http://localhost" },
    fetch: async (path, options) => {
      requests.push({ path, ...options });
      const data = path === "/api/files" ? initial : await responder(path, options);
      const status = data.httpStatus || 200;
      return { ok: status < 400, status, json: async () => data };
    },
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
  return { $, requests, rows: () => $("file-list").children };
}

test("cold files remain listed and hot rows expose a visible manual cold action", async (context) => {
  const ui = await harness(context, { ok: true, storage_enabled: true, files: [file("hot"), file("cold", "cold-id")] });
  assert.equal(ui.rows().length, 2);
  const hot = ui.rows()[0];
  const cold = ui.rows()[1];
  assert.equal(hot.children[3].children[0].textContent, "热文件");
  assert.equal(cold.children[3].children[0].textContent, "冷文件");
  assert.equal(hot.children[4].children[0].children[4].textContent, "设为冷文件");
  assert.equal(hot.children[4].children[0].children[4].disabled, false);
  assert.equal(cold.children[4].children[0].children[4].textContent, "已是冷文件");
  assert.equal(cold.children[4].children[0].children[4].disabled, true);
  assert.equal(ui.$("storage-config").hidden, true);
  assert.deepEqual(ui.requests.map((request) => request.path), ["/api/files"]);
});

test("disabled storage keeps manual controls visible with configuration guidance and legacy downloads", async (context) => {
  const ui = await harness(context, { ok: true, storage_enabled: false, files: [file(null)] });
  const actions = ui.rows()[0].children[4].children[0];
  assert.equal(actions.children[4].textContent, "设为冷文件");
  assert.equal(actions.children[4].disabled, true);
  assert.equal(ui.$("storage-config").hidden, false);
  assert.match(ui.$("storage-config").textContent, /S3.*加密主密钥/);
  const event = await actions.children[1].dispatch("click");
  assert.equal(event.defaultPrevented, undefined);
  assert.equal(ui.requests.length, 1);
});

test("manual archive displays queued progress and cancellation refreshes the hot state", async (context) => {
  const job = { job_id: "archive-id", kind: "archive", state: "queued", progress: 12, error: null, cancellable: true };
  const ui = await harness(context, { ok: true, storage_enabled: true, files: [file()] }, (path) => {
    if (path.endsWith("/cold")) return { ok: true, storage_state: "archiving", storage_job: job };
    if (path.endsWith("/storage")) return { ok: true, storage_state: "hot", storage_job: { ...job, state: "cancelled", cancellable: false } };
    return { ok: true };
  });
  const row = ui.rows()[0];
  await row.children[4].children[0].children[4].dispatch("click");
  await tick();
  assert.match(row.children[3].children[0].textContent, /等待转冷.*12%/);
  const cancel = row.children[3].children[3].children[0];
  assert.equal(cancel.hidden, false);
  await cancel.dispatch("click");
  await tick();
  assert.equal(row.children[3].children[0].textContent, "热文件");
  assert.equal(cancel.hidden, true);
  assert.ok(ui.requests.some((request) => request.path === "/api/storage-jobs/archive-id/cancel" && request.method === "POST"));
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
