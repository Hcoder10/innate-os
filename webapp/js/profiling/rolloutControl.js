// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Unified rollout lifecycle for the Profiling page. One press replaces the
// old three independent toggles (skill Run/Stop, chart Record, episode
// Capture/Save/label): the rollout IS the record window.
//
//   ready → starting → running → review → saving → ready   (Run rollout)
//   ready → running → ready                                (Profile only)
//
// "Run rollout" starts the skill + the live charts + robot-side episode
// capture together; the run ends on Stop, the learned auto-stop, or policy
// completion (all resolve the same action promise). Eval runs then land in
// Review — the episode is still open on the recorder, so ✓/✗ is save+label
// and Discard is a free cancel. Profile only skips capture and Review.
//
// Review also carries optional failure-mode tags. On save they are persisted
// per-episode via SetEpisodeOutcome's `tags` field (alongside the outcome), in
// addition to the session tally.

import { ros } from "../rosClient.js";
import { AVAILABLE_SKILLS_TOPIC, EXECUTE_SKILL_ACTION, EXECUTE_SKILL_ACTION_TYPE } from "../constants.js";
import { createEpisodeCapture } from "./episodeCapture.js";

const FAILURE_TAGS = ["missed grasp", "wrong object", "dropped", "collision", "no progress", "timeout"];
const AUTO_CONTINUE_DELAY_S = 5;

/**
 * @param {{ begin: () => void, end: () => void, samples: () => any[] }} chartWindow
 *   handle over the page's sample buffer: begin() clears + starts charting,
 *   end() freezes the window, samples() reads the frozen run.
 * @param {{ add: (entry: any) => void }} session eval-session tally
 */
export function buildRolloutControl(chartWindow, session) {
  const capture = createEpisodeCapture();

  // ---- header controls ------------------------------------------------------
  const el = document.createElement("span");
  el.className = "prof-rollout";

  const select = document.createElement("select");
  select.className = "prof-select";
  const placeholder = new Option("Select a learned skill…", "");
  select.add(placeholder);
  select.addEventListener("change", () => sync());

  const runBtn = document.createElement("button");
  runBtn.className = "prof-btn prof-btn-rec";
  const profileBtn = document.createElement("button");
  profileBtn.className = "prof-btn prof-btn-quiet";
  profileBtn.textContent = "Profile only";

  const autoLabel = document.createElement("label");
  autoLabel.className = "prof-auto microlabel";
  const autoBox = document.createElement("input");
  autoBox.type = "checkbox";
  autoLabel.append(autoBox, document.createTextNode("auto-continue"));

  el.append(select, runBtn, profileBtn, autoLabel);

  // ---- review bar -----------------------------------------------------------
  const reviewEl = document.createElement("div");
  reviewEl.className = "prof-review";
  reviewEl.hidden = true;

  const reviewText = document.createElement("span");
  reviewText.className = "prof-review-text";

  const tagRow = document.createElement("span");
  tagRow.className = "prof-tags";
  /** @type {Set<string>} */
  const pickedTags = new Set();
  for (const tag of FAILURE_TAGS) {
    const chip = document.createElement("button");
    chip.className = "prof-tag";
    chip.textContent = tag;
    chip.addEventListener("click", () => {
      if (pickedTags.has(tag)) pickedTags.delete(tag);
      else pickedTags.add(tag);
      chip.classList.toggle("active", pickedTags.has(tag));
    });
    tagRow.appendChild(chip);
  }

  const okBtn = document.createElement("button");
  okBtn.className = "prof-btn prof-btn-ok";
  okBtn.textContent = "✓ Success";
  const failBtn = document.createElement("button");
  failBtn.className = "prof-btn prof-btn-fail";
  failBtn.textContent = "✗ Failure";
  const discardBtn = document.createElement("button");
  discardBtn.className = "prof-btn";
  discardBtn.textContent = "Discard";

  reviewEl.append(reviewText, tagRow, okBtn, failBtn, discardBtn);

  // ---- error line -------------------------------------------------------------
  // Failures surface here, not just in the devtools console: at the robot the
  // operator sees the actual message (a service-call timeout names the backend
  // node to look at), and it stays up until the next run instead of flashing.
  const errorEl = document.createElement("div");
  errorEl.className = "prof-error";
  errorEl.hidden = true;

  /** @param {any} err */
  function showError(err) {
    const msg = err?.message || String(err);
    errorEl.textContent = autoBox.checked ? `${msg} — auto-continue paused` : msg;
    errorEl.hidden = false;
  }

  function hideError() {
    errorEl.hidden = true;
  }

  // ---- state ------------------------------------------------------------------
  /** @type {"ready"|"starting"|"running"|"review"|"saving"} */
  let state = "ready";
  /** @type {"eval"|"profile"} */
  let mode = "eval";
  let busy = false;
  /** @type {(() => void) | null} */
  let cancelSkill = null;
  /** @type {ReturnType<typeof summarize> | null} */
  let reviewStats = null;
  let runStartedAt = 0;
  /** @type {number | null} */
  let autoTimer = null;
  let destroyed = false;

  const unsubSkills = ros.subscribe(AVAILABLE_SKILLS_TOPIC, (msg) => {
    const all = Array.isArray(msg?.skills) ? msg.skills : [];
    const learned = all.filter((s) => s?.id && s.type === "learned" && !s.in_training);
    const prevValue = select.value;
    select.replaceChildren(placeholder);
    for (const s of learned) select.add(new Option(s.name || s.id, s.id));
    select.value = learned.some((s) => s.id === prevValue) ? prevValue : "";
    sync();
  });

  function sync(flash) {
    const running = state === "starting" || state === "running";
    runBtn.textContent =
      flash ??
      { ready: "▶ Run rollout", starting: "Starting…", running: "■ Stop", review: "▶ Run rollout", saving: "▶ Run rollout" }[state];
    runBtn.classList.toggle("active", running);
    runBtn.disabled =
      state === "starting" || state === "saving" || state === "review" ||
      (state === "ready" && !select.value && !flash);
    profileBtn.hidden = state !== "ready";
    profileBtn.disabled = !select.value;
    select.disabled = state !== "ready";
    reviewEl.hidden = state !== "review" && state !== "saving";
    okBtn.disabled = failBtn.disabled = discardBtn.disabled = state === "saving";
  }

  function toReady(flash) {
    state = "ready";
    sync(flash);
    if (flash) setTimeout(() => sync(), 1600);
  }

  /** One failure funnel: whatever step died, don't leave a half-open episode.
   * Auto-continue never re-arms after a failure — the countdown is only armed
   * from a successful save — so an error stops the loop by construction. */
  function onError(err) {
    console.error("[profiling] rollout:", err);
    if (cancelSkill) {
      cancelSkill();
      cancelSkill = null;
    }
    chartWindow.end();
    if (mode === "eval") capture.discard();
    if (destroyed) return; // page gone — cleanup done, skip the UI
    showError(err);
    toReady("Failed");
  }

  /** @param {(() => Promise<void>) | (() => void)} fn */
  async function guarded(fn) {
    if (busy) return;
    busy = true;
    try {
      await fn();
    } catch (err) {
      onError(err);
    } finally {
      busy = false;
    }
  }

  /** @param {"eval"|"profile"} m */
  async function runRollout(m) {
    mode = m;
    hideError();
    // Read the picked skill once: the skills-topic handler rebuilds the select
    // (and can clear its value) at any time, so a second read after the await
    // below could stamp the episode with one id and run a different skill.
    const skillId = select.value;
    if (mode === "eval") {
      state = "starting";
      sync();
      // Stamp the episode with the evaluated skill's id as its driving policy.
      await capture.start(skillId);
      if (destroyed) {
        // Torn down while start was in flight: destroy()'s discard may have
        // raced the episode-open service call, so discard again now that the
        // episode actually exists — and never send the goal.
        capture.discard();
        return;
      }
    }
    chartWindow.begin();
    runStartedAt = performance.now();
    const { promise, cancel } = ros.sendActionGoal(EXECUTE_SKILL_ACTION, EXECUTE_SKILL_ACTION_TYPE, {
      skill_type: skillId,
      inputs: "{}",
    });
    cancelSkill = cancel;
    state = "running";
    sync();
    // Operator Stop, the learned auto-stop, normal completion, and a rejected
    // goal all end the run — settle either way lands in onEnded.
    promise.then(onEnded, onEnded);
  }

  function onEnded() {
    // .then fires a microtask after state="running" above, so the run is
    // always "running" here unless we've torn down or already handled it.
    if (destroyed || state !== "running") return;
    cancelSkill = null;
    chartWindow.end();
    if (mode === "profile") {
      toReady("Profiled ✓");
      return;
    }
    reviewStats = summarize(chartWindow.samples(), (performance.now() - runStartedAt) / 1000);
    reviewText.textContent =
      `Rollout ended · ${reviewStats.durationS.toFixed(1)}s · ${reviewStats.steps} steps · ` +
      `p95 ${reviewStats.p95Ms.toFixed(0)}ms` +
      (reviewStats.maxProgress != null ? ` · progress ${reviewStats.maxProgress.toFixed(2)}` : "") +
      " — keep it?";
    pickedTags.clear();
    tagRow.querySelectorAll(".prof-tag").forEach((c) => c.classList.remove("active"));
    state = "review";
    sync();
  }

  /** @param {"success"|"failure"} outcome */
  async function keep(outcome) {
    state = "saving";
    sync();
    await capture.save();
    await capture.labelLast(outcome, [...pickedTags]);
    if (reviewStats) session.add({ outcome, tags: [...pickedTags], ...reviewStats });
    if (autoBox.checked && select.value) {
      // No flash: its 1.6s reset would clobber the countdown text.
      toReady();
      armAutoContinue();
    } else {
      toReady(outcome === "success" ? "Saved ✓" : "Saved ✗");
    }
  }

  function armAutoContinue() {
    let left = AUTO_CONTINUE_DELAY_S;
    runBtn.textContent = `Next in ${left}s — cancel`;
    autoTimer = window.setInterval(() => {
      left -= 1;
      if (left > 0) {
        runBtn.textContent = `Next in ${left}s — cancel`;
        return;
      }
      clearAutoContinue();
      guarded(() => runRollout("eval"));
    }, 1000);
  }

  function clearAutoContinue() {
    if (autoTimer != null) {
      clearInterval(autoTimer);
      autoTimer = null;
    }
  }

  runBtn.addEventListener("click", () =>
    guarded(() => {
      if (autoTimer != null) {
        // Countdown showing — the press means "don't start the next one".
        clearAutoContinue();
        sync();
        return;
      }
      if (state === "running" && cancelSkill) {
        cancelSkill(); // resolves the action promise → onEnded
        return;
      }
      if (state === "ready" && select.value) return runRollout("eval");
    })
  );
  profileBtn.addEventListener("click", () =>
    guarded(() => {
      if (state === "ready" && select.value) return runRollout("profile");
    })
  );
  okBtn.addEventListener("click", () => guarded(() => keep("success")));
  failBtn.addEventListener("click", () => guarded(() => keep("failure")));
  discardBtn.addEventListener("click", () =>
    guarded(async () => {
      await capture.discard();
      toReady("Discarded");
    })
  );

  sync();

  return {
    el,
    reviewEl,
    errorEl,
    destroy() {
      destroyed = true;
      unsubSkills();
      clearAutoContinue();
      if (cancelSkill) cancelSkill(); // never leave a skill running headless
      // Mid-run partials are not valid evals — drop them. A completed run
      // sitting in Review is real data — save it unlabeled; the Datasets
      // page can set the outcome later.
      if (state === "starting" || state === "running") {
        if (mode === "eval") capture.discard();
      } else if (state === "review") {
        capture.save().catch(() => {});
      }
    },
  };
}

/**
 * Reduce a run's samples to the numbers Review and the session tally show.
 * @param {any[]} samples @param {number} wallS fallback duration
 */
function summarize(samples, wallS) {
  const totals = samples.map((s) => s.total_ms).sort((a, b) => a - b);
  const p95 = totals.length ? totals[Math.min(totals.length - 1, Math.floor(0.95 * totals.length))] : 0;
  const progress = samples.map((s) => s.progress).filter((v) => typeof v === "number");
  const span = samples.length >= 2 ? samples[samples.length - 1].t - samples[0].t : wallS;
  return {
    durationS: span > 0 ? span : wallS,
    steps: samples.length,
    p95Ms: p95,
    maxProgress: progress.length ? Math.max(...progress) : null,
  };
}
