// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Agent-activity helpers — zero dependencies, plain node:
//   node tests/activityView.test.js

import assert from "node:assert/strict";
import {
  findMatchingOutputIndex,
  skillEventsAreAdjacent,
} from "../js/agent/activityView.js";

let passed = 0;
/** @param {string} name @param {() => void} fn */
function test(name, fn) {
  fn();
  passed += 1;
  console.log(`ok - ${name}`);
}

test("nearby cross-topic events pair despite slight reordering", () => {
  assert.equal(skillEventsAreAdjacent(100, 104.9), true);
  assert.equal(skillEventsAreAdjacent(100, 99.2), true);
});

test("late or invalid events cannot attach to an old skill run", () => {
  assert.equal(skillEventsAreAdjacent(100, 105.1), false);
  assert.equal(skillEventsAreAdjacent(100, 98.9), false);
  assert.equal(skillEventsAreAdjacent(Number.NaN, 100), false);
});

test("fast outputs match by exact text instead of the latest completed call", () => {
  const completed = [
    { text: "output A", ts: 100 },
    { text: "output B", ts: 100.1 },
  ];
  assert.equal(findMatchingOutputIndex(completed, "output A", 100.2), 0);
  assert.equal(findMatchingOutputIndex(completed, "output B", 100.3), 1);
  assert.equal(findMatchingOutputIndex(completed, "unknown", 100.4), -1);
});

test("an early legacy output can be reconciled when its status follows", () => {
  const orphans = [{ text: "found cup", ts: 99.5 }];
  assert.equal(findMatchingOutputIndex(orphans, "found cup", 100, false), 0);
  assert.equal(findMatchingOutputIndex(orphans, "found cup", 101, false), -1);
});

console.log(`\n${passed} passed`);
