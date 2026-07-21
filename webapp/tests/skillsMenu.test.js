// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Tests for the Skills menu's pure helpers — zero dependencies, plain node:
//   node tests/skillsMenu.test.js
// favoriteSkills is the curated-view rule (the user's robot-persisted stars);
// prettify/formatName are the row labels, which must never show the roster's
// source-prefix paths (innate-os/, local/) to an operator.

import assert from "node:assert/strict";
import { favoriteSkills, formatName, prettify } from "../js/teleop/skillsMenu.js";

let passed = 0;
/** @param {string} name @param {() => void} fn */
function test(name, fn) {
  fn();
  passed += 1;
  console.log(`ok - ${name}`);
}

const ROSTER = [
  { id: "innate-os/wave", name: "Wave" },
  { id: "innate-os/send_email", name: "Send Email" },
  { id: "local/my_trick", name: "My Trick" },
];

test("no stars yet → the whole roster shows (first-run)", () => {
  assert.deepEqual(favoriteSkills(ROSTER, new Set()), ROSTER);
});

test("stars filter the view to exactly the starred ids", () => {
  const favs = new Set(["innate-os/wave", "local/my_trick"]);
  assert.deepEqual(
    favoriteSkills(ROSTER, favs).map((s) => s.id),
    ["innate-os/wave", "local/my_trick"],
  );
});

test("a favorite whose skill left the roster is simply not shown", () => {
  const favs = new Set(["innate-os/gone_skill"]);
  assert.deepEqual(favoriteSkills(ROSTER, favs), []);
});

test("roster order is preserved, not favorites order", () => {
  const favs = new Set(["local/my_trick", "innate-os/wave"]);
  assert.deepEqual(
    favoriteSkills(ROSTER, favs).map((s) => s.id),
    ["innate-os/wave", "local/my_trick"],
  );
});

test("prettify hides the source prefix and title-cases the stem", () => {
  assert.equal(prettify("innate-os/navigate_to_position"), "Navigate To Position");
  assert.equal(prettify("local/my-custom-trick"), "My Custom Trick");
  assert.equal(prettify("wave"), "Wave");
});

test("formatName prefers the display name, falls back to the prettified id", () => {
  assert.equal(formatName({ id: "innate-os/wave", name: "Wave!" }), "Wave!");
  assert.equal(formatName({ id: "innate-os/pick_socks" }), "Pick Socks");
});

console.log(`\n${passed} passed`);
