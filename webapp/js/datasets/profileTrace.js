// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Inference-profile trace for the episode player: when an episode was captured
// during a policy rollout, profile_recorder saved the per-step profile stream
// next to the HDF5 (GET /episode/profile). This renders it as one compact
// overlay chart plus a summary line, so a saved rollout can be judged —
// progress shape, motion, latency — right beside its video. Progress is drawn
// on an absolute 0–1 scale with labeled gridlines (its value feeds the
// auto-stop thresholds, so its height must mean something); the other series
// have no shared unit and are normalized to their own range, shape only.
// Hovering reads the raw (time, value) of every series at that step. Hidden
// entirely for episodes with no trace (teleop demos, pre-feature recordings).

const SVG_NS = "http://www.w3.org/2000/svg";

// series key → [label, color]; drawn/legended only when present in the data.
/** @type {[string, [string, string]][]} key → [display name, stroke color] */
const SERIES = [
  ["progress", ["progress", "#e0a03c"]],
  ["arm_jerk", ["arm motion", "#5aa9e6"]],
  ["base_speed", ["base speed", "#7bc98f"]],
  ["total_ms", ["step ms", "#b58ce6"]],
  ["disagreement", ["disagreement", "#e66a6a"]],
];

// Chart geometry: 1000×200 viewBox, series inset 5px from top/bottom edges.
const VBW = 1000;
const VBH = 200;
const PAD = 5;

/** @param {string} q episode query string ("dir=…&id=…") */
export function buildProfileTrace(q) {
  const el = document.createElement("div");
  el.className = "player-telemetry";
  el.hidden = true;

  const abort = new AbortController();
  fetch(`/episode/profile?${q}`, { signal: abort.signal })
    .then((r) => (r.ok ? r.text() : Promise.reject(new Error(`profile ${r.status}`))))
    .then((text) => render(el, text))
    .catch(() => {}); // 404 = not a rollout episode; stay hidden

  return {
    el,
    destroy() {
      abort.abort();
    },
  };
}

/** value → chart y for a series scaled to [min, min+span]. @param {number} v @param {number} min @param {number} span */
function toY(v, min, span) {
  return VBH - ((v - min) / span) * (VBH - 2 * PAD) - PAD;
}

/** @param {string} key @param {number} v */
function fmtValue(key, v) {
  return key === "total_ms" ? `${v.toFixed(1)} ms` : v.toFixed(3);
}

/** @param {HTMLElement} el @param {string} text */
function render(el, text) {
  const lines = text.split("\n").filter(Boolean);
  /** @type {any[]} */
  const samples = [];
  let context = null;
  for (const line of lines) {
    try {
      const rec = JSON.parse(line);
      if (rec.type === "context") context = rec;
      else samples.push(rec);
    } catch {
      /* skip torn line */
    }
  }
  if (samples.length < 2) return;

  const head = document.createElement("div");
  head.className = "telemetry-head";
  const label = document.createElement("span");
  label.className = "microlabel";
  label.textContent = "inference profile";
  const legend = document.createElement("div");
  legend.className = "telemetry-legend";
  head.append(label, legend);

  const plot = document.createElement("div");
  plot.className = "telemetry-plot";
  plot.style.height = "120px";
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", `0 0 ${VBW} ${VBH}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("class", "telemetry-svg");

  /** series drawn on this trace, keeping raw values for the hover readout.
   * @type {{key:string, name:string, color:string, ys:(number|null)[]}[]} */
  const drawn = [];

  for (const [key, [name, color]] of SERIES) {
    const ys = samples.map((s) => (typeof s[key] === "number" && isFinite(s[key]) ? s[key] : null));
    const vals = /** @type {number[]} */ (ys.filter((v) => v !== null));
    if (vals.length < 2) continue;
    let min, span;
    if (key === "progress") {
      // Absolute scale: the progress head lives in ~0–1 and its raw value is what
      // auto_stop's stable_min/engage_below are tuned against.
      min = Math.min(0, ...vals);
      span = Math.max(1, ...vals) - min;
    } else {
      min = Math.min(...vals);
      span = Math.max(...vals) - min || 1;
    }
    let d = "";
    let pen = "M";
    for (let i = 0; i < ys.length; i++) {
      if (ys[i] === null) {
        pen = "M"; // gap (null sample) — lift the pen instead of bridging
        continue;
      }
      const x = (i / (ys.length - 1)) * VBW;
      const y = toY(/** @type {number} */ (ys[i]), min, span);
      d += `${pen}${x.toFixed(1)} ${y.toFixed(1)} `;
      pen = "L";
    }

    // Labeled gridlines for the one absolutely-scaled series, so a progress
    // value can be read off the chart (not just its shape).
    if (key === "progress") {
      for (const v of [0, 0.5, 1]) {
        const y = toY(v, min, span);
        const grid = document.createElementNS(SVG_NS, "line");
        grid.setAttribute("x1", "0");
        grid.setAttribute("x2", String(VBW));
        grid.setAttribute("y1", y.toFixed(1));
        grid.setAttribute("y2", y.toFixed(1));
        grid.setAttribute("stroke", color);
        grid.setAttribute("stroke-opacity", "0.25");
        grid.setAttribute("stroke-dasharray", "4 4");
        grid.setAttribute("vector-effect", "non-scaling-stroke");
        svg.appendChild(grid);
        const tick = document.createElement("span");
        tick.className = "trace-ytick mono";
        tick.style.top = `${(y / VBH) * 100}%`;
        tick.style.color = color;
        tick.textContent = v.toFixed(1);
        plot.appendChild(tick);
      }
    }

    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", d.trim());
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", color);
    path.setAttribute("stroke-width", "1.5");
    path.setAttribute("vector-effect", "non-scaling-stroke");
    svg.appendChild(path);
    drawn.push({ key, name, color, ys });

    const item = document.createElement("span");
    item.className = "legend-item";
    const dot = document.createElement("span");
    dot.className = "legend-dot";
    dot.style.background = color;
    const lbl = document.createElement("span");
    lbl.textContent = name;
    item.append(dot, lbl);
    legend.appendChild(item);
  }
  plot.appendChild(svg);

  // Hover guide + raw-value readout, same pattern as the joints chart above it.
  const hoverLine = document.createElement("div");
  hoverLine.className = "telemetry-hoverline";
  hoverLine.hidden = true;
  const tip = document.createElement("div");
  tip.className = "telemetry-tip";
  tip.hidden = true;
  const tipTime = document.createElement("div");
  tipTime.className = "tip-time mono";
  tip.appendChild(tipTime);
  const tipVals = drawn.map((s) => {
    const row = document.createElement("div");
    row.className = "tip-row";
    const dot = document.createElement("span");
    dot.className = "legend-dot";
    dot.style.background = s.color;
    const lbl = document.createElement("span");
    lbl.className = "tip-label";
    lbl.textContent = s.name;
    const val = document.createElement("span");
    val.className = "tip-val mono";
    row.append(dot, lbl, val);
    tip.appendChild(row);
    return val;
  });
  plot.append(hoverLine, tip);

  const t0 = typeof samples[0].t === "number" ? samples[0].t : null;
  plot.addEventListener("pointermove", (e) => {
    const rect = plot.getBoundingClientRect();
    if (!rect.width) return;
    const frac = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    const idx = Math.round(frac * (samples.length - 1));
    hoverLine.style.left = `${frac * 100}%`;
    hoverLine.hidden = false;
    const rel = t0 != null && typeof samples[idx].t === "number" ? `${(samples[idx].t - t0).toFixed(1)}s · ` : "";
    tipTime.textContent = `${rel}#${idx}`;
    drawn.forEach((s, i) => {
      const v = s.ys[idx];
      tipVals[i].textContent = v == null ? "—" : fmtValue(s.key, v);
    });
    tip.style.left = `${frac * 100}%`;
    tip.classList.toggle("flip", frac > 0.6);
    tip.hidden = false;
  });
  plot.addEventListener("pointerleave", () => {
    hoverLine.hidden = true;
    tip.hidden = true;
  });

  const summary = document.createElement("span");
  summary.className = "microlabel";
  summary.textContent = summarize(samples, context);

  el.append(head, plot, summary);
  el.hidden = false;
}

/** @param {any[]} samples @param {any} context */
function summarize(samples, context) {
  const total = samples.map((s) => s.total_ms).filter((v) => typeof v === "number" && isFinite(v));
  const mean = total.length ? total.reduce((a, b) => a + b, 0) / total.length : 0;
  const progress = samples.map((s) => s.progress).filter((v) => typeof v === "number" && isFinite(v));
  const parts = [`${samples.length} steps`];
  const t0 = samples[0].t;
  const tN = samples[samples.length - 1].t;
  if (typeof t0 === "number" && typeof tN === "number" && tN > t0) parts.push(`${(tN - t0).toFixed(1)}s`);
  parts.push(`${mean.toFixed(1)} ms/step avg`);
  if (progress.length) parts.push(`final progress ${progress[progress.length - 1].toFixed(2)}`);
  const skill = context?.skill?.skill_name || context?.skill?.primitive_name || context?.skill?.skill_id;
  if (skill) parts.push(`skill: ${skill}`);
  return parts.join(" · ");
}
