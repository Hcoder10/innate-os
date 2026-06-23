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
 *   postprocess_ms:number, total_ms:number, engine_ran:boolean, period_ms:number }} Sample
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
  const clearBtn = document.createElement("button");
  clearBtn.className = "prof-btn";
  clearBtn.textContent = "Clear";

  head.append(title, sub, live, spacer, recordBtn, clearBtn);

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
  charts.append(tsCard.card, histCard.card, breakdownCard.card);

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
      render(samples, statsRow, tsCard, histCard, breakdownCard);
      dirty = false;
    }
  }, 140);

  render(samples, statsRow, tsCard, histCard, breakdownCard);

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

// ---- rendering ------------------------------------------------------------

/**
 * @param {Sample[]} samples
 * @param {HTMLElement} statsRow
 * @param {ReturnType<typeof chartCard>} tsCard
 * @param {ReturnType<typeof chartCard>} histCard
 * @param {ReturnType<typeof chartCard>} breakdownCard
 */
function render(samples, statsRow, tsCard, histCard, breakdownCard) {
  renderStats(samples, statsRow);
  renderTimeseries(samples, tsCard.body);
  renderHistogram(samples, histCard.body);
  renderBreakdown(samples, breakdownCard.body);
}

/** @param {Sample[]} samples @param {HTMLElement} host */
function renderStats(samples, host) {
  host.replaceChildren();
  const n = samples.length;
  const total = samples.map((s) => s.total_ms);
  const engine = samples.filter((s) => s.engine_ran).map((s) => s.inference_ms);
  const period = n ? samples[n - 1].period_ms : 40;

  const overBudget = total.filter((v) => v > period).length;
  const hz = effectiveHz(samples);
  const p95 = percentile(total, 95);

  const cards = [
    stat("samples", n ? String(n) : "—", "recorded steps"),
    stat("effective rate", n ? `${hz.toFixed(1)} Hz` : "—", `budget ${period.toFixed(0)} ms`),
    stat("total p50", fmtMs(percentile(total, 50)), "median step"),
    stat("total p95", fmtMs(p95), "95th pct"),
    stat("total p99", fmtMs(percentile(total, 99)), "99th pct"),
    stat("model p95", fmtMs(percentile(engine, 95)), `${engine.length} engine runs`),
    stat("over budget", n ? `${((100 * overBudget) / n).toFixed(0)}%` : "—", `> ${period.toFixed(0)} ms`),
    stat("headroom", n ? fmtMs(period - p95) : "—", "budget − p95", period - p95 < 0),
  ];
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

  const pre = mean(samples.map((s) => s.preprocess_ms));
  const inf = mean(samples.map((s) => s.inference_ms));
  const post = mean(samples.map((s) => s.postprocess_ms));
  const sum = pre + inf + post || 1;

  const rows = [
    ["Preprocess", pre, "var(--blue)"],
    ["Model inference", inf, "var(--accent)"],
    ["Postprocess", post, "var(--ok)"],
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
