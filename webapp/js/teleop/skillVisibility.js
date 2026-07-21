// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Which skills the cockpit Skills menu shows by default. Pure — the menu owns
// the policy ("exposure is the consumer's list"), this module owns the rule so
// it can be unit-tested without a DOM (see webapp/test/skillVisibility.test.mjs).

/**
 * True if a skill belongs in the cockpit menu's default (non-"Show all") view:
 * user-created skills always, shipped skills only when curated in the default
 * list or granted by the active directive.
 *
 * @param {{ id?: string }} skill Roster entry (matched on its exact id).
 * @param {readonly string[]} defaultIds Shipped skill ids curated for the cockpit.
 * @param {ReadonlySet<string>} agentSkillIds Active directive's skill ids ("" set when idle).
 */
export function isCockpitSkill(skill, defaultIds, agentSkillIds) {
  const id = String(skill?.id ?? "");
  return id.startsWith("local/") || defaultIds.includes(id) || agentSkillIds.has(id);
}
