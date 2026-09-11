import assert from "node:assert/strict";
import test from "node:test";
import { pageFiles, reconcileSelection, bulkDownloadUrl, deleteBatch } from "../static/file-actions.mjs";
import { messages, translate } from "../static/i18n.mjs";

const files = Array.from({ length: 43 }, (_, i) => ({ file_id: `file-${i}`, filename: `${i}.txt` }));

test("pages contain 20 records with a stable last page and clamped bounds", () => {
  assert.equal(pageFiles(files, 1).items.length, 20);
  assert.equal(pageFiles(files, 2).items[0].file_id, "file-20");
  assert.equal(pageFiles(files, 3).items.length, 3);
  assert.equal(pageFiles(files, 99).page, 3);
  assert.deepEqual(pageFiles([], 3), { page: 1, pages: 1, items: [] });
});

test("selection survives changing pages and drops only absent IDs on refresh", () => {
  const selected = new Set([files[0].file_id, files[21].file_id]);
  pageFiles(files, 2);
  assert.deepEqual(reconcileSelection(selected, files), selected);
  assert.deepEqual(reconcileSelection(selected, files.slice(1)), new Set(["file-21"]));
});

test("ZIP URL includes only selected IDs and enforces size limits", () => {
  const url = new URL(bulkDownloadUrl(["one", "two"]), "http://localhost");
  assert.deepEqual(url.searchParams.getAll("file_id"), ["one", "two"]);
  assert.throws(() => bulkDownloadUrl([]), RangeError);
  assert.throws(() => bulkDownloadUrl(Array(101).fill("a")), RangeError);
});

test("batch deletes only supplied files and handles partial failures", async () => {
  const requested = [files[1], files[4], files[30]];
  const called = [];
  const result = await deleteBatch(requested, async (file) => {
    called.push(file.file_id);
    if (file === files[4]) throw { status: 500 };
  });
  assert.deepEqual(called, ["file-1", "file-4", "file-30"]);
  assert.deepEqual(result.deleted, [files[1], files[30]]);
  assert.deepEqual(result.failed, [files[4]]);
});

test("already absent files are classified separately from failed deletion", async () => {
  const result = await deleteBatch([files[0]], async () => { throw { status: 404 }; });
  assert.deepEqual(result.missing, [files[0]]);
  assert.deepEqual(result.failed, []);
});

test("transport failures stop later deletions for explicit retry", async () => {
  const called = [];
  const result = await deleteBatch(files.slice(0, 3), async (file) => {
    called.push(file);
    if (file === files[1]) throw new Error("connection lost");
  });
  assert.deepEqual(called, files.slice(0, 2));
  assert.deepEqual(result.deleted, [files[0]]);
  assert.deepEqual(result.failed, [files[1]]);
  assert.deepEqual(result.pending, [files[2]]);
});

test("expired auth and invalidated sessions stop remaining requests", async () => {
  const auth = await deleteBatch(files.slice(0, 3), async () => { throw { status: 401 }; });
  assert.equal(auth.authFailed, true);
  assert.deepEqual(auth.pending, files.slice(0, 3));
  let active = true;
  const result = await deleteBatch(files.slice(0, 3), async () => { active = false; }, () => {}, () => active);
  assert.equal(result.interrupted, true);
  assert.deepEqual(result.deleted, [files[0]]);
  assert.deepEqual(result.pending, files.slice(1, 3));
});

test("translations cover matching keys and variables in both languages", () => {
  assert.deepEqual(Object.keys(messages.zh).sort(), Object.keys(messages.en).sort());
  for (const key of Object.keys(messages.zh)) {
    const variables = (text) => [...text.matchAll(/\{(\w+)\}/g)].map((match) => match[1]).sort();
    assert.deepEqual(variables(messages.zh[key]), variables(messages.en[key]), key);
  }
  assert.equal(translate("en", "selected", { count: 5 }), "5 selected");
  assert.equal(translate("zh", "deletedStatus"), "已删除");
  assert.equal(translate("unsupported", "deletedStatus"), "Deleted");
});
