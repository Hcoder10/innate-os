// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// In-memory eval-session tally for the Profiling page: one entry per labeled
// rollout this sitting (discarded and profile-only runs don't count). Renders
// a ribbon of per-rollout chips plus summary numbers so a multi-rollout eval
// session reads as a running score, not disconnected runs. Ephemeral by
// design — the durable record is the "Policy Rollouts" dataset.

/**
 * @typedef {{ outcome: "success"|"failure", tags: string[], durationS: number,
 *   steps: number, p95Ms: number, maxProgress: number|null }} RolloutEntry
 */

export function createEvalSession() {
  /** @type {RolloutEntry[]} */
  const entries = [];

  const el = document.createElement("div");
  el.className = "prof-session";
  el.hidden = true;

  const tally = document.createElement("div");
  tally.className = "prof-session-tally microlabel";
  const ribbon = document.createElement("div");
  ribbon.className = "prof-session-ribbon";
  el.append(tally, ribbon);

  function render() {
    const n = entries.length;
    const ok = entries.filter((e) => e.outcome === "success").length;
    const rate = n ? Math.round((100 * ok) / n) : 0;
    tally.textContent = `Session · ${n} rollout${n === 1 ? "" : "s"} · ${ok}✓ ${n - ok}✗ · ${rate}% success`;
  }

  /** @param {RolloutEntry} entry */
  function add(entry) {
    entries.push(entry);
    el.hidden = false;

    const chip = document.createElement("span");
    const ok = entry.outcome === "success";
    chip.className = `prof-chip ${ok ? "ok" : "fail"}`;
    chip.textContent = `R${entries.length} ${ok ? "✓" : "✗"} · ${entry.p95Ms.toFixed(0)}ms`;
    // Full detail on hover; tags are also persisted on the episode itself
    // (SetEpisodeOutcome `tags`) — this chip is just the in-session view.
    chip.title =
      `${entry.durationS.toFixed(1)}s · ${entry.steps} steps · p95 ${entry.p95Ms.toFixed(1)} ms` +
      (entry.maxProgress != null ? ` · max progress ${entry.maxProgress.toFixed(2)}` : "") +
      (entry.tags.length ? `\n${entry.tags.join(", ")}` : "");
    ribbon.appendChild(chip);
    render();
  }

  return { el, add };
}
