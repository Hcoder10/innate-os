// @ts-check
// Reusable nav-map widget — a plain 2D <canvas> rendering the occupancy grid
// (/map), robot pose (/odom), planner route (/plan), and click-to-navigate
// (sends a NavigateToPose goal via the router), plus opt-in overlay layers
// (lidar scan / global costmap / local costmap / odometry trail / spatial
// memories) for the Nav page. Sizes itself to its container via a
// ResizeObserver, so it can be the standalone Map page OR a teleop PiP tile
// that reparents between a small thumbnail and the full stage. No three.js —
// a canvas + putImageData is all a 2D map needs.

import { ros } from "../rosClient.js";
import {
  AMCL_POSE_TOPIC,
  MAP_TOPIC,
  ODOM_TOPIC,
  PLAN_TOPICS,
  COMMANDED_GOAL_TOPIC,
  CANCEL_NAVIGATION_SERVICE,
  LOCALIZE_SERVICE,
  SET_INITIAL_POSE_SERVICE,
  SCAN_TOPIC,
  GLOBAL_COSTMAP_TOPIC,
  LOCAL_COSTMAP_TOPIC,
  TF_STATIC_TOPIC,
  MAPPING_POSE_TOPIC,
  MEMORY_POSITIONS_TOPIC,
  MEMORY_SEARCH_TOPIC,
} from "../constants.js";
import { MEMORY_COLOR, ageAlpha, ageText, memoryImageUrl, parseMemories, parseSearch, withAlpha } from "./memories.js";

// Overlay palette — exported so the Nav page legend shows the same colors.
// The robot is deliberately far from the costmap ramp (blue → amber → red):
// it used to be the same amber as the inscribed band and vanished into it.
export const MAP_COLORS = {
  robot: "#ff5ce1",
  trail: "rgb(255 92 225 / 45%)",
  goal: "#00ff88",
  route: "#00b7ff",
  scan: "#d96a5a",
  memory: MEMORY_COLOR,
  costLethal: "rgb(217 106 90 / 82%)",
  costInscribed: "rgb(232 163 61 / 75%)",
  costInflation: "rgb(77 124 254 / 45%)",
};

// Robot marker radius in metres — matches the footprint half-width (0.165 m
// in costmap.yaml), so the dot covers what the robot covers at every zoom.
const ROBOT_RADIUS_M = 0.18;

// /localize scan-matches for up to ~30 s before answering.
const LOCALIZE_TIMEOUT_MS = 40_000;

// Wheel-zoom bounds (metres of real-world width shown).
const MIN_ZOOM_M = 1;
const MAX_ZOOM_M = 60;
const ZOOM_STEP = 1.15; // per wheel notch

// Odometry breadcrumb trail: drop a point every few cm, keep the last N.
const TRAIL_MIN_STEP_M = 0.03;
const TRAIL_MAX_POINTS = 600;
// A step this large between consecutive odom samples (throttled to 100 ms) is
// a relocalization — Locate, manual placement, or a map switch, from any
// client — not motion. The history belongs to the previous frame: restart.
const TRAIL_JUMP_M = 1;

/**
 * @typedef {"scan" | "costmap" | "local" | "trail" | "memories"} LayerName
 */

// ---- spatial-memory layer tuning -------------------------------------------
// A stylized view cone per remembered frame: wide enough to read as "the robot
// looked this way", far narrower than the camera's real 128° FOV (which would
// wash the map in overlapping wedges).
const MEM_WEDGE_HALF_RAD = (28 * Math.PI) / 180;
const MEM_WEDGE_RANGE_M = 1.15;
const MEM_HIT_PX = 16; // hover/click hit radius around a memory dot (css px)
const MEM_PULSE_MS = 1200; // ring animation when a new memory is recorded
// A latched search verdict older than this is history, not news — show nothing.
const MEM_SEARCH_FRESH_S = 20;
const MEM_SEARCH_GLOW_MS = 18_000; // how long the recalled spot keeps breathing
const MEM_SEARCH_CARD_MS = 14_000; // the verdict card auto-dismisses

/**
 * Rasterize a nav_msgs/OccupancyGrid into `canvas` at 1px per cell, flipped
 * so canvas-top is the highest world-y. `paint` colors one cell: it writes
 * RGBA at px[di..di+3], or leaves it untouched for a transparent cell.
 * Shared by the map grid and the costmap overlay, which differ only in
 * colormap.
 * @param {any} msg
 * @param {HTMLCanvasElement} canvas 1px-per-cell offscreen buffer, resized to fit.
 * @param {CanvasRenderingContext2D | null} ctx the canvas's 2d context.
 * @param {(v: number, px: Uint8ClampedArray, di: number) => void} paint
 * @returns {{ width: number, height: number, resolution: number, originX: number, originY: number } | null}
 *   the grid's world placement, or null if the message isn't a usable grid.
 */
function rasterizeGrid(msg, canvas, ctx, paint) {
  const info = msg?.info;
  const data = msg?.data;
  const width = info?.width | 0;
  const height = info?.height | 0;
  if (!ctx || !info || !Array.isArray(data) || width <= 0 || height <= 0 || data.length < width * height) return null;
  canvas.width = width;
  canvas.height = height;
  const img = ctx.createImageData(width, height);
  for (let row = 0; row < height; row++) {
    const srcRow = height - 1 - row; // flip so canvas-top = highest world-y
    for (let col = 0; col < width; col++) {
      paint(data[srcRow * width + col], img.data, (row * width + col) * 4);
    }
  }
  ctx.putImageData(img, 0, 0);
  return {
    width,
    height,
    resolution: info.resolution || 0.05,
    originX: info.origin?.position?.x ?? 0,
    originY: info.origin?.position?.y ?? 0,
  };
}

/**
 * @param {HTMLElement} root container the map fills (sized via ResizeObserver).
 * @param {{ zoom?: number, onZoomChange?: (meters: number) => void, layers?: Partial<Record<LayerName, boolean>> }} [opts]
 *   zoom = metres of real-world width to show, centred on the robot pose (keeps the map legible when
 *   small); enables scroll-to-zoom. Omit to fit the whole grid (the standalone page). onZoomChange
 *   fires after each wheel-zoom. layers turns on optional overlays (Nav page): live lidar scan,
 *   global/local costmaps, odometry trail — each adds its subscription only while enabled.
 * @returns {{ destroy: () => void, setZoom: (meters: number) => void, setLayer: (name: LayerName, on: boolean) => void, setMappingMode: (on: boolean) => void, clearTrail: () => void, mapChanged: () => void, highlightMemory: (id: number | null) => void, focusMemory: (id: number) => void }}
 */
export function createMap(root, opts = {}) {
  let zoomMeters = opts.zoom;
  const canvas = document.createElement("canvas");
  canvas.className = "map-canvas";
  root.appendChild(canvas);
  const ctx = canvas.getContext("2d");

  // Controls mirror the mobile app's map: Locate expands to Auto/Manual,
  // Go To arms drag-to-navigate, Stop takes Go To's place while navigating.
  const ICONS = {
    back: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M10 3L5 8l5 5"/></svg>',
    locate:
      '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"><circle cx="8" cy="8" r="4.5"/><path d="M8 .8v2.4M8 12.8v2.4M.8 8h2.4M12.8 8h2.4"/></svg>',
    auto: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"><circle cx="8" cy="8" r="6"/><circle cx="8" cy="8" r="1.6" fill="currentColor" stroke="none"/></svg>',
    manual:
      '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M3 13l1-3.5L11.5 2 14 4.5 6.5 12 3 13z"/></svg>',
    go: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M14.5 1.5L1.5 7l5 2 2 5 6-12.5z"/></svg>',
    stop: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"><rect x="4" y="4" width="8" height="8" rx="1"/></svg>',
    center:
      '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"><circle cx="8" cy="8" r="4.5"/><circle cx="8" cy="8" r="1.5" fill="currentColor" stroke="none"/><path d="M8 .8v2.4M8 12.8v2.4M.8 8h2.4M12.8 8h2.4"/></svg>',
  };
  /** @param {string} icon @param {string} label @param {string} [hint] */
  function makeButton(icon, label, hint = "") {
    const btn = document.createElement("button");
    btn.className = "map-btn";
    btn.innerHTML =
      `<span class="map-btn-icon">${icon}</span>` +
      (label ? `<span class="map-btn-label">${label}</span>` : "") +
      `<span class="map-btn-hint">${hint}</span>`;
    return btn;
  }
  /** @param {HTMLButtonElement} btn @param {string} text */
  function setHint(btn, text) {
    const el = btn.querySelector(".map-btn-hint");
    if (el && el.textContent !== text) el.textContent = text;
  }
  const backBtn = makeButton(ICONS.back, "");
  backBtn.classList.add("map-btn-compact");
  backBtn.title = "Back";
  const locateBtn = makeButton(ICONS.locate, "Locate", "auto or manual");
  locateBtn.title = `${LOCALIZE_SERVICE} · ${SET_INITIAL_POSE_SERVICE}`;
  const autoBtn = makeButton(ICONS.auto, "Auto", "match lidar to the map");
  autoBtn.title = LOCALIZE_SERVICE;
  const manualBtn = makeButton(ICONS.manual, "Manual", "drag where the robot is");
  manualBtn.title = SET_INITIAL_POSE_SERVICE;
  const goBtn = makeButton(ICONS.go, "Go To", "tap a point to navigate");
  goBtn.title = "/navigate_to_pose (action)";
  const stopBtn = makeButton(ICONS.stop, "Stop", "cancel navigation");
  stopBtn.title = CANCEL_NAVIGATION_SERVICE;
  const centerBtn = makeButton(ICONS.center, "Recenter", "follow the robot");
  const controlsRow = document.createElement("div");
  controlsRow.className = "map-controls-row";
  for (const btn of [backBtn, locateBtn, autoBtn, manualBtn, goBtn, stopBtn, centerBtn]) controlsRow.appendChild(btn);
  // Live progress / outcome of the widget's own goals and locate calls.
  const statusEl = document.createElement("div");
  statusEl.className = "map-status mono";
  statusEl.hidden = true;
  const controls = document.createElement("div");
  controls.className = "map-controls";
  controls.appendChild(controlsRow);
  controls.appendChild(statusEl);
  root.appendChild(controls);

  let goalGen = 0; // ignore settlements of superseded goals
  /** @type {ReturnType<typeof setTimeout> | undefined} */
  let statusClearTimer;
  /** @param {"navigating" | "ok" | "fail" | "muted" | "hint"} kind @param {string} text @param {boolean} [autoclear] */
  function setStatus(kind, text, autoclear = false) {
    clearTimeout(statusClearTimer);
    statusEl.hidden = false;
    statusEl.dataset.kind = kind;
    statusEl.textContent = text;
    if (autoclear) {
      statusClearTimer = setTimeout(() => {
        statusEl.hidden = true;
      }, 8000);
    }
  }

  // Offscreen 1px-per-cell buffer; scaled to the canvas on draw (crisp + cheap).
  const off = document.createElement("canvas");
  const offCtx = off.getContext("2d");

  /** @type {{ width: number, height: number, resolution: number, originX: number, originY: number } | null} */
  let grid = null;
  /** @type {{ x: number, y: number, yaw: number } | null} displayed robot pose (map frame) */
  let pose = null;
  // Robot marker in the MAP frame: anchor at the last /amcl_pose fix and add
  // the odometry delta since (raw /odom is off by the whole map->odom
  // correction on a real robot). No AMCL yet -> raw odom is the global frame.
  /** @type {{ x: number, y: number, yaw: number } | null} */
  let amclPose = null;
  /** @type {{ x: number, y: number, yaw: number } | null} odom pose at the AMCL fix */
  let odomAtAmcl = null;
  /** @type {{ x: number, y: number, yaw: number } | null} latest raw odom */
  let odomPose = null;

  // While the robot is building a map (SLAM), the only pose that's valid
  // against the growing grid is mode_manager's /mapping_pose (map frame, from
  // TF): raw odom drifts against it and any AMCL fix predates the map.
  let mappingMode = false;
  /** @type {{ x: number, y: number, yaw: number } | null} */
  let mappingPose = null;
  /** @type {(() => void) | null} */
  let unsubMappingPose = null;

  function composedPose() {
    if (mappingMode) return mappingPose ?? odomPose;
    if (!amclPose) return odomPose;
    if (!odomAtAmcl || !odomPose) return amclPose;
    // Motion since the fix in the base frame, re-applied at the AMCL fix.
    const ca = Math.cos(-odomAtAmcl.yaw);
    const sa = Math.sin(-odomAtAmcl.yaw);
    const dxo = odomPose.x - odomAtAmcl.x;
    const dyo = odomPose.y - odomAtAmcl.y;
    const dx = dxo * ca - dyo * sa;
    const dy = dxo * sa + dyo * ca;
    const dyaw = odomPose.yaw - odomAtAmcl.yaw;
    const c = Math.cos(amclPose.yaw);
    const s = Math.sin(amclPose.yaw);
    return { x: amclPose.x + dx * c - dy * s, y: amclPose.y + dx * s + dy * c, yaw: amclPose.yaw + dyaw };
  }
  /** @type {Array<{ x: number, y: number }> | null} world-frame plan points */
  let plan = null;

  // ---- optional overlay layers (Nav page) ---------------------------------
  /** @type {Record<LayerName, boolean>} */
  const layers = { scan: false, costmap: false, local: false, trail: false, memories: false, ...opts.layers };
  /** @type {any} latest sensor_msgs/LaserScan */
  let scanMsg = null;
  // base_link -> base_laser from /tf_static (URDF); zero until it arrives.
  let laserOffset = { x: 0, y: 0, yaw: 0 };
  // Costmaps rendered like the map: 1px-per-cell offscreen + world placement.
  const costOff = document.createElement("canvas");
  const costOffCtx = costOff.getContext("2d");
  /** @type {{ width: number, height: number, resolution: number, originX: number, originY: number } | null} */
  let costGrid = null;
  // The controller's rolling local costmap — same rendering, odom frame.
  const localOff = document.createElement("canvas");
  const localOffCtx = localOff.getContext("2d");
  /** @type {{ width: number, height: number, resolution: number, originX: number, originY: number } | null} */
  let localGrid = null;
  /** @type {Array<{ x: number, y: number }>} map-frame breadcrumb trail */
  const trail = [];
  /** @type {Partial<Record<"scan" | "costmap" | "local" | "tf" | "memsearch", () => void>>} live layer subscriptions */
  const layerUnsubs = {};

  // ---- spatial-memory layer state -----------------------------------------
  /** @type {import("./memories.js").MemoryState | null} */
  let memState = null;
  /** @type {Set<number> | null} ids seen so far; null until the first payload
   * (which adopts everything silently — a page load must not pulse the world) */
  let memKnown = null;
  /** @type {Map<number, number>} memory id -> performance.now() of its pulse-in */
  const memPulses = new Map();
  /** @type {import("./memories.js").Memory | null} memory under the cursor */
  let memHover = null;
  /** @type {number | null} memory highlighted from outside (gallery hover) */
  let memHighlight = null;
  /** @type {number | null} memory whose popup card is open */
  let memPopupFor = null;
  /** @type {ReturnType<typeof parseSearch>} latest fresh search verdict */
  let memSearch = null;
  let memSearchUntil = 0; // performance.now() deadline for the recalled-spot glow
  /** @type {ReturnType<typeof setTimeout> | undefined} */
  let memSearchCardTimer;

  // The three DOM overlays: a hover thumbnail, a pinned popup (Go here), and
  // the search-verdict card. All positioned within `root` over the canvas.
  const memHoverCard = document.createElement("div");
  memHoverCard.className = "mem-card mem-card-hover";
  memHoverCard.hidden = true;
  root.appendChild(memHoverCard);
  const memPopup = document.createElement("div");
  memPopup.className = "mem-card mem-card-popup";
  memPopup.hidden = true;
  root.appendChild(memPopup);
  const memSearchCard = document.createElement("div");
  memSearchCard.className = "mem-search-card";
  memSearchCard.hidden = true;
  root.appendChild(memSearchCard);

  /** @param {any} msg std_msgs/String — /brain/memory_positions */
  function onMemories(msg) {
    const next = parseMemories(msg);
    if (!next) return;
    if (memKnown === null || (memState && memState.map !== next.map)) {
      memKnown = new Set(next.memories.map((m) => m.id));
      memPulses.clear();
      closeMemPopup();
      // A recall verdict belongs to the map it was answered on — a switch
      // must not leave its glow pointing into the new map's frame.
      memSearch = null;
      hideMemSearchCard();
    } else {
      for (const m of next.memories) {
        if (memKnown.has(m.id)) continue;
        memKnown.add(m.id);
        if (layers.memories) memPulses.set(m.id, performance.now());
      }
    }
    memState = next;
    if (memHover && !next.memories.some((m) => m.id === memHover?.id)) setMemHover(null);
    if (memPopupFor !== null && !next.memories.some((m) => m.id === memPopupFor)) closeMemPopup();
    kickMemAnim();
    draw();
  }

  // The first memsearch message after subscribing is the latched replay:
  // history, shown only while fresh. Later arrivals are live news — never
  // gated on the stamp, because the robot's wall clock can disagree with the
  // browser's (no RTC before NTP settles) by more than the freshness window.
  let memSearchSeen = false;
  /** @param {any} msg std_msgs/String — /brain/memory_search (latched) */
  function onMemorySearch(msg) {
    const verdict = parseSearch(msg);
    if (!verdict) return;
    const replay = !memSearchSeen;
    memSearchSeen = true;
    if (replay && Date.now() / 1000 - verdict.stamp > MEM_SEARCH_FRESH_S) return; // stale latched replay
    memSearch = verdict;
    memSearchUntil = performance.now() + (verdict.found ? MEM_SEARCH_GLOW_MS : 0);
    showMemSearchCard(verdict);
    kickMemAnim();
    draw();
  }

  // rAF ticker for the layer's animations (pulse-ins, the recalled-spot glow).
  // Runs only while something is animating; every other redraw stays event-driven.
  let memAnimFrame = 0;
  function memAnimActive() {
    const now = performance.now();
    for (const born of memPulses.values()) if (now - born < MEM_PULSE_MS) return true;
    return Boolean(memSearch?.found) && now < memSearchUntil;
  }
  function kickMemAnim() {
    if (memAnimFrame) return;
    const step = () => {
      memAnimFrame = 0;
      draw();
      if (memAnimActive()) memAnimFrame = requestAnimationFrame(step);
    };
    memAnimFrame = requestAnimationFrame(step);
  }

  /** Nearest memory within the hit radius of a canvas point, or null.
   * @param {number} px @param {number} py */
  function hitMemory(px, py) {
    if (!layers.memories || !memState || !grid || !view) return null;
    let best = null;
    let bestD = MEM_HIT_PX * dpr();
    for (const m of memState.memories) {
      const c = worldToCanvas(m.x, m.y);
      const dist = Math.hypot(c.px - px, c.py - py);
      if (dist < bestD) {
        bestD = dist;
        best = m;
      }
    }
    return best;
  }

  /** Anchor a card next to a memory's dot, clamped inside the scene.
   * @param {HTMLElement} el @param {import("./memories.js").Memory} m */
  function placeMemCard(el, m) {
    if (!grid || !view) return;
    const d = dpr();
    const { px, py } = worldToCanvas(m.x, m.y);
    const x = px / d;
    const y = py / d;
    const left = Math.min(Math.max(8, x + 16), Math.max(8, root.clientWidth - el.offsetWidth - 8));
    const above = y - el.offsetHeight - 16;
    el.style.left = `${left}px`;
    el.style.top = `${above > 8 ? above : Math.min(y + 16, Math.max(8, root.clientHeight - el.offsetHeight - 8))}px`;
  }

  /** @param {import("./memories.js").Memory | null} m */
  function setMemHover(m) {
    if (memHover?.id === m?.id) return;
    memHover = m;
    render();
    if (!m || !memState || memPopupFor !== null) {
      memHoverCard.hidden = true;
      draw();
      return;
    }
    memHoverCard.innerHTML = "";
    const img = new Image();
    img.className = "mem-card-img";
    img.alt = "remembered view";
    img.src = memoryImageUrl(memState.map, m);
    const meta = document.createElement("div");
    meta.className = "mem-card-meta mono";
    meta.textContent = `seen ${ageText(m.stamp)} · click for options`;
    memHoverCard.append(img, meta);
    memHoverCard.hidden = false;
    placeMemCard(memHoverCard, m);
    draw();
  }

  /** @param {import("./memories.js").Memory} m */
  function openMemPopup(m) {
    if (!memState) return;
    memPopupFor = m.id;
    memHoverCard.hidden = true;
    memPopup.innerHTML = "";
    const img = new Image();
    img.className = "mem-card-img mem-card-img-lg";
    img.alt = "remembered view";
    img.src = memoryImageUrl(memState.map, m);
    const meta = document.createElement("div");
    meta.className = "mem-card-meta mono";
    meta.textContent = `seen ${ageText(m.stamp)} · x ${m.x.toFixed(2)} m, y ${m.y.toFixed(2)} m`;
    const actions = document.createElement("div");
    actions.className = "mem-card-actions";
    const goHere = document.createElement("button");
    goHere.type = "button";
    goHere.className = "mem-card-btn mem-card-btn-go";
    goHere.textContent = "Go here";
    goHere.title = "/navigate_to_pose (action) — drive to this remembered viewpoint";
    goHere.addEventListener("click", () => {
      closeMemPopup();
      publishGoal(m.x, m.y, m.theta);
    });
    const close = document.createElement("button");
    close.type = "button";
    close.className = "mem-card-btn";
    close.textContent = "Close";
    close.addEventListener("click", closeMemPopup);
    actions.append(goHere, close);
    memPopup.append(img, meta, actions);
    memPopup.hidden = false;
    placeMemCard(memPopup, m);
    draw();
  }

  function closeMemPopup() {
    if (memPopupFor === null) return;
    memPopupFor = null;
    memPopup.hidden = true;
    draw();
  }

  /** @param {NonNullable<ReturnType<typeof parseSearch>>} verdict */
  function showMemSearchCard(verdict) {
    clearTimeout(memSearchCardTimer);
    memSearchCard.innerHTML = "";
    memSearchCard.classList.toggle("is-found", Boolean(verdict.found));
    const head = document.createElement("div");
    head.className = "mem-search-head";
    const label = document.createElement("span");
    label.className = "microlabel";
    label.textContent = "memory recall";
    const query = document.createElement("span");
    query.className = "mem-search-query";
    query.textContent = `“${verdict.query}”`;
    head.append(label, query);
    if (typeof verdict.latency_sec === "number") {
      const chip = document.createElement("span");
      chip.className = "mem-search-chip mono";
      chip.textContent = `${verdict.latency_sec.toFixed(1)} s${verdict.cached ? " · cached ⚡" : ""}`;
      head.appendChild(chip);
    }
    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "mem-search-close";
    closeBtn.textContent = "×";
    closeBtn.setAttribute("aria-label", "dismiss");
    closeBtn.addEventListener("click", hideMemSearchCard);
    head.appendChild(closeBtn);
    memSearchCard.appendChild(head);
    const body = document.createElement("div");
    body.className = "mem-search-body";
    if (verdict.found && memState) {
      const match = memState.memories.find((m) => m.id === verdict.id);
      if (match) {
        const img = new Image();
        img.className = "mem-search-img";
        img.alt = "recalled view";
        img.src = memoryImageUrl(memState.map, match);
        body.appendChild(img);
      }
    }
    const text = document.createElement("div");
    text.className = "mem-search-text";
    text.textContent = verdict.found
      ? verdict.explanation || "Found a matching remembered view."
      : verdict.error
        ? `Recall failed — ${verdict.error}`
        : verdict.explanation || "Nothing in the remembered views matches.";
    body.appendChild(text);
    memSearchCard.appendChild(body);
    memSearchCard.hidden = false;
    // Two frames so the transition runs from the hidden state.
    memSearchCard.classList.remove("is-in");
    requestAnimationFrame(() => requestAnimationFrame(() => memSearchCard.classList.add("is-in")));
    memSearchCardTimer = setTimeout(hideMemSearchCard, MEM_SEARCH_CARD_MS);
  }

  function hideMemSearchCard() {
    clearTimeout(memSearchCardTimer);
    memSearchCard.classList.remove("is-in");
    memSearchCard.hidden = true;
  }

  /** @param {any} msg sensor_msgs/LaserScan */
  function onScan(msg) {
    if (!Array.isArray(msg?.ranges)) return;
    scanMsg = msg;
    draw();
  }

  /** @param {any} msg tf2_msgs/TFMessage — pick out base_link -> base_laser */
  function onTfStatic(msg) {
    for (const t of msg?.transforms ?? []) {
      if (t?.child_frame_id !== "base_laser") continue;
      const tr = t.transform?.translation;
      const q = t.transform?.rotation;
      if (typeof tr?.x !== "number" || !q) continue;
      laserOffset = { x: tr.x, y: tr.y, yaw: Math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z)) };
      draw();
    }
  }

  /**
   * Nav2 cost colormap, transparent where free — the paint half of the two
   * costmap layers (global and local differ only in frame, not in color).
   * @param {number} v @param {Uint8ClampedArray} px @param {number} di
   */
  function paintCost(v, px, di) {
    if (v <= 0) return; // free/unknown stays transparent (alpha 0)
    if (v >= 100) {
      // Lethal — the obstacle itself.
      px[di] = 217;
      px[di + 1] = 106;
      px[di + 2] = 90;
      px[di + 3] = 210;
    } else if (v >= 99) {
      // Inscribed — the footprint would collide here.
      px[di] = 232;
      px[di + 1] = 163;
      px[di + 2] = 61;
      px[di + 3] = 190;
    } else {
      // Inflation gradient, fading with cost.
      px[di] = 77;
      px[di + 1] = 124;
      px[di + 2] = 254;
      px[di + 3] = 25 + Math.round((v / 98) * 115);
    }
  }

  /** @param {any} msg nav_msgs/OccupancyGrid (map frame) */
  function onCostmap(msg) {
    const g = rasterizeGrid(msg, costOff, costOffCtx, paintCost);
    if (!g) return;
    costGrid = g;
    draw();
  }

  /** @param {any} msg nav_msgs/OccupancyGrid (odom frame, rolling window) */
  function onLocalCostmap(msg) {
    const g = rasterizeGrid(msg, localOff, localOffCtx, paintCost);
    if (!g) return;
    localGrid = g;
    draw();
  }

  // Subscribe/unsubscribe to match the enabled layers, so an off layer costs
  // no bandwidth (the costmap especially — a map-sized grid as JSON).
  function syncLayerSubs() {
    if (layers.scan && !layerUnsubs.scan) {
      layerUnsubs.scan = ros.subscribe(SCAN_TOPIC, onScan, 150, "sensor_msgs/msg/LaserScan");
      layerUnsubs.tf = ros.subscribe(TF_STATIC_TOPIC, onTfStatic, 0, "tf2_msgs/msg/TFMessage");
    } else if (!layers.scan && layerUnsubs.scan) {
      layerUnsubs.scan();
      layerUnsubs.tf?.();
      delete layerUnsubs.scan;
      delete layerUnsubs.tf;
      scanMsg = null;
    }
    if (layers.costmap && !layerUnsubs.costmap) {
      layerUnsubs.costmap = ros.subscribe(GLOBAL_COSTMAP_TOPIC, onCostmap, 1000, "nav_msgs/msg/OccupancyGrid");
    } else if (!layers.costmap && layerUnsubs.costmap) {
      layerUnsubs.costmap();
      delete layerUnsubs.costmap;
      costGrid = null;
    }
    if (layers.local && !layerUnsubs.local) {
      layerUnsubs.local = ros.subscribe(LOCAL_COSTMAP_TOPIC, onLocalCostmap, 500, "nav_msgs/msg/OccupancyGrid");
    } else if (!layers.local && layerUnsubs.local) {
      layerUnsubs.local();
      delete layerUnsubs.local;
      localGrid = null;
    }
    if (layers.memories && !layerUnsubs.memsearch) {
      layerUnsubs.memsearch = ros.subscribe(MEMORY_SEARCH_TOPIC, onMemorySearch, 0, "std_msgs/msg/String");
    } else if (!layers.memories && layerUnsubs.memsearch) {
      layerUnsubs.memsearch();
      delete layerUnsubs.memsearch;
      // memState/memKnown stay: the positions feed is always on (it also
      // drives the sidebar reel's highlight/focus) — only the marks hide.
      memSearchSeen = false;
      memPulses.clear();
      memSearch = null;
      setMemHover(null);
      closeMemPopup();
      hideMemSearchCard();
    }
  }

  // Last draw's grid→canvas placement, so pointer handlers can invert it.
  /** @type {{ ox: number, oy: number, scale: number } | null} */
  let view = null;

  // "manual"/"goto" arm the map for a press-drag: click sets the position,
  // drag sets the heading.
  /** @type {"idle" | "locate" | "manual" | "goto"} */
  let ui = "idle";
  let locating = false; // auto-locate service call in flight
  let navigating = false; // a goal sent from this widget is in flight
  /** @type {{ start: { x: number, y: number }, cur: { x: number, y: number } } | null} */
  let goalDrag = null;
  // Cursor preview while manual/goto is armed: a ghost dot shows where the
  // press would land before the user commits.
  /** @type {{ x: number, y: number } | null} */
  let hoverPt = null;
  // Grab-to-pan: while set, the view centres here instead of on the robot.
  /** @type {{ x: number, y: number } | null} */
  let panCenter = null;
  /** @type {{ px: number, py: number, center: { x: number, y: number }, moved: boolean } | null} */
  let panDrag = null;
  /** @type {{ x: number, y: number, yaw: number } | null} the active goal */
  let goalMarker = null;
  // True once /nav/commanded_goal set the marker: the skill's exact target
  // wins over the per-replan plan endpoint.
  let goalIsCommanded = false;
  /** @type {ReturnType<typeof setTimeout> | undefined} */
  let navStaleTimer;

  const dpr = () => window.devicePixelRatio || 1;

  function fit() {
    const r = root.getBoundingClientRect();
    const d = dpr();
    canvas.width = Math.max(1, Math.floor(r.width * d));
    canvas.height = Math.max(1, Math.floor(r.height * d));
    canvas.style.width = `${r.width}px`;
    canvas.style.height = `${r.height}px`;
    draw();
  }

  /** @param {number} x @param {number} y world metres → canvas pixels */
  function worldToCanvas(x, y) {
    const g = /** @type {NonNullable<typeof grid>} */ (grid);
    const v = /** @type {NonNullable<typeof view>} */ (view);
    const col = (x - g.originX) / g.resolution;
    const rowFromBottom = (y - g.originY) / g.resolution;
    return { px: v.ox + col * v.scale, py: v.oy + (g.height - rowFromBottom) * v.scale };
  }

  /** @param {number} px @param {number} py canvas pixels → world metres */
  function canvasToWorld(px, py) {
    const g = /** @type {NonNullable<typeof grid>} */ (grid);
    const v = /** @type {NonNullable<typeof view>} */ (view);
    const col = (px - v.ox) / v.scale;
    const rowFromBottom = g.height - (py - v.oy) / v.scale;
    return { x: g.originX + col * g.resolution, y: g.originY + rowFromBottom * g.resolution };
  }

  /** @param {PointerEvent} e → canvas-pixel coords */
  function eventToCanvas(e) {
    const rect = canvas.getBoundingClientRect();
    const d = dpr();
    return { px: (e.clientX - rect.left) * d, py: (e.clientY - rect.top) * d };
  }

  /** @param {any} msg nav_msgs/OccupancyGrid */
  function onMap(msg) {
    const g = rasterizeGrid(msg, off, offCtx, (v, px, di) => {
      // Unknown = translucent gray; occupancy shades white (free) → black (wall).
      const shade = v < 0 ? 105 : 255 - Math.round((Math.max(0, Math.min(100, v)) / 100) * 255);
      px[di] = shade;
      px[di + 1] = shade;
      px[di + 2] = shade;
      px[di + 3] = v < 0 ? 200 : 255;
    });
    if (!g) return;
    grid = g;
    draw();
  }

  /** @param {any} msg pose-carrying message → {x, y, yaw} or null */
  function poseOf(msg) {
    const p = msg?.pose?.pose;
    const x = p?.position?.x;
    const y = p?.position?.y;
    const q = p?.orientation;
    if (typeof x !== "number" || typeof y !== "number" || !q) return null;
    return { x, y, yaw: Math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z)) };
  }

  /** @param {any} msg nav_msgs/Odometry */
  function onOdom(msg) {
    const p = poseOf(msg);
    if (!p) return;
    odomPose = p;
    pose = composedPose();
    if (layers.trail && pose) {
      const last = trail[trail.length - 1];
      const step = last ? Math.hypot(pose.x - last.x, pose.y - last.y) : Infinity;
      if (step > TRAIL_JUMP_M) trail.length = 0; // teleport = relocalized; don't draw the leap
      if (trail.length === 0 || step > TRAIL_MIN_STEP_M) {
        trail.push({ x: pose.x, y: pose.y });
        if (trail.length > TRAIL_MAX_POINTS) trail.shift();
      }
    }
    draw();
  }

  /** @param {any} msg geometry_msgs/PoseWithCovarianceStamped (map frame) */
  function onAmcl(msg) {
    const p = poseOf(msg);
    if (!p) return;
    amclPose = p;
    odomAtAmcl = odomPose;
    pose = composedPose();
    draw();
  }

  // The planner republishes the route (~1 Hz) while driving and stops on
  // arrival, so a lull means navigation ended — drop the route and goal then.
  const NAV_STALE_MS = 4000;

  function armNavStale() {
    clearTimeout(navStaleTimer);
    navStaleTimer = setTimeout(() => {
      goalMarker = null;
      goalIsCommanded = false;
      plan = null;
      activePlanTopic = null;
      render();
      draw();
    }, NAV_STALE_MS);
  }

  /** @param {any} msg geometry_msgs/PoseStamped — the skill's exact target */
  function onCommandedGoal(msg) {
    // Map-frame targets only: odom-frame goals would drift against the map canvas.
    if (msg?.header?.frame_id !== "map") return;
    const pos = msg?.pose?.position;
    const q = msg?.pose?.orientation;
    if (typeof pos?.x !== "number" || typeof pos?.y !== "number" || !q) return;
    goalMarker = { x: pos.x, y: pos.y, yaw: Math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z)) };
    goalIsCommanded = true;
    armNavStale(); // the goal marks an active navigation; expire it like the route
    render();
    draw();
  }

  // The topic whose route is currently displayed; only it may clear the route.
  /** @type {string | null} */
  let activePlanTopic = null;

  /** @param {string} topic @param {any} msg nav_msgs/Path */
  function onPlan(topic, msg) {
    const poses = msg?.poses;
    if (!Array.isArray(poses)) return;
    const pts = [];
    for (const ps of poses) {
      const pos = ps?.pose?.position;
      if (typeof pos?.x === "number" && typeof pos?.y === "number") pts.push({ x: pos.x, y: pos.y });
    }
    if (pts.length) {
      activePlanTopic = topic;
      plan = pts;
      // Fallback goal marker until the exact commanded goal arrives (the plan
      // endpoint wiggles with every replan); also covers navigations that
      // bypass the skill.
      if (!goalIsCommanded) {
        const end = poses[poses.length - 1]?.pose;
        const q = end?.orientation;
        if (q) {
          const yaw = Math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z));
          goalMarker = { x: pts[pts.length - 1].x, y: pts[pts.length - 1].y, yaw };
        }
      }
      armNavStale();
    } else {
      // Empty path = navigation finished/aborted — but only from the planner
      // that owns the displayed route; the inactive one must not clear it.
      if (activePlanTopic && topic !== activePlanTopic) return;
      plan = null;
      goalMarker = null;
      goalIsCommanded = false;
      clearTimeout(navStaleTimer);
    }
    render();
    draw();
  }

  function draw() {
    if (!ctx) return;
    ctx.fillStyle = "#0a0a0c";
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    if (!grid) {
      ctx.fillStyle = "#8a8a93";
      ctx.font = `${14 * dpr()}px ui-monospace, monospace`;
      ctx.textAlign = "center";
      ctx.fillText("waiting for /map…", canvas.width / 2, canvas.height / 2);
      return;
    }

    // Centre on the pan point if the user grabbed the map, else follow the
    // robot; zoom mode shows a fixed real-world window (zoomMeters across).
    let scale, ox, oy;
    const center = panCenter ?? (zoomMeters && pose ? pose : null);
    if (zoomMeters && center) {
      const cellsAcross = zoomMeters / grid.resolution;
      scale = Math.min(canvas.width, canvas.height) / cellsAcross;
    } else {
      // Fit-the-whole-grid scale (no zoom set, or before the first pose).
      const pad = 16 * dpr();
      scale = Math.min((canvas.width - 2 * pad) / grid.width, (canvas.height - 2 * pad) / grid.height);
    }
    if (center) {
      const col = (center.x - grid.originX) / grid.resolution;
      const rowFromBottom = (center.y - grid.originY) / grid.resolution;
      ox = canvas.width / 2 - col * scale;
      oy = canvas.height / 2 - (grid.height - rowFromBottom) * scale;
    } else {
      ox = (canvas.width - grid.width * scale) / 2;
      oy = (canvas.height - grid.height * scale) / 2;
    }
    view = { ox, oy, scale };
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(off, ox, oy, grid.width * scale, grid.height * scale);

    // Costmap overlay, aligned by world coords (its origin/size differ from the
    // map's). scale is px per map cell, so convert via the resolution ratio.
    if (layers.costmap && costGrid) {
      const topLeft = worldToCanvas(costGrid.originX, costGrid.originY + costGrid.height * costGrid.resolution);
      const cellPx = (costGrid.resolution / grid.resolution) * scale;
      ctx.drawImage(costOff, topLeft.px, topLeft.py, costGrid.width * cellPx, costGrid.height * cellPx);
    }

    // Local costmap: its coordinates are in the ODOM frame, so place it via
    // the map<-odom correction implied by the composed (map-frame) pose vs
    // raw odom — identity until AMCL's first fix, when the frames coincide.
    if (layers.local && localGrid) {
      let theta = 0;
      let tx = 0;
      let ty = 0;
      if (pose && odomPose) {
        theta = pose.yaw - odomPose.yaw;
        const c = Math.cos(theta);
        const s = Math.sin(theta);
        tx = pose.x - (odomPose.x * c - odomPose.y * s);
        ty = pose.y - (odomPose.x * s + odomPose.y * c);
      }
      const c = Math.cos(theta);
      const s = Math.sin(theta);
      // The image's top-left corner (max-y edge, matching the row flip) in
      // odom, carried into the map frame, then a canvas rotation of -theta
      // (canvas y points down, so world CCW is canvas CW).
      const tlx = localGrid.originX;
      const tly = localGrid.originY + localGrid.height * localGrid.resolution;
      const { px, py } = worldToCanvas(tx + tlx * c - tly * s, ty + tlx * s + tly * c);
      const cellPx = (localGrid.resolution / grid.resolution) * scale;
      ctx.save();
      ctx.translate(px, py);
      ctx.rotate(-theta);
      ctx.drawImage(localOff, 0, 0, localGrid.width * cellPx, localGrid.height * cellPx);
      ctx.restore();
    }

    drawMemories();

    if (layers.trail && trail.length >= 2) {
      ctx.strokeStyle = MAP_COLORS.trail;
      ctx.lineWidth = 1.5 * dpr();
      ctx.lineJoin = "round";
      ctx.beginPath();
      trail.forEach((p, i) => {
        const { px, py } = worldToCanvas(p.x, p.y);
        if (i === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
      });
      ctx.stroke();
    }

    if (plan && plan.length >= 2) {
      ctx.strokeStyle = MAP_COLORS.route;
      ctx.lineWidth = 2 * dpr();
      ctx.lineJoin = "round";
      ctx.beginPath();
      plan.forEach((p, i) => {
        const { px, py } = worldToCanvas(p.x, p.y);
        if (i === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
      });
      ctx.stroke();
    }

    // Laser scan, projected from the composed robot pose + the static laser
    // offset. Rects, not arcs — up to ~1500 points per sweep.
    if (layers.scan && scanMsg && pose) {
      const c = Math.cos(pose.yaw);
      const s = Math.sin(pose.yaw);
      const lx = pose.x + laserOffset.x * c - laserOffset.y * s;
      const ly = pose.y + laserOffset.x * s + laserOffset.y * c;
      const lyaw = pose.yaw + laserOffset.yaw;
      const { ranges, angle_min: a0, angle_increment: inc, range_min: rMin, range_max: rMax } = scanMsg;
      const size = Math.max(3, 3 * dpr());
      ctx.fillStyle = MAP_COLORS.scan;
      for (let i = 0; i < ranges.length; i++) {
        const r = ranges[i];
        if (!Number.isFinite(r) || r < rMin || r > rMax) continue;
        const a = lyaw + a0 + i * inc;
        const { px, py } = worldToCanvas(lx + r * Math.cos(a), ly + r * Math.sin(a));
        ctx.fillRect(px - size / 2, py - size / 2, size, size);
      }
    }

    drawMemorySearch();

    // Ghost dot under the cursor while manual/goto is armed, until the press
    // starts the real drag.
    if (hoverPt && !goalDrag && (ui === "manual" || ui === "goto")) {
      const { px, py } = worldToCanvas(hoverPt.x, hoverPt.y);
      const r = Math.max(4, 6 * dpr());
      ctx.globalAlpha = 0.45;
      ctx.fillStyle = ui === "manual" ? MAP_COLORS.robot : MAP_COLORS.goal;
      ctx.beginPath();
      ctx.arc(px, py, r, 0, Math.PI * 2);
      ctx.fill();
      ctx.globalAlpha = 1;
    }

    // Goal dot + heading arrow; a manual-locate drag places the robot, not a
    // goal — draw it in the robot's color.
    const goalAt = goalDrag ? { x: goalDrag.start.x, y: goalDrag.start.y } : goalMarker;
    if (goalAt) {
      const color = goalDrag && ui === "manual" ? MAP_COLORS.robot : MAP_COLORS.goal;
      const { px, py } = worldToCanvas(goalAt.x, goalAt.y);
      const r = Math.max(4, 6 * dpr());
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.arc(px, py, r, 0, Math.PI * 2);
      ctx.fill();
      const yaw = goalDrag ? Math.atan2(goalDrag.cur.y - goalDrag.start.y, goalDrag.cur.x - goalDrag.start.x) : goalMarker?.yaw;
      if (typeof yaw === "number") {
        ctx.strokeStyle = color;
        ctx.lineWidth = 2 * dpr();
        ctx.beginPath();
        ctx.moveTo(px, py);
        ctx.lineTo(px + Math.cos(yaw) * r * 2.4, py - Math.sin(yaw) * r * 2.4);
        ctx.stroke();
      }
    }

    if (pose) {
      const { px, py } = worldToCanvas(pose.x, pose.y);
      // World-sized, not screen-sized: the dot covers the same floor area at
      // every zoom (scale is px per map cell). Floored so it never vanishes.
      const rad = Math.max(3 * dpr(), (ROBOT_RADIUS_M / grid.resolution) * scale);
      ctx.fillStyle = MAP_COLORS.robot;
      ctx.beginPath();
      ctx.arc(px, py, rad, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = MAP_COLORS.robot;
      ctx.lineWidth = 2 * dpr();
      ctx.beginPath();
      ctx.moveTo(px, py);
      ctx.lineTo(px + Math.cos(pose.yaw) * rad * 2.4, py - Math.sin(pose.yaw) * rad * 2.4);
      ctx.stroke();
    }

    // Cards are world-anchored DOM: keep them glued to their memory through
    // every pan/zoom/follow reframe.
    if (!memHoverCard.hidden && memHover) placeMemCard(memHoverCard, memHover);
    if (!memPopup.hidden && memPopupFor !== null) {
      const m = memState?.memories.find((mem) => mem.id === memPopupFor);
      if (m) placeMemCard(memPopup, m);
      else closeMemPopup();
    }
  }

  /** The remembered viewpoints: one soft view cone + dot each, fading with
   * age; a white ring blooms once when a memory is first recorded. */
  function drawMemories() {
    if (!ctx || !grid || !view || !layers.memories || !memState?.memories.length) return;
    const nowS = Date.now() / 1000;
    const nowMs = performance.now();
    const d = dpr();
    const g = /** @type {NonNullable<typeof grid>} */ (grid);
    const v = /** @type {NonNullable<typeof view>} */ (view);
    const range = (MEM_WEDGE_RANGE_M / g.resolution) * v.scale;
    const searchHit = memSearch?.found && nowMs < memSearchUntil ? memSearch.id : null;
    for (const m of memState.memories) {
      const { px, py } = worldToCanvas(m.x, m.y);
      const active = memHover?.id === m.id || memHighlight === m.id || memPopupFor === m.id || searchHit === m.id;
      const alpha = active ? 1 : ageAlpha(m.stamp, nowS);
      // The view cone (canvas y is down, so world angles negate).
      const grad = ctx.createRadialGradient(px, py, 0, px, py, range);
      grad.addColorStop(0, withAlpha(MEMORY_COLOR, (active ? 0.5 : 0.3) * alpha));
      grad.addColorStop(1, withAlpha(MEMORY_COLOR, 0));
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.moveTo(px, py);
      ctx.arc(px, py, range, -m.theta - MEM_WEDGE_HALF_RAD, -m.theta + MEM_WEDGE_HALF_RAD);
      ctx.closePath();
      ctx.fill();
      // The dot.
      ctx.fillStyle = withAlpha(MEMORY_COLOR, active ? 1 : 0.45 + 0.55 * alpha);
      ctx.beginPath();
      ctx.arc(px, py, (active ? 4.5 : 3) * d, 0, Math.PI * 2);
      ctx.fill();
      if (active) {
        ctx.strokeStyle = withAlpha("#ffffff", 0.9);
        ctx.lineWidth = 1.5 * d;
        ctx.beginPath();
        ctx.arc(px, py, 6.5 * d, 0, Math.PI * 2);
        ctx.stroke();
      }
      // Pulse-in: a ring blooms outward as the robot commits a new memory.
      const born = memPulses.get(m.id);
      if (born !== undefined) {
        const t = (nowMs - born) / MEM_PULSE_MS;
        if (t >= 1) memPulses.delete(m.id);
        else {
          const ease = 1 - (1 - t) ** 3;
          ctx.strokeStyle = withAlpha("#ffffff", 0.85 * (1 - t));
          ctx.lineWidth = 2 * d;
          ctx.beginPath();
          ctx.arc(px, py, (6 + 30 * ease) * d, 0, Math.PI * 2);
          ctx.stroke();
        }
      }
    }
  }

  /** The recall moment: breathing rings on the matched memory and a drifting
   * dashed thread from the robot to it — "I remember it's over there". */
  function drawMemorySearch() {
    if (!ctx || !grid || !view || !layers.memories) return;
    if (!memSearch?.found || typeof memSearch.x !== "number" || typeof memSearch.y !== "number") return;
    if (performance.now() >= memSearchUntil) return;
    const d = dpr();
    const t = performance.now() / 1000;
    const { px, py } = worldToCanvas(memSearch.x, memSearch.y);
    if (pose) {
      const from = worldToCanvas(pose.x, pose.y);
      ctx.strokeStyle = withAlpha(MEMORY_COLOR, 0.75);
      ctx.lineWidth = 1.5 * d;
      ctx.setLineDash([6 * d, 6 * d]);
      ctx.lineDashOffset = -((t * 30 * d) % (12 * d));
      ctx.beginPath();
      ctx.moveTo(from.px, from.py);
      ctx.lineTo(px, py);
      ctx.stroke();
      ctx.setLineDash([]);
    }
    for (let k = 0; k < 2; k++) {
      const phase = (t * 0.8 + k * 0.5) % 1;
      ctx.strokeStyle = withAlpha(MEMORY_COLOR, 0.9 * (1 - phase));
      ctx.lineWidth = 2 * d;
      ctx.beginPath();
      ctx.arc(px, py, (7 + phase * 26) * d, 0, Math.PI * 2);
      ctx.stroke();
    }
  }

  function render() {
    const navActive = navigating || goalMarker !== null;
    // Mapping mode: navigation actions (locate/goto/stop) are meaningless
    // against a half-built map — only Recenter survives.
    backBtn.hidden = ui === "idle" || mappingMode;
    locateBtn.hidden = ui !== "idle" || mappingMode;
    locateBtn.disabled = locating || navActive;
    locateBtn.classList.toggle("is-active", locating);
    setHint(locateBtn, locating ? "locating…" : "auto or manual");
    autoBtn.hidden = ui !== "locate" || mappingMode;
    autoBtn.disabled = locating;
    manualBtn.hidden = (ui !== "locate" && ui !== "manual") || mappingMode;
    manualBtn.classList.toggle("is-active", ui === "manual");
    setHint(manualBtn, ui === "manual" ? "click & drag on the map" : "drag where the robot is");
    goBtn.hidden = (ui !== "idle" && ui !== "goto") || (ui === "idle" && navActive) || mappingMode;
    goBtn.classList.toggle("is-active", ui === "goto");
    setHint(goBtn, ui === "goto" ? "click & drag on the map" : "tap a point to navigate");
    stopBtn.hidden = !(ui === "idle" && navActive) || mappingMode;
    centerBtn.hidden = panCenter === null;
    canvas.style.cursor = ui === "manual" || ui === "goto" ? "crosshair" : memHover ? "pointer" : "grab";
  }

  /** @param {"idle" | "locate" | "manual" | "goto"} next */
  function setUi(next) {
    ui = next;
    if (ui !== "idle") {
      setMemHover(null);
      closeMemPopup();
    }
    if ((ui !== "manual" && ui !== "goto") && (goalDrag || hoverPt)) {
      goalDrag = null;
      hoverPt = null;
      draw();
    }
    render();
  }

  // Drag instructions live in the status line; only wipe them, never a result.
  function clearHint() {
    if (statusEl.dataset.kind === "hint") statusEl.hidden = true;
  }

  async function autoLocate() {
    if (locating) return;
    locating = true;
    setUi("idle");
    setStatus("hint", "Locating — matching the lidar scan against the map…");
    try {
      const res = await ros.callService(LOCALIZE_SERVICE, {}, LOCALIZE_TIMEOUT_MS);
      if (res?.success) setStatus("ok", res.message || "Localized", true);
      else setStatus("fail", res?.message || "Could not localize — try Manual");
    } catch (err) {
      setStatus("fail", `Locate failed — ${err instanceof Error ? err.message : String(err)}`);
    } finally {
      locating = false;
      render();
    }
  }

  /** @param {number} x @param {number} y @param {number} yaw seed AMCL at the hand-placed pose */
  async function sendInitialPose(x, y, yaw) {
    setStatus("hint", "Setting position…");
    // Hand placement is approximate — same convergence room the mobile app gives AMCL.
    const covariance = new Array(36).fill(0);
    covariance[0] = 0.25;
    covariance[7] = 0.25;
    covariance[35] = 0.068;
    try {
      await ros.callService(SET_INITIAL_POSE_SERVICE, {
        pose: {
          header: { stamp: { sec: 0, nanosec: 0 }, frame_id: "map" },
          pose: {
            pose: { position: { x, y, z: 0 }, orientation: { x: 0, y: 0, z: Math.sin(yaw / 2), w: Math.cos(yaw / 2) } },
            covariance,
          },
        },
      });
      setStatus("ok", "Position set", true);
    } catch (err) {
      setStatus("fail", `Set position failed — ${err instanceof Error ? err.message : String(err)}`);
    }
  }

  /** @param {number} x @param {number} y @param {number} yaw */
  function publishGoal(x, y, yaw) {
    const qz = Math.sin(yaw / 2);
    const qw = Math.cos(yaw / 2);
    // Action goal through the router, pinned to the map planner — /goal_pose
    // would bypass it and run on whatever planner was last latched. Zero stamp
    // = "latest" to TF; wall stamps break under ROS sim time.
    const gen = ++goalGen;
    /** @type {{ distance_remaining?: number, number_of_recoveries?: number } | null} */
    let lastFb = null;
    navigating = true;
    setStatus("navigating", "Navigating…");
    ros
      .sendActionGoal(
        "/navigate_to_pose",
        "nav2_msgs/action/NavigateToPose",
        {
          pose: {
            header: { stamp: { sec: 0, nanosec: 0 }, frame_id: "map" },
            pose: { position: { x, y, z: 0 }, orientation: { x: 0, y: 0, z: qz, w: qw } },
          },
          behavior_tree: "navigation",
        },
        {
          onFeedback: (v) => {
            if (gen !== goalGen || typeof v?.distance_remaining !== "number") return;
            lastFb = v;
            const rec = v.number_of_recoveries ? `, ${v.number_of_recoveries} recover${v.number_of_recoveries === 1 ? "y" : "ies"}` : "";
            setStatus("navigating", `Navigating — ${v.distance_remaining.toFixed(1)} m left${rec}`);
          },
        },
      )
      .promise.then(() => {
        if (gen === goalGen) setStatus("ok", "Reached the goal", true);
      })
      .catch(() => {
        if (gen !== goalGen) return;
        if (stopRequested === gen) {
          setStatus("muted", "Stopped", true);
          return;
        }
        // NavigateToPose's result message is empty on Humble — reconstruct
        // the reason from the last feedback.
        const d = lastFb?.distance_remaining;
        const n = lastFb?.number_of_recoveries ?? 0;
        const detail =
          typeof d === "number"
            ? ` ${d.toFixed(1)} m from the goal${n ? ` after ${n} recover${n === 1 ? "y" : "ies"}` : ""} — route blocked or robot stuck?`
            : " — no path (goal unreachable or outside the map?)";
        setStatus("fail", `Failed${detail}`);
      })
      .finally(() => {
        if (gen !== goalGen) return; // a newer goal took over the flag
        navigating = false;
        render();
      });
    plan = null; // drop the stale route; the new one streams in on /plan
    goalMarker = { x, y, yaw };
    goalIsCommanded = false; // a fresh click supersedes any previous skill goal
    armNavStale(); // hold the goal until the route starts, then while it runs
    render();
  }

  /** @param {PointerEvent} e */
  function onPointerDown(e) {
    if (!grid || !view) return;
    e.preventDefault();
    const { px, py } = eventToCanvas(e);
    canvas.setPointerCapture(e.pointerId);
    if (ui === "manual" || ui === "goto") {
      const w = canvasToWorld(px, py);
      hoverPt = null; // the drag's own dot takes over
      goalDrag = { start: w, cur: w };
      draw();
      return;
    }
    // Otherwise grab-to-pan.
    panDrag = { px, py, center: canvasToWorld(canvas.width / 2, canvas.height / 2), moved: false };
  }

  /** @param {PointerEvent} e */
  function onPointerMove(e) {
    if (goalDrag) {
      const { px, py } = eventToCanvas(e);
      goalDrag.cur = canvasToWorld(px, py);
      draw();
      return;
    }
    if (!panDrag && (ui === "manual" || ui === "goto")) {
      if (!grid || !view) return;
      const { px, py } = eventToCanvas(e);
      hoverPt = canvasToWorld(px, py);
      draw();
      return;
    }
    if (!panDrag && ui === "idle") {
      const { px, py } = eventToCanvas(e);
      setMemHover(hitMemory(px, py));
    }
    if (!panDrag || !grid || !view) return;
    const { px, py } = eventToCanvas(e);
    const dx = px - panDrag.px;
    const dy = py - panDrag.py;
    if (!panDrag.moved) {
      // A few px of dead zone so plain clicks don't nudge the view.
      if (Math.hypot(dx, dy) < 4 * dpr()) return;
      panDrag.moved = true;
      canvas.style.cursor = "grabbing";
    }
    // Shift the centre opposite the drag (canvas y down, world y up).
    const mPerPx = grid.resolution / view.scale;
    panCenter = { x: panDrag.center.x - dx * mPerPx, y: panDrag.center.y + dy * mPerPx };
    draw();
  }

  /** @param {PointerEvent} e */
  function onPointerUp(e) {
    if (panDrag) {
      const clicked = !panDrag.moved;
      panDrag = null;
      render(); // restore the cursor, surface Recenter
      // A plain click (no pan) opens the memory under the pointer — or lets an
      // open popup go when the click landed on empty map. Hit-test here, not
      // via memHover: touch has no hover phase before the tap.
      if (clicked) {
        const { px, py } = eventToCanvas(e);
        const hit = hitMemory(px, py);
        if (hit) openMemPopup(hit);
        else closeMemPopup();
      }
      return;
    }
    if (!goalDrag) return;
    const { start, cur } = goalDrag;
    goalDrag = null;
    const dx = cur.x - start.x;
    const dy = cur.y - start.y;
    // Short drag → no meaningful heading, just face "east".
    const yaw = Math.hypot(dx, dy) > 0.1 ? Math.atan2(dy, dx) : 0;
    if (ui === "manual") sendInitialPose(start.x, start.y, yaw);
    else publishGoal(start.x, start.y, yaw);
    setUi("idle");
    draw();
  }

  // Scroll to zoom (only in robot-centred mode). Scroll up = zoom in = show fewer metres.
  /** @param {WheelEvent} e */
  function onWheel(e) {
    if (!zoomMeters) return; // fit-whole mode (standalone page) doesn't zoom
    e.preventDefault();
    const next = zoomMeters * (e.deltaY > 0 ? ZOOM_STEP : 1 / ZOOM_STEP);
    zoomMeters = Math.min(MAX_ZOOM_M, Math.max(MIN_ZOOM_M, next));
    draw();
    opts.onZoomChange?.(zoomMeters);
  }

  backBtn.addEventListener("click", () => {
    clearHint();
    setUi("idle");
  });
  centerBtn.addEventListener("click", () => {
    panCenter = null; // back to following the robot
    render();
    draw();
  });
  locateBtn.addEventListener("click", () => setUi("locate"));
  autoBtn.addEventListener("click", autoLocate);
  manualBtn.addEventListener("click", () => {
    if (ui === "manual") {
      clearHint();
      setUi("locate");
      return;
    }
    setStatus("hint", "Click the map where the robot is, drag to set its heading");
    setUi("manual");
  });
  goBtn.addEventListener("click", () => {
    if (ui === "goto") {
      clearHint();
      setUi("idle");
      return;
    }
    setStatus("hint", "Click the destination, drag to set the final heading");
    setUi("goto");
  });

  // Stop cancels every active navigation goal server-side, then drops the
  // local goal marker and route.
  let stopRequested = 0; // goal generation the user stopped, for status wording
  stopBtn.addEventListener("click", async () => {
    stopBtn.disabled = true;
    setHint(stopBtn, "stopping…");
    stopRequested = goalGen;
    try {
      await ros.callService(CANCEL_NAVIGATION_SERVICE, {});
      goalMarker = null;
      goalIsCommanded = false;
      plan = null;
      draw();
      setHint(stopBtn, "stopped");
    } catch (err) {
      console.error("[map] cancel navigation failed:", err);
      setHint(stopBtn, "stop failed");
    } finally {
      stopBtn.disabled = false;
      setTimeout(() => {
        setHint(stopBtn, "cancel navigation");
        render();
      }, 1500);
    }
  });
  canvas.addEventListener("pointerdown", onPointerDown);
  canvas.addEventListener("pointermove", onPointerMove);
  canvas.addEventListener("pointerup", onPointerUp);
  canvas.addEventListener("pointerleave", () => {
    setMemHover(null);
    if (!hoverPt) return;
    hoverPt = null;
    draw();
  });
  canvas.addEventListener("wheel", onWheel, { passive: false });
  /** @param {KeyboardEvent} e */
  function onKeyDown(e) {
    if (e.key !== "Escape") return;
    closeMemPopup();
    hideMemSearchCard();
  }
  document.addEventListener("keydown", onKeyDown);

  // Resize with the container, not just the window — covers reparenting between
  // the small PiP tile and the full stage in teleop.
  const ro = new ResizeObserver(() => fit());
  ro.observe(root);
  fit();
  render();

  syncLayerSubs();
  const unsubMap = ros.subscribe(MAP_TOPIC, onMap, 250, "nav_msgs/msg/OccupancyGrid");
  const unsubOdom = ros.subscribe(ODOM_TOPIC, onOdom, 100, "nav_msgs/msg/Odometry");
  const unsubAmcl = ros.subscribe(AMCL_POSE_TOPIC, onAmcl, 0, "geometry_msgs/msg/PoseWithCovarianceStamped");
  const unsubPlans = PLAN_TOPICS.map((topic) => ros.subscribe(topic, (msg) => onPlan(topic, msg), 250, "nav_msgs/msg/Path"));
  const unsubGoal = ros.subscribe(COMMANDED_GOAL_TOPIC, onCommandedGoal, 0, "geometry_msgs/msg/PoseStamped");
  // Always on (a tiny 1 Hz JSON), not layer-gated: highlightMemory/focusMemory
  // must keep working from the sidebar reel while the layer chip is off.
  const unsubMemories = ros.subscribe(MEMORY_POSITIONS_TOPIC, onMemories, 0, "std_msgs/msg/String");

  return {
    /** Swap to a saved zoom (e.g. when this widget reparents between thumbnail and full stage). */
    setZoom(meters) {
      if (typeof meters === "number" && meters > 0 && meters !== zoomMeters) {
        zoomMeters = meters;
        draw();
      }
    },
    /**
     * Enter/leave mapping mode: swaps the pose source to /mapping_pose,
     * clears nav-mode leftovers (route, goal, AMCL anchor — all tied to the
     * previous map), and hides the navigation controls.
     * @param {boolean} on
     */
    setMappingMode(on) {
      if (mappingMode === on) return;
      mappingMode = on;
      setUi("idle");
      plan = null;
      goalMarker = null;
      goalIsCommanded = false;
      activePlanTopic = null;
      clearTimeout(navStaleTimer);
      if (on) {
        amclPose = null;
        odomAtAmcl = null;
        // Memory marks are frames of the *finished* map — meaningless against
        // the one being built.
        setMemHover(null);
        closeMemPopup();
        hideMemSearchCard();
        memSearch = null;
        unsubMappingPose = ros.subscribe(
          MAPPING_POSE_TOPIC,
          (msg) => {
            const p = poseOf(msg);
            if (!p) return;
            mappingPose = p;
            pose = composedPose();
            draw();
          },
          100,
          "nav_msgs/msg/Odometry",
        );
      } else {
        unsubMappingPose?.();
        unsubMappingPose = null;
        mappingPose = null;
      }
      pose = composedPose();
      render();
      draw();
    },
    /** Drop the breadcrumb trail — points from a previous map are meaningless in the new map's frame. */
    clearTrail() {
      trail.length = 0;
      draw();
    },
    /**
     * The active map switched: the AMCL anchor and trail belong to the old
     * map's frame — drop both. (AMCL keeps streaming its stale pose across a
     * switch, so "wait for the next fix" can't work; the stale stub the trail
     * regrows from is wiped by the jump detector when re-localization lands.)
     */
    mapChanged() {
      amclPose = null;
      odomAtAmcl = null;
      trail.length = 0;
      // The memory layer swaps itself when the positions payload names the new
      // map; the search verdict and popup belong to the old frame — drop now.
      closeMemPopup();
      hideMemSearchCard();
      memSearch = null;
      pose = composedPose();
      draw();
    },
    /** Gallery hover: hold one memory bright without opening anything. @param {number | null} id */
    highlightMemory(id) {
      if (memHighlight === id) return;
      memHighlight = id;
      draw();
    },
    /** Gallery click: centre the view on a memory and open its popup. @param {number} id */
    focusMemory(id) {
      if (mappingMode) return; // the coordinates belong to the finished map, and Go-here mid-mapping is wrong
      const m = memState?.memories.find((mem) => mem.id === id);
      if (!m) return;
      panCenter = { x: m.x, y: m.y };
      render(); // surfaces Recenter, like a hand pan
      openMemPopup(m);
      draw();
    },
    /** Toggle an overlay layer (subscribes/unsubscribes its topics live).
     * The trail survives an off/on toggle — hiding is not clearing (that's clearTrail). */
    setLayer(name, on) {
      if (layers[name] === on) return;
      layers[name] = on;
      syncLayerSubs();
      draw();
    },
    destroy() {
      clearTimeout(navStaleTimer);
      clearTimeout(statusClearTimer);
      clearTimeout(memSearchCardTimer);
      cancelAnimationFrame(memAnimFrame);
      document.removeEventListener("keydown", onKeyDown);
      ro.disconnect();
      unsubMap();
      unsubOdom();
      unsubAmcl();
      for (const unsub of unsubPlans) unsub();
      unsubGoal();
      unsubMemories();
      unsubMappingPose?.();
      for (const unsub of Object.values(layerUnsubs)) unsub();
      canvas.remove();
      controls.remove();
      memHoverCard.remove();
      memPopup.remove();
      memSearchCard.remove();
    },
  };
}
