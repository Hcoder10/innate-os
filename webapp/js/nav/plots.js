// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Live strip charts for the Nav page — the rolling-time-series half of the
// telemetry sidebar. Three charts that answer questions the numeric readouts
// can't: is the base tracking what nav commands it (commanded vs measured,
// overlaid), and is an obstacle closing in.
//
// Canvas, not SVG: the episode/profiling graphs are SVG because they render a
// fixed recorded trace once, while these redraw ~15x/s forever — the same
// reason mapWidget is a canvas. No chart library; a strip chart is a polyline
// and two labels.
//
// Samples are timestamped on ARRIVAL, not from header.stamp: rws throttles
// server-side, and the robot's clock isn't guaranteed to agree with the
// browser's, so a header-stamped x-axis would drift or jump. Arrival time is
// what "live" honestly means here.

import { ros } from "../rosClient.js";
import { createVelocityTracker } from "./odomVelocity.js";
import { CMD_VEL_TOPIC, ODOM_TOPIC, SCAN_TOPIC } from "../constants.js";

const WINDOW_MS = 30_000;
// A series that stops publishing (e.g. /cmd_vel goes silent the moment teleop
// releases) must break the line, not bridge the silence with a straight
// segment that implies a command that was never sent.
const GAP_MS = 1_000;
const MAX_FPS = 15;

/** Series palette, matching the repo's existing trace colors (profileTrace.js). */
const CMD_COLOR = "#e0a03c"; // amber — commanded
const MEASURED_COLOR = "#5aa9e6"; // blue — measured
const OBSTACLE_COLOR = "#e66a6a"; // red — nearest obstacle

/**
 * @typedef {{ key: string, name: string, color: string, topic: string }} SeriesSpec
 * @typedef {{ label: string, unit: string, zeroed: boolean, minSpan: number, series: SeriesSpec[] }} ChartSpec
 */

/** @type {Record<string, ChartSpec>} */
const CHARTS = {
  linear: {
    label: "linear velocity",
    unit: "m/s",
    zeroed: true,
    minSpan: 0.4,
    series: [
      { key: "cmd", name: "commanded", color: CMD_COLOR, topic: CMD_VEL_TOPIC },
      { key: "measured", name: "measured", color: MEASURED_COLOR, topic: `${ODOM_TOPIC} (differentiated)` },
    ],
  },
  angular: {
    label: "angular velocity",
    unit: "°/s",
    zeroed: true,
    minSpan: 30,
    series: [
      { key: "cmd", name: "commanded", color: CMD_COLOR, topic: CMD_VEL_TOPIC },
      { key: "measured", name: "measured", color: MEASURED_COLOR, topic: `${ODOM_TOPIC} (differentiated)` },
    ],
  },
  obstacle: {
    label: "nearest obstacle",
    unit: "m",
    zeroed: false,
    minSpan: 1,
    series: [{ key: "scan", name: "lidar min", color: OBSTACLE_COLOR, topic: SCAN_TOPIC }],
  },
};

/** @param {number} rad */
function deg(rad) {
  return (rad * 180) / Math.PI;
}

/**
 * Round up to a 1/2/5 x 10^n bound, so the axis reads in human numbers.
 * @param {number} v
 */
function niceCeil(v) {
  if (!(v > 0)) return 1;
  const base = 10 ** Math.floor(Math.log10(v));
  const n = v / base;
  return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * base;
}

/**
 * One strip chart: a titled panel with a legend and a rolling canvas.
 * @param {HTMLElement} parent
 * @param {ChartSpec} spec
 */
function createChart(parent, spec) {
  const section = document.createElement("section");
  section.className = "nav-panel";

  const head = document.createElement("div");
  head.className = "telemetry-head";
  const label = document.createElement("p");
  label.className = "microlabel";
  label.textContent = spec.label;
  label.title = [...new Set(spec.series.map((s) => s.topic))].join(" · ");
  const legend = document.createElement("div");
  legend.className = "telemetry-legend";
  for (const s of spec.series) {
    const item = document.createElement("span");
    item.className = "legend-item";
    item.title = s.topic;
    const dot = document.createElement("span");
    dot.className = "legend-dot";
    dot.style.background = s.color;
    const name = document.createElement("span");
    name.textContent = s.name;
    item.append(dot, name);
    legend.appendChild(item);
  }
  head.append(label, legend);

  const plot = document.createElement("div");
  plot.className = "telemetry-plot nav-plot";
  plot.title = "rolling 30 s window";
  const canvas = document.createElement("canvas");
  canvas.className = "nav-plot-canvas";
  plot.appendChild(canvas);
  section.append(head, plot);
  parent.appendChild(section);

  const ctx = canvas.getContext("2d");
  /** Per-series ring of arrival-stamped samples. @type {Map<string, Array<{ t: number, v: number }>>} */
  const data = new Map(spec.series.map((s) => [s.key, []]));
  const dpr = () => window.devicePixelRatio || 1;

  function fit() {
    const r = plot.getBoundingClientRect();
    const d = dpr();
    canvas.width = Math.max(1, Math.floor(r.width * d));
    canvas.height = Math.max(1, Math.floor(r.height * d));
    canvas.style.width = `${r.width}px`;
    canvas.style.height = `${r.height}px`;
  }

  /**
   * Drop samples that scrolled off the left edge; keep one extra so the
   * line still enters from off-canvas rather than starting mid-plot.
   * @param {Array<{ t: number, v: number }>} buf @param {number} from
   */
  function prune(buf, from) {
    let keep = 0;
    while (keep < buf.length && buf[keep].t < from) keep++;
    if (keep > 1) buf.splice(0, keep - 1);
  }

  /** @param {string} key @param {number} v */
  function push(key, v) {
    const buf = data.get(key);
    if (!buf || typeof v !== "number" || !Number.isFinite(v)) return;
    const now = performance.now();
    buf.push({ t: now, v });
    // Prune on arrival as well as on draw: rAF stops painting on a hidden
    // tab while the socket keeps delivering, and draw()'s prune stops with it.
    prune(buf, now - WINDOW_MS);
  }

  /** @param {number} now */
  function draw(now) {
    if (!ctx) return;
    const from = now - WINDOW_MS;
    for (const buf of data.values()) prune(buf, from);

    const w = canvas.width;
    const h = canvas.height;
    ctx.clearRect(0, 0, w, h);

    // Auto-scale to the visible data: zeroed charts stay symmetric about 0 so
    // forward/reverse read at the same scale; others grow from 0.
    let peak = 0;
    for (const buf of data.values()) {
      for (const s of buf) {
        if (s.t < from) continue;
        peak = Math.max(peak, Math.abs(s.v));
      }
    }
    const bound = niceCeil(Math.max(peak, spec.minSpan / (spec.zeroed ? 2 : 1)));
    const top = bound;
    const bottom = spec.zeroed ? -bound : 0;
    const pad = 4 * dpr();
    /** @param {number} v */
    const toY = (v) => h - pad - ((v - bottom) / (top - bottom)) * (h - 2 * pad);
    /** @param {number} t */
    const toX = (t) => ((t - from) / WINDOW_MS) * w;

    // Zero baseline — the reference the velocity traces are read against.
    const zeroY = Math.round(toY(0)) + 0.5;
    ctx.strokeStyle = "rgb(255 255 255 / 12%)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, zeroY);
    ctx.lineTo(w, zeroY);
    ctx.stroke();

    for (const s of spec.series) {
      const buf = data.get(s.key);
      if (!buf || buf.length === 0) continue;
      ctx.strokeStyle = s.color;
      ctx.lineWidth = 1.5 * dpr();
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      ctx.beginPath();
      let pen = false;
      for (let i = 0; i < buf.length; i++) {
        const p = buf[i];
        const prev = buf[i - 1];
        // Silence longer than GAP_MS is a real absence of data — lift the pen.
        if (prev && p.t - prev.t > GAP_MS) pen = false;
        const x = toX(p.t);
        const y = toY(p.v);
        if (!pen) {
          ctx.moveTo(x, y);
          pen = true;
        } else {
          ctx.lineTo(x, y);
        }
      }
      ctx.stroke();
      // Dot at the live edge, but only while the series is actually publishing.
      const last = buf[buf.length - 1];
      if (now - last.t <= GAP_MS) {
        ctx.fillStyle = s.color;
        ctx.beginPath();
        ctx.arc(toX(last.t), toY(last.v), 2 * dpr(), 0, Math.PI * 2);
        ctx.fill();
      }
    }

    // Axis bounds, top-left/bottom-left — enough to read the scale without a
    // full axis furniture set.
    ctx.fillStyle = "#8a8a93";
    ctx.font = `${9 * dpr()}px ui-monospace, monospace`;
    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    ctx.fillText(`${top}${spec.unit}`, 3 * dpr(), 2 * dpr());
    ctx.textBaseline = "bottom";
    ctx.fillText(`${bottom}${spec.unit}`, 3 * dpr(), h - 2 * dpr());
  }

  const ro = new ResizeObserver(fit);
  ro.observe(plot);
  fit();

  return {
    push,
    draw,
    destroy() {
      ro.disconnect();
      section.remove();
    },
  };
}

/**
 * Mount the Nav page's live plots and wire them to the nav topics.
 * @param {HTMLElement} root the sidebar container.
 * @returns {{ destroy: () => void }}
 */
export function createNavPlots(root) {
  const linear = createChart(root, CHARTS.linear);
  const angular = createChart(root, CHARTS.angular);
  const obstacle = createChart(root, CHARTS.obstacle);

  // Odometry.twist is never populated by this robot, so measured motion is
  // differentiated from the pose (see odomVelocity.js).
  const velTracker = createVelocityTracker();

  /** @type {Array<() => void>} */
  const unsubs = [
    // Measured motion. Throttles match the readout panel's, so the two share
    // one ref-counted rosbridge subscription per topic rather than doubling it.
    ros.subscribe(
      ODOM_TOPIC,
      (msg) => {
        const measured = velTracker.update(msg);
        if (!measured) return; // no usable interval yet — leave a gap, don't plot a zero
        linear.push("measured", measured.v);
        angular.push("measured", deg(measured.w));
      },
      100,
    ),
    // What the cmd_vel mux actually sent the base — silent while idle.
    ros.subscribe(
      CMD_VEL_TOPIC,
      (msg) => {
        linear.push("cmd", msg?.linear?.x);
        const w = msg?.angular?.z;
        if (typeof w === "number") angular.push("cmd", deg(w));
      },
      100,
      "geometry_msgs/msg/Twist",
    ),
    ros.subscribe(
      SCAN_TOPIC,
      (msg) => {
        const ranges = msg?.ranges;
        if (!Array.isArray(ranges)) return;
        let best = Infinity;
        for (const r of ranges) {
          if (Number.isFinite(r) && r >= msg.range_min && r <= msg.range_max && r < best) best = r;
        }
        if (best !== Infinity) obstacle.push("scan", best);
      },
      150,
      "sensor_msgs/msg/LaserScan",
    ),
  ];

  // One shared loop drives all three: the x-axis scrolls with wall time, so a
  // chart must repaint even when no message arrived. rAF (not setInterval) so
  // a hidden tab stops painting entirely — the buffers just prune on return.
  let raf = 0;
  let last = 0;
  const charts = [linear, angular, obstacle];
  /** @param {number} now */
  function frame(now) {
    raf = requestAnimationFrame(frame);
    if (now - last < 1000 / MAX_FPS) return;
    last = now;
    for (const c of charts) c.draw(performance.now());
  }
  raf = requestAnimationFrame(frame);

  return {
    destroy() {
      cancelAnimationFrame(raf);
      for (const unsub of unsubs) unsub();
      for (const c of charts) c.destroy();
    },
  };
}
