import assert from "node:assert/strict";
import test from "node:test";
import { copyText, droppedFiles, hasFileTransfer, sendUpload, setupUploads, textUpload } from "../static/browser-upload.mjs";

class Element {
  constructor(tag = "div", document = null) {
    this.tagName = tag.toUpperCase();
    this.document = document;
    this.listeners = new Map();
    this.children = [];
    this.attributes = new Map();
    this.value = "";
    this.textContent = "";
    this.style = {};
    this.classes = new Set();
    this.classList = {
      add: (value) => this.classes.add(value),
      remove: (value) => this.classes.delete(value),
      toggle: (value, enabled) => enabled ? this.classes.add(value) : this.classes.delete(value),
    };
  }
  addEventListener(type, callback) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(callback);
  }
  async dispatch(type, properties = {}) {
    const event = {
      target: this, defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; },
      stopPropagation() {}, ...properties,
    };
    for (const callback of this.listeners.get(type) || []) await callback(event);
    return event;
  }
  setAttribute(key, value) { this.attributes.set(key, value); }
  append(...children) {
    this.children.push(...children);
    for (const child of children) child.parent = this;
  }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  remove() { this.parent.children = this.parent.children.filter((child) => child !== this); }
  focus() { if (this.document) this.document.activeElement = this; }
  select() { this.selected = true; }
  setSelectionRange(start, end) { this.selection = [start, end]; }
  click() { this.clicked = true; }
}

class FakeXHR {
  static instances = [];
  constructor() {
    this.headers = {};
    this.upload = {};
    FakeXHR.instances.push(this);
  }
  open(method, url) { this.method = method; this.url = url; }
  setRequestHeader(key, value) { this.headers[key] = value; }
  send(body) { this.body = body; }
  abort() { this.aborted = true; this.onabort?.(); }
  respond(status, data) {
    this.status = status;
    this.responseText = typeof data === "string" ? data : JSON.stringify(data);
    this.onload();
  }
}

const tick = () => new Promise((resolve) => setImmediate(resolve));
const file = (name, text = "") => Object.assign(new Blob([text]), { name });
const success = (filename = "stored.txt") => ({ ok: true, filename, download_url: `http://example.test/f/id/${encodeURIComponent(filename)}` });

function environment(context, initiallyAllowed = true) {
  const document = new Element();
  document.body = new Element("body", document);
  document.elements = new Map();
  document.createElement = (tag) => new Element(tag, document);
  document.getElementById = (id) => {
    if (!document.elements.has(id)) document.elements.set(id, new Element("div", document));
    return document.elements.get(id);
  };
  const original = new Map();
  for (const [key, value] of Object.entries({ document, location: { href: "http://example.test/" }, XMLHttpRequest: FakeXHR })) {
    original.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  }
  context.after(() => {
    for (const [key, descriptor] of original) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  });
  FakeXHR.instances = [];
  const state = { allowed: initiallyAllowed, refreshed: 0, authRequired: 0, busyChanges: [] };
  const controller = setupUploads({
    t: (key, values = {}) => `${key} ${JSON.stringify(values)}`,
    getToken: () => "secret-token",
    canUpload: () => state.allowed,
    onAuthRequired: () => { state.authRequired++; state.allowed = false; controller.render(); },
    onUploaded: () => state.refreshed++,
    formatSize: (size) => `${size} B`,
    onBusyChange: (busy) => state.busyChanges.push(busy),
  });
  return { document, $: document.getElementById, state, controller };
}

test("text uploads preserve exact UTF-8 content and select a usable filename", async () => {
  const content = " 你好\n<script>alert(1)</script>\n🙂 ";
  const upload = textUpload(content, "  pasted  ");
  assert.equal(upload.name, "pasted.txt");
  assert.equal(upload.blob.type, "text/plain;charset=utf-8");
  assert.equal(await upload.blob.text(), content);
  assert.equal(upload.blob.size, Buffer.byteLength(content));
  assert.equal(textUpload("x", "  ").name, "note.txt");
  assert.equal(textUpload("x", "script.py").name, "script.py");
});

test("directory drops exclude folder placeholders but retain empty real files", async () => {
  const realFile = file("empty.txt");
  const transfer = {
    types: ["Files"],
    items: [
      { kind: "file", getAsFile: () => file("folder"), webkitGetAsEntry: () => ({ isDirectory: true }) },
      { kind: "file", getAsFile: () => realFile, webkitGetAsEntry: () => ({ isFile: true }) },
      { kind: "string", getAsFile: () => null },
    ],
  };
  assert.deepEqual(await droppedFiles(transfer), { files: [realFile], hasDirectories: true });
  assert.equal(hasFileTransfer(transfer), true);
  assert.equal(hasFileTransfer({ types: ["text/plain"], items: [{ kind: "string" }] }), false);
  assert.deepEqual(await droppedFiles({ files: [realFile] }), { files: [realFile], hasDirectories: false });
  assert.deepEqual(await droppedFiles({ items: [{ kind: "file", getAsFile: () => file("folder"), getAsFileSystemHandle: async () => ({ kind: "directory" }) }] }), { files: [], hasDirectories: true });
});

test("raw uploads encode names, send the token and body, and expose progress and HTTP auth errors", async (context) => {
  environment(context);
  const blob = new Blob(["你好"]);
  const progress = [];
  const upload = sendUpload({ blob, name: "文档 #1.txt", token: "test-token", onProgress: (value) => progress.push(value) });
  const request = FakeXHR.instances[0];
  assert.equal(request.method, "PUT");
  assert.equal(request.url, "/upload/%E6%96%87%E6%A1%A3%20%231.txt");
  assert.equal(request.headers["X-Upload-Token"], "test-token");
  assert.equal(request.body, blob);
  request.upload.onprogress({ lengthComputable: true, loaded: 3, total: 6 });
  assert.deepEqual(progress, [50]);
  request.respond(401, { ok: false, error: "Missing token" });
  await assert.rejects(upload.promise, (error) => error.status === 401 && error.detail === "Missing token");
});

test("browser upload waits for auth, prevents duplicate saves, and clears only a confirmed draft", async (context) => {
  const { $, state, controller } = environment(context, false);
  $("text-content").value = "copied text";
  await $("text-form").dispatch("submit");
  assert.equal(FakeXHR.instances.length, 0);
  assert.equal($("save-text").disabled, true);
  assert.deepEqual(state.busyChanges, []);
  state.allowed = true;
  controller.render();
  await $("text-form").dispatch("submit");
  await $("text-form").dispatch("submit");
  assert.equal(FakeXHR.instances.length, 1);
  assert.equal(FakeXHR.instances[0].url, "/upload/note.txt");
  assert.equal(controller.isBusy(), true);
  FakeXHR.instances[0].respond(201, success("server filename.txt"));
  await tick();
  assert.equal($("text-content").value, "");
  assert.equal(state.refreshed, 1);
  assert.deepEqual(state.busyChanges, [true, false]);
  const [title, controls] = $("upload-results").children[0].children;
  assert.equal(title.textContent, "server filename.txt");
  assert.equal(controls.children[0].value, "http://example.test/f/id/server%20filename.txt");
  assert.equal(controls.children[0].readOnly, true);
  assert.equal(controls.children[2].download, "server filename.txt");
});

test("failed text saves preserve the draft and successful saves preserve edits made in flight", async (context) => {
  const { $, state } = environment(context);
  $("text-content").value = "original";
  $("text-filename").value = "notes.md";
  await $("text-form").dispatch("submit");
  FakeXHR.instances[0].respond(413, { ok: false, error: "File too large" });
  await tick();
  assert.equal($("text-content").value, "original");
  assert.equal($("text-filename").value, "notes.md");
  assert.match($("upload-status").textContent, /File too large/);
  assert.equal(state.refreshed, 0);
  await $("text-form").dispatch("submit");
  $("text-content").value = "new edits";
  FakeXHR.instances[1].respond(201, success());
  await tick();
  assert.equal($("text-content").value, "new edits");
  assert.equal(state.refreshed, 1);
});

test("multi-file batches run sequentially and report partial failure without losing successful links", async (context) => {
  const { $, state } = environment(context);
  $("file-input").files = [file("one.txt"), file("two.txt"), file("three.txt")];
  await $("file-input").dispatch("change");
  assert.equal(FakeXHR.instances.length, 1);
  FakeXHR.instances[0].respond(201, success("one.txt"));
  await tick();
  assert.equal(FakeXHR.instances.length, 2);
  FakeXHR.instances[1].respond(500, { ok: false, error: "Disk full" });
  await tick();
  FakeXHR.instances[2].respond(201, success("three.txt"));
  await tick();
  assert.equal($("upload-results").children.length, 2);
  assert.match($("upload-status").textContent, /uploadPartial.*"saved":2.*"failed":1.*two.txt: Disk full/);
  assert.equal(state.refreshed, 1);
});

test("401 stops remaining uploads and clears links while retaining a draft", async (context) => {
  const { $, state, controller } = environment(context);
  $("text-content").value = "keep me";
  $("file-input").files = [file("one.txt"), file("two.txt"), file("three.txt")];
  await $("file-input").dispatch("change");
  FakeXHR.instances[0].respond(201, success());
  await tick();
  FakeXHR.instances[1].respond(401, { ok: false, error: "Expired token" });
  await tick();
  assert.equal(FakeXHR.instances.length, 2);
  assert.equal(state.authRequired, 1);
  assert.equal(controller.isBusy(), false);
  assert.equal($("upload-results").children.length, 0);
  assert.equal($("text-content").value, "keep me");
  assert.equal(state.refreshed, 0);
  assert.deepEqual(state.busyChanges, [true, false]);
});

test("lock aborts an active upload and ignores late responses without losing draft text", async (context) => {
  const { $, state, controller } = environment(context);
  $("text-content").value = "my draft";
  await $("text-form").dispatch("submit");
  state.allowed = false;
  controller.reset();
  FakeXHR.instances[0].respond(201, success());
  await tick();
  assert.equal(FakeXHR.instances[0].aborted, true);
  assert.equal($("text-content").value, "my draft");
  assert.equal($("upload-results").children.length, 0);
  assert.equal(state.refreshed, 0);
  assert.equal($("save-text").disabled, true);
});

test("page-wide file drops cannot navigate away and text dragging remains native", async (context) => {
  const { document, $, state } = environment(context);
  const textDrag = await document.dispatch("drop", { dataTransfer: { types: ["text/plain"] } });
  assert.equal(textDrag.defaultPrevented, false);
  const transfer = { types: ["Files"], files: [file("from-desktop.txt", "hello")] };
  const drop = await document.dispatch("drop", { dataTransfer: transfer });
  assert.equal(drop.defaultPrevented, true);
  assert.equal(FakeXHR.instances.length, 1);
  FakeXHR.instances[0].respond(201, success());
  await tick();
  state.allowed = false;
  const locked = await document.dispatch("drop", { dataTransfer: transfer });
  assert.equal(locked.defaultPrevented, true);
  assert.equal(FakeXHR.instances.length, 1);
  assert.match($("upload-status").textContent, /uploadLocked/);
});

test("copy uses the HTTP fallback and leaves failed copies selectable", async (context) => {
  const { document } = environment(context);
  const original = Object.getOwnPropertyDescriptor(globalThis, "navigator");
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { clipboard: { writeText: async () => { throw new Error("denied"); } } } });
  context.after(() => {
    if (original) Object.defineProperty(globalThis, "navigator", original);
    else delete globalThis.navigator;
  });
  document.execCommand = (command) => command === "copy";
  assert.equal(await copyText("http://example.test/link"), true);
  assert.equal(document.body.children.length, 0);
  document.execCommand = () => false;
  const visible = document.createElement("input");
  document.body.append(visible);
  assert.equal(await copyText("http://example.test/manual", visible), false);
  assert.equal(document.activeElement, visible);
  assert.equal(visible.selected, true);
  assert.equal(visible.value, "http://example.test/manual");
});
