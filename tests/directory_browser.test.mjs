import assert from "node:assert/strict";
import test from "node:test";
import { directoryBreadcrumbs, setupDirectoryBrowser } from "../static/directory-browser.mjs";

class Element {
  constructor(tag = "div") {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.listeners = new Map();
    this.attributes = new Map();
    this.open = false;
    this.classList = { toggle() {} };
  }
  set textContent(value) { this.text = value; this.children = []; }
  get textContent() { return this.text || ""; }
  set href(value) { this.setAttribute("href", value); }
  get href() { return this.getAttribute("href"); }
  setAttribute(key, value) { this.attributes.set(key, value); }
  getAttribute(key) { return this.attributes.get(key) ?? null; }
  removeAttribute(key) { this.attributes.delete(key); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(key, callback) {
    if (!this.listeners.has(key)) this.listeners.set(key, []);
    this.listeners.get(key).push(callback);
  }
  fire(key, event = { preventDefault() {} }) { for (const callback of this.listeners.get(key) || []) callback(event); }
  showModal() { this.open = true; }
  close() { this.open = false; this.fire("close"); }
}

const tick = () => new Promise((resolve) => setImmediate(resolve));
const root = { file_id: "folder-id", filename: "结果", file_count: 2, size: 1234, download_url: "/f/folder-id/results.zip", download_filename: "结果.zip" };
const child = { filename: "same.txt", path: "nested/same.txt", kind: "file", size: 4, preview_type: "text", preview_url: "/p/folder-id/same.txt?path=nested%2Fsame.txt", download_url: "/f/folder-id/same.txt?path=nested%2Fsame.txt" };

function harness(context, responder) {
  const nodes = new Map();
  const $ = (id) => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const previous = new Map(["document", "location"].map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  globalThis.document = { getElementById: $, createElement: (tag) => new Element(tag) };
  globalThis.location = { origin: "http://localhost", href: "http://localhost/" };
  const state = { language: "en", requests: [], previews: [], auth: 0 };
  const browser = setupDirectoryBrowser({
    t: (key, values) => `${state.language}:${key}${values ? JSON.stringify(values) : ""}`,
    formatSize: (size) => `${size} B`,
    api: async (path) => { state.requests.push(path); return responder(path); },
    onPreview: (entry) => state.previews.push(entry),
    onAuthRequired: () => state.auth++,
  });
  context.after(() => {
    browser.close();
    for (const [key, descriptor] of previous) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  });
  return { $, state, browser };
}

test("breadcrumbs preserve each nested level and Unicode names", () => {
  assert.deepEqual(directoryBreadcrumbs("结果", "one/two"), [{ name: "结果", path: "" }, { name: "one", path: "one" }, { name: "two", path: "one/two" }]);
});

test("folder browser opens subdirectories, previews children, downloads ZIP, and returns to root", async (context) => {
  const folder = { kind: "directory", filename: "nested", path: "nested" };
  const ui = harness(context, (path) => ({ entries: path.endsWith("path=nested") ? [child] : [folder] }));
  await ui.browser.open(root);
  assert.equal(ui.$("directory-dialog").open, true);
  assert.equal(ui.$("directory-download").href, root.download_url);
  assert.equal(ui.$("directory-download").download, "结果.zip");
  assert.equal(ui.$("directory-back").disabled, true);
  ui.$("directory-entries").children[0].children[1].children[0].fire("click");
  await tick();
  assert.equal(ui.state.requests[1], "/api/directories/folder-id?path=nested");
  assert.equal(ui.$("directory-breadcrumbs").children.length, 2);
  const actions = ui.$("directory-entries").children[0].children[1];
  assert.equal(actions.children[1].href, child.download_url);
  assert.equal(actions.children[1].download, "same.txt");
  actions.children[0].fire("click");
  assert.deepEqual(ui.state.previews, [child]);
  ui.$("directory-back").fire("click");
  await tick();
  assert.equal(ui.state.requests.at(-1), "/api/directories/folder-id?path=");
  assert.equal(ui.$("directory-back").disabled, true);
});

test("close and subsequent navigation discard stale responses", async (context) => {
  let resolveOld;
  const pending = new Promise((resolve) => { resolveOld = resolve; });
  const ui = harness(context, (path) => path.includes("folder-id") ? pending : { entries: [] });
  const opening = ui.browser.open(root);
  ui.browser.close();
  await ui.browser.open({ ...root, file_id: "new-id", filename: "new" });
  resolveOld({ entries: [child] });
  await opening;
  assert.deepEqual(ui.$("directory-entries").children, []);
  assert.equal(ui.$("directory-title").textContent, "new");
  assert.equal(ui.$("directory-status").textContent, "en:directoryEmpty");
});

test("expired directory authentication closes the dialog and clears links", async (context) => {
  const ui = harness(context, () => { throw Object.assign(new Error("token"), { status: 401 }); });
  await ui.browser.open(root);
  assert.equal(ui.state.auth, 1);
  assert.equal(ui.$("directory-dialog").open, false);
  assert.equal(ui.$("directory-download").href, null);
});

test("missing directories show a recoverable state and language changes keep the current entries", async (context) => {
  let missing = true;
  const ui = harness(context, () => {
    if (missing) throw Object.assign(new Error("missing"), { status: 404 });
    return { entries: [{ ...child, filename: "<script>alert(1)</script>" }] };
  });
  await ui.browser.open(root);
  assert.equal(ui.$("directory-status").textContent, "en:directoryMissing");
  assert.equal(ui.$("directory-retry").disabled, false);
  missing = false;
  ui.$("directory-retry").fire("click");
  await tick();
  ui.state.language = "zh";
  ui.browser.render();
  assert.equal(ui.$("directory-entries").children[0].children[0].children[0].textContent, "<script>alert(1)</script>");
  assert.equal(ui.$("directory-download").textContent, "zh:downloadDirectory");
  assert.equal(ui.state.requests.length, 2);
});

test("unsafe child download links cannot be followed", async (context) => {
  const ui = harness(context, () => ({ entries: [{ ...child, download_url: "javascript:alert(1)" }] }));
  await ui.browser.open({ ...root, download_url: "//other.test/f/file" });
  assert.equal(ui.$("directory-download").href, null);
  assert.equal(ui.$("directory-entries").children[0].children[1].children[1].href, null);
});
