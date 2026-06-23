// @ts-check
// Profiling page entry — record and visualize ACT policy inference timing.
//
// The manipulation server publishes a per-step timing breakdown on
// INFERENCE_PROFILE_TOPIC (std_msgs/String → JSON) while a learned behavior
// runs. This page subscribes, lets the operator Record / Stop / Clear a window
// of those samples, and renders live latency graphs so we can see where the
// 25 Hz inference budget goes and whether there's headroom to improve.

import { ros } from "../rosClient.js";
import { initShell } from "../shell.js";
import { mountPage } from "../pageMount.js";
import { INFERENCE_PROFILE_TOPIC } from "../constants.js";

initShell("profiling", "../");

const stage = /** @type {HTMLElement} */ (document.getElementById("stage"));
mountPage(stage, "profiling", buildView);

const SVG_NS = "http://www.w3.org/2000/svg";
const MAX_PLOT_POINTS = 400; // timeseries window; stats use the full record

/**
 * @typedef {{ seq:number, t:number, preprocess_ms:number, inference_ms:number,
 *   engine_ms:number, transfer_ms:number, postprocess_ms:number, total_ms:number,
 *   engine_ran:boolean, period_ms:number, progress?:number, disagreement?:number,
 *   arm_jerk?:number, base_jerk?:number }} Sample
 */

/**
 * @param {HTMLElement} root
 * @returns {{ destroy: () => void }}
 */
function buildView(root) {
  /** @type {Sample[]} */
  let samples = [];
  let recording = false;
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

  const recordBtn = document.createElement("button");
  recordBtn.className = "prof-btn prof-btn-rec";
  const exportBtn = document.createElement("button");
  exportBtn.className = "prof-btn";
  exportBtn.textContent = "Export JSON";
  const clearBtn = document.createElement("button");
  clearBtn.className = "prof-btn";
  clearBtn.textContent = "Clear";

  head.append(title, sub, live, spacer, recordBtn, exportBtn, clearBtn);

  // ---- body ---------------------------------------------------------------
  const body = document.createElement("div");
  body.className = "prof-body";

  const statsRow = document.createElement("div");
  statsRow.className = "prof-stats";

  const charts = document.createElement("div");
  charts.className = "prof-charts";
  const tsCard = chartCard("Total step latency vs 25 Hz budget", "ms over recorded steps");
  const histCard = chartCard("Model inference time", "engine-run steps only");
  const breakdownCard = chartCard("Average per-step breakdown", "where each step spends time");
  // Behavior quality: how well / how confident the policy is, vs just how fast.
  const progressCard = chartCard("Progress", "policy's learned progress head (action[8])");
  const disagreementCard = chartCard("Ensemble disagreement", "uncertainty — spikes precede failures");
  charts.append(
    tsCard.card,
    histCard.card,
    breakdownCard.card,
    progressCard.card,
    disagreementCard.card,
  );

  const hint = document.createElement("p");
  hint.className = "prof-hint microlabel";
  hint.textContent =
    "Run a learned behavior, then press Record. Data only flows while a policy is executing.";

  body.append(statsRow, charts, hint);
  root.append(head, body);

  // ---- controls -----------------------------------------------------------
  function syncRecordBtn() {
    recordBtn.textContent = recording ? "■ Stop" : "● Record";
    recordBtn.classList.toggle("active", recording);
  }
  recordBtn.addEventListener("click", () => {
    recording = !recording;
    syncRecordBtn();
    dirty = true;
  });
  clearBtn.addEventListener("click", () => {
    samples = [];
    dirty = true;
  });
  exportBtn.addEventListener("click", () => exportJson(samples, exportBtn));
  syncRecordBtn();

  // ---- subscription -------------------------------------------------------
  const unsubscribe = ros.subscribe(INFERENCE_PROFILE_TOPIC, (msg) => {
    lastMsgAt = performance.now();
    if (!recording) return;
    const s = parseSample(msg?.data);
    if (s) {
      samples.push(s);
      dirty = true;
    }
  });

  // ---- render loop --------------------------------------------------------
  // Throttle to ~7 Hz: SVG re-render on every 25 Hz sample is wasteful and the
  // eye can't read faster anyway. The live dot updates on the same tick.
  const timer = setInterval(() => {
    const sinceMsg = performance.now() - lastMsgAt;
    const flowing = lastMsgAt > 0 && sinceMsg < 500;
    setLive(live, flowing, recording);
    if (dirty) {
      render(samples, statsRow, tsCard, histCard, breakdownCard, progressCard, disagreementCard);
      dirty = false;
    }
  }, 140);

  render(samples, statsRow, tsCard, histCard, breakdownCard, progressCard, disagreementCard);

  return {
    destroy() {
      clearInterval(timer);
      unsubscribe();
    },
  };
}

/** @param {any} data @returns {Sample | null} */
function parseSample(data) {
  if (typeof data !== "string") return null;
  try {
    const s = JSON.parse(data);
    if (typeof s.total_ms !== "number") return null;
    return s;
  } catch {
    return null;
  }
}

/** @param {HTMLElement} live @param {boolean} flowing @param {boolean} recording */
function setLive(live, flowing, recording) {
  const dot = live.querySelector(".prof-live-dot");
  const text = live.querySelector(".prof-live-text");
  if (!dot || !text) return;
  const state = !flowing ? "idle" : recording ? "rec" : "live";
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
      "failures/OOD), arm_jerk/base_jerk (per-step ‖Δcommand‖, lower=smoother). samples is the raw record.",
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
 * @param {ReturnType<typeof chartCard>} tsCard
 * @param {ReturnType<typeof chartCard>} histCard
 * @param {ReturnType<typeof chartCard>} breakdownCard
 * @param {ReturnType<typeof chartCard>} progressCard
 * @param {ReturnType<typeof chartCard>} disagreementCard
 */
function render(samples, statsRow, tsCard, histCard, breakdownCard, progressCard, disagreementCard) {
  renderStats(samples, statsRow);
  renderTimeseries(samples, tsCard.body);
  renderHistogram(samples, histCard.body);
  renderBreakdown(samples, breakdownCard.body);
  renderLine(samples, progressCard.body, (s) => s.progress, "var(--ok)", { lo: 0, hi: 1 });
  renderLine(samples, disagreementCard.body, (s) => s.disagreement, "var(--blue)", {});
}

/**
 * Generic line chart over the recorded window for an optional per-sample scalar.
 * @param {Sample[]} samples
 * @param {HTMLElement} host
 * @param {(s: Sample) => number|undefined} accessor
 * @param {string} color
 * @param {{lo?: number, hi?: number}} fixed  optional fixed y-range (else auto)
 */
function renderLine(samples, host, accessor, color, fixed) {
  host.replaceChildren();
  const pts = samples.slice(-MAX_PLOT_POINTS).map(accessor).filter((v) => typeof v === "number");
  if (!pts.length) return placeholder(host, "no signal in this recording");

  const lo = fixed.lo ?? Math.min(...pts);
  const hi = fixed.hi ?? Math.max(...pts);
  const span = hi - lo || 1;
  const W = 1000;
  const H = 240;
  const svg = makeSvg(W, H);
  svg.setAttribute("preserveAspectRatio", "none");

  let d = "";
  pts.forEach((v, i) => {
    const x = (i / Math.max(1, pts.length - 1)) * W;
    const y = H - ((v - lo) / span) * H;
    d += `${i === 0 ? "M" : "L"}${x.toFixed(1)} ${y.toFixed(1)} `;
  });
  const path = document.createElementNS(SVG_NS, "path");
  path.setAttribute("d", d.trim());
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", color);
  path.setAttribute("stroke-width", "1.5");
  path.setAttribute("vector-effect", "non-scaling-stroke");
  svg.appendChild(path);

  host.appendChild(svg);
  const last = pts[pts.length - 1];
  host.appendChild(axisLabels([hi.toFixed(2), `now ${last.toFixed(2)}`, lo.toFixed(2)]));
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
      disagreementMean: disagree.length ? mean(disagree) : null,
      disagreementP95: disagree.length ? percentile(disagree, 95) : null,
      armJerkMean: armJerk.length ? mean(armJerk) : null,
      baseJerkMean: baseJerk.length ? mean(baseJerk) : null,
    },
  };
}

/** @param {Sample[]} samples @param {HTMLElement} host */
function renderStats(samples, host) {
  host.replaceChildren();
  const n = samples.length;
  const st = computeStats(samples);
  const cards = [
    stat("samples", n ? String(n) : "—", "recorded steps"),
    stat("effective rate", n ? `${st.effectiveHz.toFixed(1)} Hz` : "—", `budget ${st.budgetMs.toFixed(0)} ms`),
    stat("total p50", fmtMs(st.total.p50), "median step"),
    stat("total p95", fmtMs(st.total.p95), "95th pct"),
    stat("total p99", fmtMs(st.total.p99), "99th pct"),
    stat("engine p50", fmtMs(st.engine.p50), "pure GPU forward"),
    stat("model p95", fmtMs(st.model.p95), `${st.model.count} engine runs`),
    stat("over budget", n ? `${st.overBudgetPct.toFixed(0)}%` : "—", `> ${st.budgetMs.toFixed(0)} ms`),
    stat("headroom", n ? fmtMs(st.headroomMs) : "—", "budget − p95", st.headroomMs < 0),
  ];
  const q = st.quality;
  const fmt = (v, d = 3) => (v == null ? "—" : v.toFixed(d));
  if (q.armJerkMean != null) cards.push(stat("arm jerk", `${fmt(q.armJerkMean)} rad`, "mean ‖Δcmd‖ — smoothness"));
  if (q.disagreementMean != null) cards.push(stat("uncertainty", fmt(q.disagreementMean), "mean ensemble disagreement"));
  host.append(...cards);
}

/**
 * @param {string} label @param {string} value @param {string} foot @param {boolean} [warn]
 */
function stat(label, value, foot, warn = false) {
  const card = document.createElement("div");
  card.className = "prof-stat" + (warn ? " warn" : "");
  const l = document.createElement("div");
  l.className = "prof-stat-label microlabel";
  l.textContent = label;
  const v = document.createElement("div");
  v.className = "prof-stat-value mono";
  v.textContent = value;
  const f = document.createElement("div");
  f.className = "prof-stat-foot";
  f.textContent = foot;
  card.append(l, v, f);
  return card;
}

/** @param {Sample[]} samples @param {HTMLElement} host */
function renderTimeseries(samples, host) {
  host.replaceChildren();
  if (!samples.length) return placeholder(host);

  const pts = samples.slice(-MAX_PLOT_POINTS);
  const period = pts[pts.length - 1].period_ms;
  const totals = pts.map((s) => s.total_ms);
  const max = Math.max(period, ...totals) * 1.1;

  const W = 1000;
  const H = 240;
  const svg = makeSvg(W, H);
  svg.setAttribute("preserveAspectRatio", "none");

  // budget line
  const by = H - (period / max) * H;
  svg.appendChild(hline(by, W, "var(--danger)", "4 4"));

  // model-ran markers (faint) so the chunk cadence is visible
  pts.forEach((s, i) => {
    if (!s.engine_ran) return;
    const x = (i / Math.max(1, pts.length - 1)) * W;
    svg.appendChild(vline(x, H, "var(--accent-faint)"));
  });

  // total line
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

  host.appendChild(svg);
  host.appendChild(axisLabels([fmtMs(max, 0), `budget ${period.toFixed(0)} ms`, "0"]));
  host.appendChild(legend([["var(--accent)", "total step"], ["var(--danger)", "budget"], ["var(--accent-dim)", "model ran"]]));
}

/** @param {Sample[]} samples @param {HTMLElement} host */
function renderHistogram(samples, host) {
  host.replaceChildren();
  const vals = samples.filter((s) => s.engine_ran).map((s) => s.inference_ms);
  if (!vals.length) return placeholder(host, "no engine-run steps yet");

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
  const svg = makeSvg(W, H);
  const bw = W / bins;
  counts.forEach((c, i) => {
    const h = (c / peak) * (H - 4);
    const rect = document.createElementNS(SVG_NS, "rect");
    rect.setAttribute("x", (i * bw + 1).toFixed(1));
    rect.setAttribute("y", (H - h).toFixed(1));
    rect.setAttribute("width", (bw - 2).toFixed(1));
    rect.setAttribute("height", h.toFixed(1));
    rect.setAttribute("fill", "var(--accent)");
    rect.setAttribute("opacity", "0.85");
    svg.appendChild(rect);
  });

  // median marker
  const med = percentile(vals, 50);
  const mx = ((med - min) / span) * W;
  svg.appendChild(vline(mx, H, "var(--text)", "1.5"));

  host.appendChild(svg);
  host.appendChild(axisLabels([`${min.toFixed(1)} ms`, `median ${med.toFixed(1)} ms`, `${max.toFixed(1)} ms`]));
}

/** @param {Sample[]} samples @param {HTMLElement} host */
function renderBreakdown(samples, host) {
  host.replaceChildren();
  if (!samples.length) return placeholder(host);

  const b = computeStats(samples).breakdown;
  const sum = b.preprocess + b.engine + b.overhead + b.transfer + b.postprocess || 1;

  const rows = [
    ["Preprocess", b.preprocess, "var(--blue)"],
    ["Model fwd (GPU)", b.engine, "var(--accent)"],
    ["Copy + ensemble", b.overhead, "var(--accent-dim)"],
    ["GPU → CPU copy", b.transfer, "var(--muted)"],
    ["Postprocess", b.postprocess, "var(--ok)"],
  ];

  for (const [label, val, color] of rows) {
    const row = document.createElement("div");
    row.className = "prof-bar-row";
    const name = document.createElement("span");
    name.className = "prof-bar-name";
    name.textContent = /** @type {string} */ (label);
    const track = document.createElement("div");
    track.className = "prof-bar-track";
    const fill = document.createElement("div");
    fill.className = "prof-bar-fill";
    fill.style.width = `${(/** @type {number} */ (val) / sum) * 100}%`;
    fill.style.background = /** @type {string} */ (color);
    track.appendChild(fill);
    const v = document.createElement("span");
    v.className = "prof-bar-val mono";
    v.textContent = fmtMs(/** @type {number} */ (val));
    row.append(name, track, v);
    host.appendChild(row);
  }
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
function placeholder(host, text = "no data — press Record while a behavior runs") {
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
