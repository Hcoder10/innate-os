// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Inference-profile trace for the episode player: when an episode was captured
// during a policy rollout, profile_recorder saved the per-step profile stream
// next to the HDF5 (GET /episode/profile). This renders it as one compact
// overlay chart (each series normalized to its own range) plus a summary line,
// so a saved rollout can be judged — progress shape, motion, latency — right
// beside its video. Hidden entirely for episodes with no trace (teleop demos,
// pre-feature recordings).

const SVG_NS = "http://www.w3.org/2000/svg";

// series key → [label, color]; drawn/legended only when present in the data.
const SERIES = [
  ["progress", ["progress", "#e0a03c"]],
  ["arm_jerk", ["arm motion", "#5aa9e6"]],
  ["base_speed", ["base speed", "#7bc98f"]],
  ["total_ms", ["step ms", "#b58ce6"]],
  ["disagreement", ["disagreement", "#e66a6a"]],
];

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
  svg.setAttribute("viewBox", "0 0 1000 200");
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("class", "telemetry-svg");

  for (const [key, [name, color]] of SERIES) {
    const ys = samples.map((s) => (typeof s[key] === "number" && isFinite(s[key]) ? s[key] : null));
    const vals = ys.filter((v) => v !== null);
    if (vals.length < 2) continue;
    const min = Math.min(...vals);
    const span = Math.max(...vals) - min || 1;
    let d = "";
    let pen = "M";
    for (let i = 0; i < ys.length; i++) {
      if (ys[i] === null) {
        pen = "M"; // gap (null sample) — lift the pen instead of bridging
        continue;
      }
      const x = (i / (ys.length - 1)) * 1000;
      const y = 200 - ((ys[i] - min) / span) * 190 - 5;
      d += `${pen}${x.toFixed(1)} ${y.toFixed(1)} `;
      pen = "L";
    }
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", d.trim());
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", color);
    path.setAttribute("stroke-width", "1.5");
    path.setAttribute("vector-effect", "non-scaling-stroke");
    svg.appendChild(path);

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
  const parts = [`${samples.length} steps`, `${mean.toFixed(1)} ms/step avg`];
  if (progress.length) parts.push(`final progress ${progress[progress.length - 1].toFixed(2)}`);
  const skill = context?.skill?.skill_name || context?.skill?.primitive_name || context?.skill?.skill_id;
  if (skill) parts.push(`skill: ${skill}`);
  return parts.join(" · ");
}
