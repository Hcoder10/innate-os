// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Tests for js/clipboard.js — zero dependencies, plain node:
//   node tests/clipboard.test.js
// navigator and document are stubbed so the legacy textarea + execCommand
// fallback — the branch that only runs on insecure origins (plain HTTP),
// which local HTTPS testing never exercises — is covered too.

import assert from "node:assert/strict";
import { copyText } from "../js/clipboard.js";

let passed = 0;
/** @param {string} name @param {() => void | Promise<void>} fn */
async function test(name, fn) {
  await fn();
  passed += 1;
  console.log(`ok - ${name}`);
}

/** @param {any} value */
function setGlobal(name, value) {
  // Plain assignment fails for node's own `navigator` getter; defineProperty works.
  Object.defineProperty(globalThis, name, { value, configurable: true });
}

/**
 * Install a stub DOM and return handles for asserting on the fallback path.
 * @param {{ execResult?: boolean }} [opts]
 */
function installDom({ execResult = true } = {}) {
  const state = {
    /** @type {any[]} */ appended: [],
    /** @type {string[]} */ execCalls: [],
    prevFocusCalls: 0,
  };
  setGlobal("document", {
    activeElement: { focus: () => state.prevFocusCalls++ },
    body: { appendChild: (/** @type {any} */ el) => state.appended.push(el) },
    createElement: (/** @type {string} */ tag) => ({
      tag,
      value: "",
      style: {},
      /** @type {Record<string, string>} */ attrs: {},
      setAttribute(/** @type {string} */ k, /** @type {string} */ v) { this.attrs[k] = v; },
      selected: false,
      select() { this.selected = true; },
      /** @type {number[] | null} */ range: null,
      setSelectionRange(/** @type {number} */ a, /** @type {number} */ b) { this.range = [a, b]; },
      removed: false,
      remove() { this.removed = true; },
    }),
    execCommand: (/** @type {string} */ cmd) => { state.execCalls.push(cmd); return execResult; },
    getSelection: () => null,
  });
  return state;
}

await test("uses navigator.clipboard.writeText when available", async () => {
  const dom = installDom();
  /** @type {string[]} */ const written = [];
  setGlobal("navigator", { clipboard: { writeText: async (/** @type {string} */ t) => { written.push(t); } } });
  await copyText("hello");
  assert.deepEqual(written, ["hello"]);
  assert.equal(dom.appended.length, 0); // no fallback textarea
});

await test("falls back to execCommand when navigator.clipboard is undefined", async () => {
  const dom = installDom();
  setGlobal("navigator", {});
  await copyText("log line 1\nlog line 2");
  assert.equal(dom.appended.length, 1);
  const ta = dom.appended[0];
  assert.equal(ta.value, "log line 1\nlog line 2");
  assert.equal(ta.attrs.readonly, ""); // no soft keyboard on touch devices
  assert.equal(ta.style.position, "fixed");
  assert.equal(ta.style.top, "0"); // pinned in-viewport, not at static position
  assert.ok(ta.selected);
  assert.deepEqual(ta.range, [0, "log line 1\nlog line 2".length]); // iOS needs setSelectionRange
  assert.deepEqual(dom.execCalls, ["copy"]);
  assert.ok(ta.removed);
  assert.equal(dom.prevFocusCalls, 1); // focus handed back to the copy button
});

await test("falls back when writeText rejects (e.g. NotAllowedError)", async () => {
  const dom = installDom();
  setGlobal("navigator", { clipboard: { writeText: async () => { throw new Error("NotAllowedError"); } } });
  await copyText("x");
  assert.deepEqual(dom.execCalls, ["copy"]);
});

await test("falls back when clipboard exists but writeText is missing", async () => {
  const dom = installDom();
  setGlobal("navigator", { clipboard: {} }); // old WebViews
  await copyText("x");
  assert.deepEqual(dom.execCalls, ["copy"]);
});

await test("rejects when execCommand fails, still cleaning up", async () => {
  const dom = installDom({ execResult: false });
  setGlobal("navigator", {});
  await assert.rejects(() => copyText("x"), /execCommand copy failed/);
  assert.ok(dom.appended[0].removed);
  assert.equal(dom.prevFocusCalls, 1);
});

console.log(`\n${passed} tests passed`);
