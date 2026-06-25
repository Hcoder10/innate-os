// Gating-logic tests for js/appPromo.js — zero dependencies, plain node:
//   node tests/appPromo.test.js
// Only detectMobilePlatform() is exercised here: it's pure (reads navigator)
// and is what decides whether the prompt ever appears.

import assert from "node:assert/strict";
import { detectMobilePlatform } from "../js/appPromo.js";

let passed = 0;
/** @param {string} name @param {() => void} fn */
function test(name, fn) {
  fn();
  passed += 1;
  console.log(`ok - ${name}`);
}

/** Stub the bits of navigator the detector reads (the global is read-only in Node). */
function setNavigator(ua, maxTouchPoints = 0) {
  Object.defineProperty(globalThis, "navigator", {
    value: { userAgent: ua, maxTouchPoints },
    configurable: true,
  });
}

const IPHONE =
  "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1";
const ANDROID =
  "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36";
const MAC =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15";

test("iPhone → ios", () => {
  setNavigator(IPHONE);
  assert.equal(detectMobilePlatform(), "ios");
});

test("Android phone → android", () => {
  setNavigator(ANDROID);
  assert.equal(detectMobilePlatform(), "android");
});

test("desktop Mac → null (no prompt)", () => {
  setNavigator(MAC, 0);
  assert.equal(detectMobilePlatform(), null);
});

test("iPadOS (Mac UA + touch) → ios", () => {
  // iPadOS Safari masquerades as a Mac; the touch screen is the tell.
  setNavigator(MAC, 5);
  assert.equal(detectMobilePlatform(), "ios");
});

console.log(`\n${passed} passed`);
