// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Tests for the cockpit menu's curation rule — zero dependencies, plain node:
//   node tests/skillVisibility.test.js
// The Skills menu shows a curated view (user skills + DEFAULT_COCKPIT_SKILLS +
// the active directive's skills) instead of the full roster; isCockpitSkill is
// the pure rule behind it.

import assert from "node:assert/strict";
import { readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { DEFAULT_COCKPIT_SKILLS } from "../js/constants.js";
import { isCockpitSkill } from "../js/teleop/skillVisibility.js";

let passed = 0;
/** @param {string} name @param {() => void} fn */
function test(name, fn) {
  fn();
  passed += 1;
  console.log(`ok - ${name}`);
}

const NONE = new Set();

test("user (local/) skills always show", () => {
  assert.equal(isCockpitSkill({ id: "local/my-replay" }, [], NONE), true);
  assert.equal(isCockpitSkill({ id: "local/pick_socks" }, DEFAULT_COCKPIT_SKILLS, NONE), true);
});

test("curated shipped skills show; uncurated ones don't", () => {
  assert.equal(isCockpitSkill({ id: "innate-os/navigate_to_position" }, DEFAULT_COCKPIT_SKILLS, NONE), true);
  assert.equal(isCockpitSkill({ id: "innate-os/update_chess_state" }, DEFAULT_COCKPIT_SKILLS, NONE), false);
  assert.equal(isCockpitSkill({ id: "innate-os/send_email" }, DEFAULT_COCKPIT_SKILLS, NONE), false);
});

test("the active directive's skills join the view", () => {
  const chess = new Set(["innate-os/update_chess_state", "innate-os/reset_chess_game"]);
  assert.equal(isCockpitSkill({ id: "innate-os/update_chess_state" }, DEFAULT_COCKPIT_SKILLS, chess), true);
  // ...without dragging in unrelated uncurated skills.
  assert.equal(isCockpitSkill({ id: "innate-os/send_email" }, DEFAULT_COCKPIT_SKILLS, chess), false);
});

test("matches are exact ids, not prefixes or display names", () => {
  assert.equal(isCockpitSkill({ id: "innate-os/wave_hello" }, DEFAULT_COCKPIT_SKILLS, NONE), false);
  assert.equal(isCockpitSkill({ id: "wave" }, DEFAULT_COCKPIT_SKILLS, NONE), false);
});

test("malformed roster entries are hidden, not crashing", () => {
  assert.equal(isCockpitSkill({}, DEFAULT_COCKPIT_SKILLS, NONE), false);
  assert.equal(isCockpitSkill({ id: undefined }, DEFAULT_COCKPIT_SKILLS, NONE), false);
});

test("every DEFAULT_COCKPIT_SKILLS entry is a fully-qualified shipped id", () => {
  for (const id of DEFAULT_COCKPIT_SKILLS) {
    assert.match(id, /^innate-os\/[a-z0-9_]+$/, `unexpected id format: ${id}`);
  }
});

test("every DEFAULT_COCKPIT_SKILLS entry exists in workspace/innate_skills", () => {
  // Shipped ids are innate-os/<py stem or skill-dir name> (catalog._compute_skill_id),
  // so a rename/removal there must fail here instead of silently thinning the menu.
  const skillsDir = fileURLToPath(new URL("../../workspace/innate_skills/", import.meta.url));
  const stems = new Set(readdirSync(skillsDir).map((entry) => entry.replace(/\.py$/, "")));
  for (const id of DEFAULT_COCKPIT_SKILLS) {
    assert.ok(stems.has(id.split("/")[1]), `${id} has no source in workspace/innate_skills/`);
  }
});

console.log(`\n${passed} passed`);
