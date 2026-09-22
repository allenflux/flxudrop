import assert from "node:assert/strict";
import test from "node:test";
import { setupPreview } from "../static/file-preview.mjs";

class Element {
  constructor(tagName = "div") {
    this.tagName = tagName.toUpperCase();
    this.children = [];
    this.attributes = new Map();
    this.listeners = new Map();
    this.open = false;
    this._text = "";
    this.classList = { toggle() {} };
  }
  set textContent(value) { this._text = value; this.children = []; }
  get textContent() { return this._text; }
  set href(value) { this.setAttribute("href", value); }
  get href() { return this.getAttribute("href"); }
  set src(value) { this.setAttribute("src", value); }
  get src() { return this.getAttribute("src"); }
  setAttribute(key, value) { this.attributes.set(key, value); }
  getAttribute(key) { return this.attributes.get(key) ?? null; }
  removeAttribute(key) { this.attributes.delete(key); }
  replaceChildren(...children) { this._text = ""; this.children = children; }
  addEventListener(name, callback) {
    const callbacks = this.listeners.get(name) || [];
    callbacks.push(callback);
    this.listeners.set(name, callbacks);
  }
  fire(name, event = { preventDefault() {} }) {
    for (const callback of this.listeners.get(name) || []) callback(event);
  }
  showModal() { this.open = true; }
  close() { this.open = false; this.fire("close"); }
  pause() { this.paused = true; }
  load() { this.reloaded = true; }
}

function response(text = "hello", status = 200, truncated = false) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => text,
    headers: new Headers({ "X-Preview-Truncated": String(truncated) }),
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

function fixture(kind = "text", id = "first") {
  return {
    filename: `${id}.txt`, size: 5, preview_type: kind,
    preview_url: `/p/${id}/${id}.txt`, download_url: `/f/${id}/${id}.txt`,
  };
}

function harness(testContext, responder = () => response()) {
  const nodes = new Map([
    ["preview-dialog", new Element("dialog")], ["preview-title", new Element("h2")],
    ["preview-meta", new Element("p")], ["preview-content", new Element()],
    ["preview-status", new Element("p")], ["preview-download", new Element("a")],
    ["close-preview", new Element("button")],
  ]);
  let language = "en";
  const requests = [];
  const previous = new Map(["document", "location", "fetch"].map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  globalThis.document = { getElementById: (id) => nodes.get(id), createElement: (tag) => new Element(tag) };
  globalThis.location = { href: "http://localhost/", origin: "http://localhost" };
  globalThis.fetch = async (url, options) => {
    requests.push({ url, ...options });
    return responder(url, options);
  };
  const preview = setupPreview({
    t: (key, values) => `${language}:${key}${values?.name ? `:${values.name}` : ""}`,
    formatSize: (size) => `${size} B`,
  });
  testContext.after(() => {
    preview.close();
    for (const [key, descriptor] of previous) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  });
  return { preview, nodes, requests, setLanguage: (value) => { language = value; } };
}

test("HTML/SVG uploads remain literal text and language changes preserve the loaded content", async (t) => {
  const hostileText = '<svg onload="alert(1)"><script>alert(2)</script></svg>';
  const ui = harness(t, () => response(hostileText, 200, true));
  await ui.preview.open(fixture());
  const pre = ui.nodes.get("preview-content").children[0];
  assert.equal(pre.tagName, "PRE");
  assert.equal(pre.textContent, hostileText);
  assert.deepEqual(pre.children, []);
  assert.equal(ui.nodes.get("preview-status").textContent, "en:previewTruncated");
  ui.setLanguage("zh");
  ui.preview.render();
  assert.equal(ui.nodes.get("preview-content").children[0], pre);
  assert.equal(ui.nodes.get("preview-status").textContent, "zh:previewTruncated");
  assert.equal(ui.nodes.get("preview-download").textContent, "zh:download");
  assert.equal(ui.requests.length, 1);
});

test("switching files aborts the old request and ignores its eventual text", async (t) => {
  const oldText = deferred();
  const ui = harness(t, (url) => url.includes("/first/") ? { ...response(), text: () => oldText.promise } : response("new text"));
  const first = ui.preview.open(fixture());
  await Promise.resolve();
  await ui.preview.open(fixture("text", "second"));
  assert.equal(ui.requests[0].signal.aborted, true);
  oldText.resolve("stale text");
  await first;
  assert.equal(ui.nodes.get("preview-content").children[0].textContent, "new text");
  assert.equal(ui.nodes.get("preview-title").textContent, "second.txt");
});

test("closing an in-flight preview prevents the response from reopening or repopulating it", async (t) => {
  const pending = deferred();
  const ui = harness(t, () => pending.promise);
  const opening = ui.preview.open(fixture());
  ui.preview.close();
  assert.equal(ui.requests[0].signal.aborted, true);
  pending.resolve(response("too late"));
  await opening;
  assert.equal(ui.nodes.get("preview-dialog").open, false);
  assert.deepEqual(ui.nodes.get("preview-content").children, []);
  assert.equal(ui.nodes.get("preview-download").href, null);
});

test("missing text and media are reported without embedding an error page", async (t) => {
  const ui = harness(t, () => response("missing", 404));
  for (const kind of ["text", "image", "pdf", "audio", "video"]) {
    await ui.preview.open(fixture(kind));
    assert.equal(ui.nodes.get("preview-status").textContent, "en:previewMissing");
    assert.deepEqual(ui.nodes.get("preview-content").children, []);
    assert.equal(ui.requests.at(-1).method, kind === "text" ? "GET" : "HEAD");
  }
});

test("unsupported and empty files retain the download action", async (t) => {
  const ui = harness(t, () => response(""));
  await ui.preview.open(fixture("unsupported"));
  assert.equal(ui.requests.length, 0);
  assert.equal(ui.nodes.get("preview-status").textContent, "en:previewUnsupported");
  assert.equal(ui.nodes.get("preview-download").href, "/f/first/first.txt");
  await ui.preview.open(fixture());
  assert.equal(ui.nodes.get("preview-status").textContent, "en:previewEmpty");
  assert.equal(ui.nodes.get("preview-download").href, "/f/first/first.txt");
});

test("foreign, executable and path-escaping preview URLs never reach fetch or a viewer", async (t) => {
  const ui = harness(t);
  for (const url of ["javascript:alert(1)", "//example.com/p/id/test", "https://example.com/p/id/test", "/p/../f/id/test", "/p/\\example.com/test"]) {
    await ui.preview.open({ ...fixture("pdf"), preview_url: url, download_url: "javascript:alert(1)" });
    assert.equal(ui.nodes.get("preview-status").textContent, "en:previewError");
    assert.equal(ui.nodes.get("preview-download").href, null);
    assert.deepEqual(ui.nodes.get("preview-content").children, []);
  }
  assert.equal(ui.requests.length, 0);
});

test("media playback stops on close and stale load/error callbacks do not change a new preview", async (t) => {
  const ui = harness(t);
  await ui.preview.open(fixture("video"));
  const video = ui.nodes.get("preview-content").children[0];
  assert.equal(video.controls, true);
  video.fire("loadedmetadata");
  assert.equal(ui.nodes.get("preview-status").hidden, true);
  ui.preview.close();
  assert.equal(video.paused, true);
  assert.equal(video.src, null);
  assert.equal(video.reloaded, true);
  await ui.preview.open(fixture("unsupported", "second"));
  video.fire("error");
  assert.equal(ui.nodes.get("preview-status").textContent, "en:previewUnsupported");
});

test("PDF frames use the verified local endpoint and their accessible title follows the selected language", async (t) => {
  const ui = harness(t);
  await ui.preview.open(fixture("pdf"));
  const frame = ui.nodes.get("preview-content").children[0];
  assert.equal(frame.tagName, "IFRAME");
  assert.equal(frame.src, "/p/first/first.txt");
  assert.equal(frame.getAttribute("sandbox"), null);
  assert.equal(frame.getAttribute("referrerpolicy"), "no-referrer");
  assert.equal(frame.title, "en:previewFile:first.txt");
  ui.setLanguage("zh");
  ui.preview.render();
  assert.equal(frame.title, "zh:previewFile:first.txt");
});

test("transport failures show a recoverable error and closing by Escape clears content", async (t) => {
  const ui = harness(t, () => { throw new Error("network failure"); });
  await ui.preview.open(fixture());
  assert.equal(ui.nodes.get("preview-status").textContent, "en:previewError");
  assert.equal(ui.nodes.get("preview-download").href, "/f/first/first.txt");
  let prevented = false;
  ui.nodes.get("preview-dialog").fire("cancel", { preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  assert.equal(ui.nodes.get("preview-dialog").open, false);
});
