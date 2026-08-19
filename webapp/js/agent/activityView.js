// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc

export const COMPACT_ACTIVITY_KEY = "innate.agentActivityCompact";
export const SKILL_OUTPUT_LINK_WINDOW_SEC = 5;

/**
 * Compact is the onboarding default. A saved explicit opt-out restores the
 * fuller debugging stream; unavailable storage must never break the panel.
 * @param {{ getItem: (key: string) => string | null } | null | undefined} storage
 */
export function readCompactActivity(storage) {
  try {
    const saved = storage?.getItem(COMPACT_ACTIVITY_KEY);
    return saved === null || saved === undefined ? true : saved !== "false";
  } catch {
    return true;
  }
}

/** @param {{ setItem: (key: string, value: string) => void } | null | undefined} storage @param {boolean} compact */
export function writeCompactActivity(storage, compact) {
  try {
    storage?.setItem(COMPACT_ACTIVITY_KEY, String(compact));
  } catch {
    // Private browsing / disabled storage: keep the in-memory toggle working.
  }
}

/**
 * Status and chat_out are separate ROS topics, so allow a small amount of
 * cross-topic reordering while refusing to attach a late output to a later run.
 * @param {number} firstTs @param {number} secondTs @param {number} [windowSec]
 */
export function skillEventsAreAdjacent(firstTs, secondTs, windowSec = SKILL_OUTPUT_LINK_WINDOW_SEC) {
  if (!Number.isFinite(firstTs) || !Number.isFinite(secondTs)) return false;
  const delta = secondTs - firstTs;
  return delta >= -1 && delta <= windowSec;
}

/**
 * Find an exact-text legacy/status pair without guessing based on whichever
 * skill happened to finish most recently.
 * @param {{ text: string, ts: number }[]} candidates
 * @param {string} text
 * @param {number} ts
 * @param {boolean} [candidateIsFirst]
 */
export function findMatchingOutputIndex(candidates, text, ts, candidateIsFirst = true) {
  return candidates.findIndex(
    (candidate) =>
      candidate.text === text &&
      (candidateIsFirst
        ? skillEventsAreAdjacent(candidate.ts, ts)
        : skillEventsAreAdjacent(ts, candidate.ts)),
  );
}
