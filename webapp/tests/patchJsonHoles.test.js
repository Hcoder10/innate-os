// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Tests for rosClient.js's patchJsonHoles — zero dependencies, plain node:
//   node tests/patchJsonHoles.test.js
// rws serializes non-finite floats as empty array slots (`[2.48,,,,0.39]`),
// which strict JSON rejects. The patcher fills holes with null; it walks the
// string rather than regexing it so a `,,` inside a string value is never
// rewritten.

import assert from "node:assert/strict";

// rosClient's module-level singleton registers page-lifecycle listeners at
// import time; stub just enough of the browser for a headless import.
globalThis.window = { addEventListener() {} };
globalThis.document = { addEventListener() {} };

const { patchJsonHoles } = await import("../js/rosClient.js");

let passed = 0;
/** @param {string} name @param {() => void} fn */
function test(name, fn) {
  fn();
  passed += 1;
  console.log(`ok - ${name}`);
}

/** Patch, parse, and return the value — the round trip consumers rely on. */
function parse(raw) {
  return JSON.parse(patchJsonHoles(raw));
}

test("fills consecutive holes mid-array", () => {
  assert.deepEqual(parse("[2.48,,,,0.39]"), [2.48, null, null, null, 0.39]);
});

test("fills a leading hole", () => {
  assert.deepEqual(parse("[,1]"), [null, 1]);
});

test("fills trailing holes", () => {
  assert.deepEqual(parse("[1,,]"), [1, null, null]);
});

test("leaves an empty array empty", () => {
  assert.deepEqual(parse("[]"), []);
});

test("never rewrites a `,,` inside a string value", () => {
  const out = parse('{"frame_id":"cam,,left","ranges":[1,,2]}');
  assert.equal(out.frame_id, "cam,,left");
  assert.deepEqual(out.ranges, [1, null, 2]);
});

test("never rewrites `[,` inside a string value", () => {
  const out = parse('{"note":"a[,b","ranges":[,2]}');
  assert.equal(out.note, "a[,b");
  assert.deepEqual(out.ranges, [null, 2]);
});

test("tracks strings across escaped quotes", () => {
  const out = parse('{"s":"a\\",,b","ranges":[1,,2]}');
  assert.equal(out.s, 'a",,b');
  assert.deepEqual(out.ranges, [1, null, 2]);
});

test("valid JSON passes through byte-identical", () => {
  const raw = '{"op":"publish","topic":"/scan","msg":{"ranges":[0.5,1.25],"s":"x"}}';
  assert.equal(patchJsonHoles(raw), raw);
});

test("a realistic rws LaserScan frame round-trips", () => {
  const raw = '{"op":"publish","topic":"/scan","msg":{"header":{"frame_id":"base_laser"},"ranges":[2.48,,,,0.39,,1.1]}}';
  assert.deepEqual(parse(raw).msg.ranges, [2.48, null, null, null, 0.39, null, 1.1]);
});

console.log(`\n${passed} passed`);
