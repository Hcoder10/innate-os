// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc

export const SKILL_OUTPUT_LINK_WINDOW_SEC = 5;

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
