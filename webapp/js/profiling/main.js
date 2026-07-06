// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Profiling page entry — run, watch, and judge learned-skill rollouts.
//
// The manipulation server publishes a per-step timing breakdown on
// INFERENCE_PROFILE_TOPIC (std_msgs/String → JSON) while a learned behavior
// runs. This page subscribes and charts it live, but the lifecycle belongs to
// rolloutControl: one "Run rollout" starts the skill + these charts + robot-
// side episode capture together, and ending the run lands in a ✓/✗ review.
// The chart window opens on run start and freezes on run end — there is no
// separate Record toggle.

import { ros } from "../rosClient.js";
import { mountPage } from "../pageMount.js";
import { INFERENCE_PROFILE_TOPIC } from "../constants.js";
import { buildRolloutControl } from "./rolloutControl.js";
import { createEvalSession } from "./evalSession.js";
import { buildRebootArm } from "./rebootArm.js";

const SVG_NS = "http://www.w3.org/2000/svg";
const MAX_PLOT_POINTS = 400; // timeseries window; stats use the full retained record
// Cap the retained record so an unattended recording can't grow without bound. At the
// 25 Hz inference rate this is ~20 min of history — well past any real profiling session —
// and it bounds the per-tick computeStats/histogram sort cost, not just memory. Older
// samples roll off; stats and the export reflect the most recent MAX_SAMPLES.
const MAX_SAMPLES = 30000;

// The auto-stop idle detector fires when arm_jerk and base_speed both hold below
// their eps for idle_seconds. These mirror the LearnedStopDetector defaults
// (idle_arm_eps / idle_base_eps) so the stillness stats and the eps guide lines on
// the motion charts measure the same thing the detector checks.
// Declared before mount() below: buildView runs synchronously inside mountPage
// and reads these, so a later declaration would be a TDZ ReferenceError.
const STILL_ARM_EPS = 0.01;
const STILL_BASE_EPS = 0.01;

/** @param {HTMLElement} stage */
export function mount(stage) {
  return mountPage(stage, "profiling-page", buildView);
}

/**
 * @typedef {{ seq:number, t:number, preprocess_ms:number, inference_ms:number,
 *   engine_ms:number, transfer_ms:number, postprocess_ms:number, total_ms:number,
 *   engine_ran:boolean, period_ms:number, progress?:number, disagreement?:number,
 *   arm_jerk?:number, base_jerk?:number, base_speed?:number }} Sample
 */

/**
 * @param {HTMLElement} root
 * @returns {{ destroy: () => void }}
 */
function buildView(root) {
  /** @type {Sample[]} */
  let samples = [];
  let windowOpen = false; // a rollout is in flight — buffer incoming samples
  let lastMsgAt = 0; // wall-clock of last received message, for the live dot
  let dirty = false;

  // ---- header -------------------------------------------------------------
  const head = document.createElement("div");
  head.className = "page-head";
  const title = document.createElement("h1");
  title.className = "page-title";
  title.textContent = "Profiling";

  const sub = document.createElement("span");
  sub.className = "prof-sub microlabel";
  sub.textContent = "ACT inference";

  const live = document.createElement("span");
  live.className = "prof-live";
  live.innerHTML = '<span class="prof-live-dot"></span><span class="prof-live-text">no signal</span>';

  const spacer = document.createElement("div");
  spacer.style.flex = "1";

  const exportBtn = document.createElement("button");
  exportBtn.className = "prof-btn";
  exportBtn.textContent = "Export JSON";
  const clearBtn = document.createElement("button");
  clearBtn.className = "prof-btn";
  clearBtn.textContent = "Clear";

  // The whole rollout lifecycle (skill run + this chart window + robot-side
  // episode capture) hangs off one control; the page just lends it the buffer.
  const chartWindow = {
    begin() {
      samples = [];
      windowOpen = true;
      dirty = true;
    },
    end() {
      windowOpen = false;
      dirty = true;
    },
    samples: () => samples,
  };
  const session = createEvalSession();
  const rollout = buildRolloutControl(chartWindow, session);
  // Arm recovery — available regardless of rollout state (an overload can latch
  // the servos off mid-run); orthogonal to the run lifecycle, so it lives here.
  const reboot = buildRebootArm();

  head.append(title, sub, live, spacer, rollout.el, reboot.el, exportBtn, clearBtn);

  // ---- body ---------------------------------------------------------------
  const body = document.createElement("div");
  body.className = "prof-body";

  const statsRow = document.createElement("div");
  statsRow.className = "prof-stats";

  const charts = document.createElement("div");
  charts.className = "prof-charts";
  const cards = {
    ts: chartCard("Total step latency vs 25 Hz budget", "ms over recorded steps"),
    hist: chartCard("Model inference time", "engine-run steps only"),
    breakdown: chartCard("Average per-step breakdown", "where each step spends time"),
    // Behavior quality: how well / how confident the policy is, vs just how fast.
    progress: chartCard("Progress", "policy's learned progress head (action[8])"),
    disagreement: chartCard("Ensemble disagreement", "uncertainty — spikes precede failures"),
    // Auto-stop signals: what the idle detector watches, with its eps as a guide line.
    armMotion: chartCard("Arm motion", `‖Δ arm cmd‖ per step — idle stop needs < ${STILL_ARM_EPS}`),
    baseSpeed: chartCard("Base speed", `commanded |lin|+|ang| — idle stop needs < ${STILL_BASE_EPS}`),
  };
  for (const c of Object.values(cards)) charts.append(c.card);

  const hint = document.createElement("p");
  hint.className = "prof-hint microlabel";
  hint.textContent =
    "Pick a learned skill, then Run rollout to drive it with charts live and save it as a labeled " +
    "eval episode (video + trajectory + profile trace, in the Policy Rollouts dataset) — or " +
    "Profile only to watch the charts without recording anything on the robot.";

  body.append(rollout.reviewEl, session.el, statsRow, charts, hint);
  root.append(head, body);

  // ---- controls -----------------------------------------------------------
  clearBtn.addEventListener("click", () => {
    samples = [];
    dirty = true;
  });
  exportBtn.addEventListener("click", () => exportJson(samples, exportBtn));

  // ---- subscription -------------------------------------------------------
  const unsubscribe = ros.subscribe(INFERENCE_PROFILE_TOPIC, (msg) => {
    lastMsgAt = performance.now();
    if (!windowOpen) return;
    const s = parseSample(msg?.data);
    if (s) {
      samples.push(s);
      if (samples.length > MAX_SAMPLES) samples.shift();
      dirty = true;
    }
  });

  // ---- render loop --------------------------------------------------------
  // Throttle to ~7 Hz: SVG re-render on every 25 Hz sample is wasteful and the
  // eye can't read faster anyway. The live dot updates on the same tick.
  const timer = setInterval(() => {
    const sinceMsg = performance.now() - lastMsgAt;
    const flowing = lastMsgAt > 0 && sinceMsg < 500;
    setLive(live, flowing, windowOpen);
    if (dirty) {
      render(samples, statsRow, cards);
      dirty = false;
    }
  }, 140);

  render(samples, statsRow, cards);

  return {
    destroy() {
      clearInterval(timer);
      unsubscribe();
      rollout.destroy();
      reboot.destroy();
    },
  };
}

// Timing fields the page reads unguarded in computeStats; a dropped or renamed one
// would propagate undefined/NaN into mean/percentile. Quality fields are optional and
// filtered downstream, so they're not required here.
const REQUIRED_NUMERIC = [
  "seq", "t", "preprocess_ms", "inference_ms", "engine_ms",
  "transfer_ms", "postprocess_ms", "total_ms", "period_ms",
];

/** @param {any} data @returns {Sample | null} */
function parseSample(data) {
  if (typeof data !== "string") return null;
  let s;
  try {
    s = JSON.parse(data);
  } catch {
    return null;
  }
  // Reject the whole sample unless every required field is present and the right type,
  // rather than letting a missing value silently become NaN in the stats.
  if (typeof s.engine_ran !== "boolean") return null;
  for (const f of REQUIRED_NUMERIC) {
    if (typeof s[f] !== "number" || !isFinite(s[f])) return null;
  }
  return s;
}

/** @param {HTMLElement} live @param {boolean} flowing @param {boolean} windowOpen */
function setLive(live, flowing, windowOpen) {
  const dot = live.querySelector(".prof-live-dot");
  const text = live.querySelector(".prof-live-text");
  if (!dot || !text) return;
  const state = !flowing ? "idle" : windowOpen ? "rec" : "live";
  live.dataset.state = state;
  text.textContent = state === "idle" ? "no signal" : state === "rec" ? "recording" : "receiving";
}

// ---- export ---------------------------------------------------------------

/**
 * Build a self-describing JSON profile and hand it off — copied to the clipboard
 * for pasting into an LLM, with a file download as fallback. All times in ms.
 * @param {Sample[]} samples
 * @param {HTMLButtonElement} btn
 */
async function exportJson(samples, btn) {
  if (!samples.length) return flashBtn(btn, "No data", "Export JSON");
  const st = computeStats(samples);
  const r = (x, d = 2) => Math.round(x * 10 ** d) / 10 ** d;
  const rn = (x, d = 2) => (x == null ? null : r(x, d)); // null-safe (quality may be absent)
  const profile = {
    generated_at: new Date().toISOString(),
    source: "Innate webapp · ACT inference profiler",
    topic: INFERENCE_PROFILE_TOPIC,
    schema:
      "ACT policy inference timing. All values in milliseconds unless noted. " +
      "budget_ms is the inference period target (1000/target_hz). breakdown_ms holds " +
      "per-step means for the pipeline stages: preprocess (image resize/normalize/to-GPU), " +
      "engine (pure GPU model forward, CUDA-event measured), overhead (input copies + " +
      "temporal-ensemble + Python, = inference − engine − transfer), transfer (final GPU→CPU " +
      "copy), postprocess (command publish). model_inference_ms is the full select_action+copy " +
      "for engine-run steps; engine_forward_ms is just the GPU forward. behavior_quality holds " +
      "policy signals (not timing): progress (learned progress head action[8]), disagreement " +
      "(ensemble uncertainty — weighted std across overlapping chunk predictions; spikes precede " +
      "failures/OOD), arm_jerk/base_jerk (per-step ‖Δcommand‖, lower=smoother), base_speed " +
      "(commanded |lin.x|+|ang.z|). auto_stop maps to the LearnedStopDetector knobs: " +
      "progress_max bounds progress_threshold; arm_jerk_floor_p5/base_speed_floor_p5 are the " +
      "quietest the motion signals get (idle_arm_eps/idle_base_eps must exceed them to fire); " +
      "still_longest_s/still_now_s are the longest and trailing runs with both signals below " +
      `the detector's default eps (${STILL_ARM_EPS}), to read against idle_seconds. ` +
      "samples is the raw record.",
    sample_count: st.sampleCount,
    budget_ms: r(st.budgetMs),
    summary: {
      effective_hz: r(st.effectiveHz),
      over_budget_pct: r(st.overBudgetPct, 1),
      headroom_ms: r(st.headroomMs),
      total_ms: { mean: r(st.total.mean), p50: r(st.total.p50), p95: r(st.total.p95), p99: r(st.total.p99), max: r(st.total.max) },
      model_inference_ms: { count: st.model.count, mean: r(st.model.mean), p95: r(st.model.p95) },
      engine_forward_ms: { mean: r(st.engine.mean), p50: r(st.engine.p50), p95: r(st.engine.p95) },
      breakdown_ms: {
        preprocess: r(st.breakdown.preprocess),
        engine: r(st.breakdown.engine),
        overhead: r(st.breakdown.overhead),
        transfer: r(st.breakdown.transfer),
        postprocess: r(st.breakdown.postprocess),
      },
      behavior_quality: {
        progress_last: rn(st.quality.progressLast, 4),
        disagreement_mean: rn(st.quality.disagreementMean, 4),
        disagreement_p95: rn(st.quality.disagreementP95, 4),
        arm_jerk_mean: rn(st.quality.armJerkMean, 4),
        base_jerk_mean: rn(st.quality.baseJerkMean, 4),
      },
      auto_stop: {
        progress_max: rn(st.quality.progressMax, 4),
        arm_jerk_floor_p5: rn(st.quality.armJerkFloor, 4),
        base_speed_floor_p5: rn(st.quality.baseSpeedFloor, 4),
        still_longest_s: rn(st.quality.stillLongest, 1),
        still_now_s: rn(st.quality.stillNow, 1),
      },
    },
    samples: samples.map((s) => ({
      seq: s.seq,
      t: s.t,
      preprocess_ms: r(s.preprocess_ms, 3),
      inference_ms: r(s.inference_ms, 3),
      engine_ms: r(s.engine_ms, 3),
      transfer_ms: r(s.transfer_ms, 3),
      postprocess_ms: r(s.postprocess_ms, 3),
      total_ms: r(s.total_ms, 3),
      engine_ran: s.engine_ran,
      ...(s.progress != null ? { progress: r(s.progress, 4) } : {}),
      ...(s.disagreement != null ? { disagreement: r(s.disagreement, 4) } : {}),
      ...(s.arm_jerk != null ? { arm_jerk: r(s.arm_jerk, 4) } : {}),
      ...(s.base_jerk != null ? { base_jerk: r(s.base_jerk, 4) } : {}),
      ...(s.base_speed != null ? { base_speed: r(s.base_speed, 4) } : {}),
    })),
  };

  const text = JSON.stringify(profile, null, 2);
  try {
    await navigator.clipboard.writeText(text);
    flashBtn(btn, "Copied ✓", "Export JSON");
  } catch {
    downloadText(text, `act-profile-${st.sampleCount}steps.json`);
    flashBtn(btn, "Downloaded", "Export JSON");
  }
}

/** @param {HTMLButtonElement} btn @param {string} msg @param {string} restore */
function flashBtn(btn, msg, restore) {
  btn.textContent = msg;
  btn.disabled = true;
  setTimeout(() => {
    btn.textContent = restore;
    btn.disabled = false;
  }, 1400);
}

/** @param {string} text @param {string} filename */
function downloadText(text, filename) {
  const url = URL.createObjectURL(new Blob([text], { type: "application/json" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

// ---- rendering ------------------------------------------------------------

/**
 * @param {Sample[]} samples
 * @param {HTMLElement} statsRow
 * @param {Record<string, ReturnType<typeof chartCard>>} cards
 */
function render(samples, statsRow, cards) {
  // computeStats sorts the retained record for percentiles, so do it once per tick and
  // share it with the views that need aggregates.
  const st = computeStats(samples);
  renderStats(st, statsRow);
  renderTimeseries(samples, cards.ts.body);
  renderHistogram(samples, cards.hist.body);
  renderBreakdown(st, cards.breakdown.body);
  renderLine(samples, cards.progress.body, (s) => s.progress, "var(--ok)", { lo: 0, hi: 1 });
  renderLine(samples, cards.disagreement.body, (s) => s.disagreement, "var(--blue)", {});
  renderLine(samples, cards.armMotion.body, (s) => s.arm_jerk, "var(--accent)", {
    lo: 0,
    threshold: STILL_ARM_EPS,
  });
  renderLine(samples, cards.baseSpeed.body, (s) => s.base_speed, "var(--blue)", {
    lo: 0,
    threshold: STILL_BASE_EPS,
  });
}

/**
 * Lazily attach a persistent scaffold to a host and reuse it across ticks, rebuilding only
 * when `key` changes (e.g. empty <-> populated). Returns whatever `build` produced so the
 * caller can update its nodes in place instead of recreating the subtree every render.
 * @template T
 * @param {HTMLElement} host
 * @param {string} key
 * @param {(host: HTMLElement) => T} build
 * @returns {T}
 */
function scaffold(host, key, build) {
  const h = /** @type {any} */ (host);
  if (h.__key !== key) {
    host.replaceChildren();
    h.__ui = build(host);
    h.__key = key;
  }
  return h.__ui;
}

/**
 * Generic line chart over the recorded window for an optional per-sample scalar.
 * @param {Sample[]} samples
 * @param {HTMLElement} host
 * @param {(s: Sample) => number|undefined} accessor
 * @param {string} color
 * @param {{lo?: number, hi?: number, threshold?: number}} fixed  optional fixed y-range
 *   (else auto) and an optional dashed guide line at a threshold value
 */
function renderLine(samples, host, accessor, color, fixed) {
  const pts = samples.slice(-MAX_PLOT_POINTS).map(accessor).filter((v) => typeof v === "number");
  if (!pts.length) {
    scaffold(host, "empty", (h) => placeholder(h, "no signal in this recording"));
    return;
  }

  const lo = fixed.lo ?? Math.min(...pts);
  const hi = fixed.hi ?? Math.max(...pts);
  const span = hi - lo || 1;
  const W = 1000;
  const H = 240;
  // Persist the svg + path + axis; only the path's `d` and the axis labels change each tick.
  const { path, axis, thresholdLine } = scaffold(host, "data", (h) => {
    const svg = makeSvg(W, H);
    svg.setAttribute("preserveAspectRatio", "none");
    const thr = fixed.threshold != null ? hline(0, W, "var(--danger)", "4 4") : null;
    if (thr) svg.appendChild(thr);
    const p = document.createElementNS(SVG_NS, "path");
    p.setAttribute("fill", "none");
    p.setAttribute("stroke", color);
    p.setAttribute("stroke-width", "1.5");
    p.setAttribute("vector-effect", "non-scaling-stroke");
    svg.appendChild(p);
    h.appendChild(svg);
    const ax = axisLabels(["", "", ""]);
    h.appendChild(ax);
    return { path: p, axis: ax, thresholdLine: thr };
  });

  if (thresholdLine && fixed.threshold != null) {
    // Reposition each tick — the auto range rescales with the data.
    const ty = H - ((fixed.threshold - lo) / span) * H;
    thresholdLine.setAttribute("y1", ty.toFixed(1));
    thresholdLine.setAttribute("y2", ty.toFixed(1));
    thresholdLine.setAttribute("opacity", ty >= 0 && ty <= H ? "1" : "0");
  }

  let d = "";
  pts.forEach((v, i) => {
    const x = (i / Math.max(1, pts.length - 1)) * W;
    const y = H - ((v - lo) / span) * H;
    d += `${i === 0 ? "M" : "L"}${x.toFixed(1)} ${y.toFixed(1)} `;
  });
  path.setAttribute("d", d.trim());
  const last = pts[pts.length - 1];
  setAxis(axis, [hi.toFixed(2), `now ${last.toFixed(2)}`, lo.toFixed(2)]);
}

/**
 * Aggregate the recorded samples into the numbers the page shows and exports.
 * @param {Sample[]} samples
 */
function computeStats(samples) {
  const n = samples.length;
  const total = samples.map((s) => s.total_ms);
  const run = samples.filter((s) => s.engine_ran);
  const inference = run.map((s) => s.inference_ms);
  const engineFwd = run.map((s) => s.engine_ms || 0);
  const period = n ? samples[n - 1].period_ms : 40;
  const overBudget = total.filter((v) => v > period).length;
  const p95 = percentile(total, 95);
  // Mean per-step inference splits into the pure GPU forward, the final D2H copy,
  // and the remainder (input copies + temporal-ensemble + Python).
  const meanInfer = mean(samples.map((s) => s.inference_ms));
  const meanEngine = mean(samples.map((s) => s.engine_ms || 0));
  const meanTransfer = mean(samples.map((s) => s.transfer_ms || 0));
  // Behavior-quality signals (present only when recorded against an ensembling policy
  // with a progress head). num() drops missing values so stats stay meaningful.
  const num = (key) => samples.map((s) => s[key]).filter((v) => typeof v === "number");
  const progress = num("progress");
  const disagree = num("disagreement");
  const armJerk = num("arm_jerk");
  const baseJerk = num("base_jerk");
  const baseSpeed = num("base_speed");
  const still = stillness(samples);
  return {
    sampleCount: n,
    budgetMs: period,
    effectiveHz: effectiveHz(samples),
    overBudgetPct: n ? (100 * overBudget) / n : 0,
    headroomMs: period - p95,
    total: {
      mean: mean(total),
      p50: percentile(total, 50),
      p95,
      p99: percentile(total, 99),
      max: total.length ? Math.max(...total) : 0,
    },
    model: { count: run.length, mean: mean(inference), p95: percentile(inference, 95) },
    engine: { mean: mean(engineFwd), p50: percentile(engineFwd, 50), p95: percentile(engineFwd, 95) },
    breakdown: {
      preprocess: mean(samples.map((s) => s.preprocess_ms)),
      engine: meanEngine,
      overhead: Math.max(0, meanInfer - meanEngine - meanTransfer),
      transfer: meanTransfer,
      postprocess: mean(samples.map((s) => s.postprocess_ms)),
    },
    quality: {
      progressLast: progress.length ? progress[progress.length - 1] : null,
      progressMax: progress.length ? Math.max(...progress) : null,
      disagreementMean: disagree.length ? mean(disagree) : null,
      disagreementP95: disagree.length ? percentile(disagree, 95) : null,
      armJerkMean: armJerk.length ? mean(armJerk) : null,
      // The p5 floors are the quietest the signals get — idle_arm_eps/idle_base_eps
      // must sit above them for the idle stop to ever fire.
      armJerkFloor: armJerk.length ? percentile(armJerk, 5) : null,
      baseJerkMean: baseJerk.length ? mean(baseJerk) : null,
      baseSpeedFloor: baseSpeed.length ? percentile(baseSpeed, 5) : null,
      stillLongest: still.longest,
      stillNow: still.now,
    },
  };
}

/**
 * Still-dwell stats at the detector's default eps: a sample is "still" when both
 * motion signals are below eps, exactly the condition the idle stop dwells on.
 * `longest` is the longest still run in the record and `now` the trailing one, in
 * seconds of message time — read them against idle_seconds. Samples missing either
 * signal break the run (the detector likewise restarts its dwell on gaps).
 * @param {Sample[]} samples
 * @returns {{ longest: number|null, now: number|null }}
 */
function stillness(samples) {
  if (!samples.some((s) => typeof s.arm_jerk === "number" && typeof s.base_speed === "number")) {
    return { longest: null, now: null };
  }
  let longest = 0;
  let now = 0;
  let runStart = null;
  for (const s of samples) {
    const isStill =
      typeof s.arm_jerk === "number" && s.arm_jerk < STILL_ARM_EPS &&
      typeof s.base_speed === "number" && s.base_speed < STILL_BASE_EPS;
    if (!isStill) {
      runStart = null;
      now = 0;
      continue;
    }
    if (runStart === null) runStart = s.t;
    now = s.t - runStart;
    if (now > longest) longest = now;
  }
  return { longest, now };
}

/** @param {ReturnType<typeof computeStats>} st @param {HTMLElement} host */
function renderStats(st, host) {
  const n = st.sampleCount;
  const q = st.quality;
  const fmt = (v, d = 3) => (v == null ? "—" : v.toFixed(d));
  // Fixed-order rows; labels are stable, so build the cards once and update value/foot/warn
  // in place each tick. The two quality cards are always present but hidden until recorded.
  const rows = [
    ["samples", n ? String(n) : "—", "recorded steps", false, true],
    ["effective rate", n ? `${st.effectiveHz.toFixed(1)} Hz` : "—", `budget ${st.budgetMs.toFixed(0)} ms`, false, true],
    ["total p50", fmtMs(st.total.p50), "median step", false, true],
    ["total p95", fmtMs(st.total.p95), "95th pct", false, true],
    ["total p99", fmtMs(st.total.p99), "99th pct", false, true],
    ["engine p50", fmtMs(st.engine.p50), "pure GPU forward", false, true],
    ["model p95", fmtMs(st.model.p95), `${st.model.count} engine runs`, false, true],
    ["over budget", n ? `${st.overBudgetPct.toFixed(0)}%` : "—", `> ${st.budgetMs.toFixed(0)} ms`, false, true],
    ["headroom", n ? fmtMs(st.headroomMs) : "—", "budget − p95", st.headroomMs < 0, true],
    ["arm jerk", `${fmt(q.armJerkMean)} rad`, "mean ‖Δcmd‖ — smoothness", false, q.armJerkMean != null],
    ["uncertainty", fmt(q.disagreementMean), "mean ensemble disagreement", false, q.disagreementMean != null],
    // Auto-stop tuning: each card names the LearnedStopDetector knob it informs.
    ["progress", fmt(q.progressLast), `max ${fmt(q.progressMax)} — progress_threshold`, false, q.progressLast != null],
    ["arm floor", fmt(q.armJerkFloor, 4), "p5 ‖Δcmd‖ — idle_arm_eps above this", false, q.armJerkFloor != null],
    ["base floor", fmt(q.baseSpeedFloor, 4), "p5 |cmd speed| — idle_base_eps above this", false, q.baseSpeedFloor != null],
    ["longest still", q.stillLongest == null ? "—" : `${q.stillLongest.toFixed(1)} s`, `both < ${STILL_ARM_EPS} — idle_seconds`, false, q.stillLongest != null],
    ["still now", q.stillNow == null ? "—" : `${q.stillNow.toFixed(1)} s`, "trailing dwell at default eps", false, q.stillNow != null],
  ];
  const cards = scaffold(host, "stats", (h) => rows.map((r) => stat(/** @type {string} */ (r[0]), h)));
  rows.forEach((r, i) => {
    const c = cards[i];
    c.value.textContent = /** @type {string} */ (r[1]);
    c.foot.textContent = /** @type {string} */ (r[2]);
    c.card.classList.toggle("warn", !!r[3]);
    c.card.hidden = !r[4];
  });
}

/**
 * Build one stat card with a fixed label; value/foot are filled in by the caller each tick.
 * @param {string} label @param {HTMLElement} host
 */
function stat(label, host) {
  const card = document.createElement("div");
  card.className = "prof-stat";
  const l = document.createElement("div");
  l.className = "prof-stat-label microlabel";
  l.textContent = label;
  const value = document.createElement("div");
  value.className = "prof-stat-value mono";
  const foot = document.createElement("div");
  foot.className = "prof-stat-foot";
  card.append(l, value, foot);
  host.appendChild(card);
  return { card, value, foot };
}

/** @param {Sample[]} samples @param {HTMLElement} host */
function renderTimeseries(samples, host) {
  if (!samples.length) {
    scaffold(host, "empty", (h) => placeholder(h));
    return;
  }

  const pts = samples.slice(-MAX_PLOT_POINTS);
  const period = pts[pts.length - 1].period_ms;
  const totals = pts.map((s) => s.total_ms);
  const max = Math.max(period, ...totals) * 1.1;
  const W = 1000;
  const H = 240;

  const { svg, axis } = scaffold(host, "data", (h) => {
    const el = makeSvg(W, H);
    el.setAttribute("preserveAspectRatio", "none");
    h.appendChild(el);
    const ax = axisLabels(["", "", ""]);
    h.appendChild(ax);
    h.appendChild(legend([["var(--accent)", "total step"], ["var(--danger)", "budget"], ["var(--accent-dim)", "model ran"]]));
    return { svg: el, axis: ax };
  });

  // The plotted geometry changes every tick (window scrolls, model-ran marker count varies),
  // so redraw the svg's interior; the card, axis, and legend persist across ticks.
  svg.replaceChildren();
  const by = H - (period / max) * H;
  svg.appendChild(hline(by, W, "var(--danger)", "4 4")); // budget line
  pts.forEach((s, i) => {
    if (!s.engine_ran) return; // model-ran markers (faint) so the chunk cadence is visible
    const x = (i / Math.max(1, pts.length - 1)) * W;
    svg.appendChild(vline(x, H, "var(--accent-faint)"));
  });
  let d = "";
  pts.forEach((s, i) => {
    const x = (i / Math.max(1, pts.length - 1)) * W;
    const y = H - (s.total_ms / max) * H;
    d += `${i === 0 ? "M" : "L"}${x.toFixed(1)} ${y.toFixed(1)} `;
  });
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", d.trim());
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", "var(--accent)");
  path.setAttribute("stroke-width", "1.5");
  path.setAttribute("vector-effect", "non-scaling-stroke");
  svg.appendChild(path);

  setAxis(axis, [fmtMs(max, 0), `budget ${period.toFixed(0)} ms`, "0"]);
}

/** @param {Sample[]} samples @param {HTMLElement} host */
function renderHistogram(samples, host) {
  const vals = samples.filter((s) => s.engine_ran).map((s) => s.inference_ms);
  if (!vals.length) {
    scaffold(host, "empty", (h) => placeholder(h, "no engine-run steps yet"));
    return;
  }

  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const span = max - min || 1;
  const bins = 24;
  const counts = new Array(bins).fill(0);
  for (const v of vals) {
    const b = Math.min(bins - 1, Math.floor(((v - min) / span) * bins));
    counts[b]++;
  }
  const peak = Math.max(...counts);

  const W = 1000;
  const H = 240;
  // bin count and x/width are fixed, so build the bars + median marker once and only update
  // each bar's height and the median's x-position per tick.
  const { rects, median, axis } = scaffold(host, "data", (h) => {
    const svg = makeSvg(W, H);
    const bw = W / bins;
    const bars = counts.map((_, i) => {
      const rect = document.createElementNS(SVG_NS, "rect");
      rect.setAttribute("x", (i * bw + 1).toFixed(1));
      rect.setAttribute("width", (bw - 2).toFixed(1));
      rect.setAttribute("fill", "var(--accent)");
      rect.setAttribute("opacity", "0.85");
      svg.appendChild(rect);
      return rect;
    });
    const med = vline(0, H, "var(--text)", "1.5");
    svg.appendChild(med);
    h.appendChild(svg);
    const ax = axisLabels(["", "", ""]);
    h.appendChild(ax);
    return { rects: bars, median: med, axis: ax };
  });

  rects.forEach((rect, i) => {
    const h = (counts[i] / peak) * (H - 4);
    rect.setAttribute("y", (H - h).toFixed(1));
    rect.setAttribute("height", h.toFixed(1));
  });
  const med = percentile(vals, 50);
  const mx = (((med - min) / span) * W).toFixed(1);
  median.setAttribute("x1", mx);
  median.setAttribute("x2", mx);
  setAxis(axis, [`${min.toFixed(1)} ms`, `median ${med.toFixed(1)} ms`, `${max.toFixed(1)} ms`]);
}

const BREAKDOWN_ROWS = [
  ["Preprocess", "var(--blue)"],
  ["Model fwd (GPU)", "var(--accent)"],
  ["Copy + ensemble", "var(--accent-dim)"],
  ["GPU → CPU copy", "var(--muted)"],
  ["Postprocess", "var(--ok)"],
];

/** @param {ReturnType<typeof computeStats>} st @param {HTMLElement} host */
function renderBreakdown(st, host) {
  if (!st.sampleCount) {
    scaffold(host, "empty", (h) => placeholder(h));
    return;
  }
  const b = st.breakdown;
  const vals = [b.preprocess, b.engine, b.overhead, b.transfer, b.postprocess];
  const sum = vals.reduce((a, v) => a + v, 0) || 1;
  // Fixed label/colour rows, so build them once and only nudge the fill width + value text.
  const bars = scaffold(host, "data", (h) => BREAKDOWN_ROWS.map(([label, color]) => breakdownBar(label, color, h)));
  bars.forEach((bar, i) => {
    bar.fill.style.width = `${(vals[i] / sum) * 100}%`;
    bar.val.textContent = fmtMs(vals[i]);
  });
}

/** @param {string} label @param {string} color @param {HTMLElement} host */
function breakdownBar(label, color, host) {
  const row = document.createElement("div");
  row.className = "prof-bar-row";
  const name = document.createElement("span");
  name.className = "prof-bar-name";
  name.textContent = label;
  const track = document.createElement("div");
  track.className = "prof-bar-track";
  const fill = document.createElement("div");
  fill.className = "prof-bar-fill";
  fill.style.background = color;
  track.appendChild(fill);
  const val = document.createElement("span");
  val.className = "prof-bar-val mono";
  row.append(name, track, val);
  host.appendChild(row);
  return { fill, val };
}

// ---- small DOM/SVG helpers ------------------------------------------------

/** @param {string} title @param {string} foot */
function chartCard(title, foot) {
  const card = document.createElement("div");
  card.className = "prof-chart-card";
  const h = document.createElement("div");
  h.className = "prof-chart-head";
  const t = document.createElement("span");
  t.className = "prof-chart-title";
  t.textContent = title;
  const f = document.createElement("span");
  f.className = "prof-chart-foot microlabel";
  f.textContent = foot;
  h.append(t, f);
  const bodyEl = document.createElement("div");
  bodyEl.className = "prof-chart-body";
  card.append(h, bodyEl);
  return { card, body: bodyEl };
}

/** @param {number} w @param {number} h */
function makeSvg(w, h) {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.setAttribute("class", "prof-svg");
  return svg;
}

/** @param {number} y @param {number} w @param {string} color @param {string} dash */
function hline(y, w, color, dash) {
  const l = document.createElementNS(SVG_NS, "line");
  l.setAttribute("x1", "0");
  l.setAttribute("x2", String(w));
  l.setAttribute("y1", y.toFixed(1));
  l.setAttribute("y2", y.toFixed(1));
  l.setAttribute("stroke", color);
  l.setAttribute("stroke-width", "1");
  l.setAttribute("stroke-dasharray", dash);
  l.setAttribute("vector-effect", "non-scaling-stroke");
  return l;
}

/** @param {number} x @param {number} h @param {string} color @param {string} [w] */
function vline(x, h, color, w = "1") {
  const l = document.createElementNS(SVG_NS, "line");
  l.setAttribute("x1", x.toFixed(1));
  l.setAttribute("x2", x.toFixed(1));
  l.setAttribute("y1", "0");
  l.setAttribute("y2", String(h));
  l.setAttribute("stroke", color);
  l.setAttribute("stroke-width", w);
  l.setAttribute("vector-effect", "non-scaling-stroke");
  return l;
}

/** @param {string[]} labels top/mid/bottom */
function axisLabels(labels) {
  const wrap = document.createElement("div");
  wrap.className = "prof-axis";
  for (const text of labels) {
    const span = document.createElement("span");
    span.textContent = text;
    wrap.appendChild(span);
  }
  return wrap;
}

/** Update an existing axisLabels row in place. @param {HTMLElement} wrap @param {string[]} labels */
function setAxis(wrap, labels) {
  wrap.querySelectorAll("span").forEach((span, i) => {
    span.textContent = labels[i] ?? "";
  });
}

/** @param {Array<[string,string]>} items color,label */
function legend(items) {
  const wrap = document.createElement("div");
  wrap.className = "prof-legend";
  for (const [color, label] of items) {
    const item = document.createElement("span");
    item.className = "prof-legend-item";
    item.innerHTML = `<span class="prof-legend-swatch" style="background:${color}"></span>${label}`;
    wrap.appendChild(item);
  }
  return wrap;
}

/** @param {HTMLElement} host @param {string} [text] */
function placeholder(host, text = "no data — run a rollout to chart it") {
  const p = document.createElement("p");
  p.className = "prof-empty microlabel";
  p.textContent = text;
  host.appendChild(p);
}

// ---- math -----------------------------------------------------------------

/** @param {number[]} arr */
function mean(arr) {
  if (!arr.length) return 0;
  return arr.reduce((a, b) => a + b, 0) / arr.length;
}

/** @param {number[]} arr @param {number} p 0-100 */
function percentile(arr, p) {
  if (!arr.length) return 0;
  const sorted = [...arr].sort((a, b) => a - b);
  const idx = Math.min(sorted.length - 1, Math.floor((p / 100) * sorted.length));
  return sorted[idx];
}

/** @param {Sample[]} samples → steps/sec from message timestamps */
function effectiveHz(samples) {
  if (samples.length < 2) return 0;
  const span = samples[samples.length - 1].t - samples[0].t;
  return span > 0 ? (samples.length - 1) / span : 0;
}

/** @param {number} ms @param {number} [digits] */
function fmtMs(ms, digits = 1) {
  if (!isFinite(ms)) return "—";
  return `${ms.toFixed(digits)} ms`;
}
