// @ts-check
// Reusable nav-map widget — a plain 2D <canvas> rendering the occupancy grid
// (/map), robot pose (/odom), planner route (/plan), and click-to-navigate
// (sends a NavigateToPose goal via the router), plus opt-in overlay layers
// (lidar scan / global costmap / local costmap / odometry trail) for the Nav page.
// Sizes itself to its container via a ResizeObserver, so
// it can be the standalone Map page OR a teleop PiP tile that reparents between
// a small thumbnail and the full stage. No three.js — a canvas + putImageData
// is all a 2D map needs.

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
} from "../constants.js";

// Overlay palette — exported so the Nav page legend shows the same colors.
// The robot is deliberately far from the costmap ramp (blue → amber → red):
// it used to be the same amber as the inscribed band and vanished into it.
export const MAP_COLORS = {
  robot: "#ff5ce1",
  trail: "rgb(255 92 225 / 45%)",
  goal: "#00ff88",
  route: "#00b7ff",
  scan: "#d96a5a",
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

/**
 * @typedef {"scan" | "costmap" | "local" | "trail"} LayerName
 */

/**
 * @param {HTMLElement} root container the map fills (sized via ResizeObserver).
 * @param {{ zoom?: number, onZoomChange?: (meters: number) => void, layers?: Partial<Record<LayerName, boolean>> }} [opts]
 *   zoom = metres of real-world width to show, centred on the robot pose (keeps the map legible when
 *   small); enables scroll-to-zoom. Omit to fit the whole grid (the standalone page). onZoomChange
 *   fires after each wheel-zoom. layers turns on optional overlays (Nav page): live lidar scan,
 *   global/local costmaps, odometry trail — each adds its subscription only while enabled.
 * @returns {{ destroy: () => void, setZoom: (meters: number) => void, setLayer: (name: LayerName, on: boolean) => void, setMappingMode: (on: boolean) => void }}
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
  const autoBtn = makeButton(ICONS.auto, "Auto", "match lidar to the map");
  const manualBtn = makeButton(ICONS.manual, "Manual", "drag where the robot is");
  const goBtn = makeButton(ICONS.go, "Go To", "tap a point to navigate");
  const stopBtn = makeButton(ICONS.stop, "Stop", "cancel navigation");
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
  const layers = { scan: false, costmap: false, local: false, trail: false, ...opts.layers };
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
  /** @type {Partial<Record<"scan" | "costmap" | "local" | "tf", () => void>>} live layer subscriptions */
  const layerUnsubs = {};

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
   * nav_msgs/OccupancyGrid → Nav2 cost colormap on a 1px-per-cell offscreen
   * canvas, transparent where free. Shared by the global and local costmaps.
   * @param {any} msg @param {HTMLCanvasElement} target @param {CanvasRenderingContext2D | null} targetCtx
   * @returns {{ width: number, height: number, resolution: number, originX: number, originY: number } | null}
   */
  function decodeCostmap(msg, target, targetCtx) {
    const info = msg?.info;
    const data = msg?.data;
    const width = info?.width | 0;
    const height = info?.height | 0;
    if (!info || !Array.isArray(data) || width <= 0 || height <= 0 || data.length < width * height || !targetCtx) {
      return null;
    }
    target.width = width;
    target.height = height;
    const img = targetCtx.createImageData(width, height);
    for (let row = 0; row < height; row++) {
      const srcRow = height - 1 - row; // same flip as the map grid
      for (let col = 0; col < width; col++) {
        const v = data[srcRow * width + col];
        const di = (row * width + col) * 4;
        if (v <= 0) continue; // free/unknown stays transparent (alpha 0)
        if (v >= 100) {
          // Lethal — the obstacle itself.
          img.data[di] = 217;
          img.data[di + 1] = 106;
          img.data[di + 2] = 90;
          img.data[di + 3] = 210;
        } else if (v >= 99) {
          // Inscribed — the footprint would collide here.
          img.data[di] = 232;
          img.data[di + 1] = 163;
          img.data[di + 2] = 61;
          img.data[di + 3] = 190;
        } else {
          // Inflation gradient, fading with cost.
          img.data[di] = 77;
          img.data[di + 1] = 124;
          img.data[di + 2] = 254;
          img.data[di + 3] = 25 + Math.round((v / 98) * 115);
        }
      }
    }
    targetCtx.putImageData(img, 0, 0);
    return {
      width,
      height,
      resolution: info.resolution || 0.05,
      originX: info.origin?.position?.x ?? 0,
      originY: info.origin?.position?.y ?? 0,
    };
  }

  /** @param {any} msg nav_msgs/OccupancyGrid (map frame) */
  function onCostmap(msg) {
    const g = decodeCostmap(msg, costOff, costOffCtx);
    if (!g) return;
    costGrid = g;
    draw();
  }

  /** @param {any} msg nav_msgs/OccupancyGrid (odom frame, rolling window) */
  function onLocalCostmap(msg) {
    const g = decodeCostmap(msg, localOff, localOffCtx);
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
    const info = msg?.info;
    const data = msg?.data;
    const width = info?.width | 0;
    const height = info?.height | 0;
    if (!info || !Array.isArray(data) || width <= 0 || height <= 0 || data.length < width * height) return;
    off.width = width;
    off.height = height;
    if (!offCtx) return;
    const img = offCtx.createImageData(width, height);
    for (let row = 0; row < height; row++) {
      const srcRow = height - 1 - row; // flip so canvas-top = highest world-y
      for (let col = 0; col < width; col++) {
        const v = data[srcRow * width + col];
        const di = (row * width + col) * 4;
        let shade;
        let a = 255;
        if (v < 0) {
          shade = 105; // unknown
          a = 200;
        } else {
          shade = 255 - Math.round((Math.max(0, Math.min(100, v)) / 100) * 255);
        }
        img.data[di] = shade;
        img.data[di + 1] = shade;
        img.data[di + 2] = shade;
        img.data[di + 3] = a;
      }
    }
    offCtx.putImageData(img, 0, 0);
    grid = {
      width,
      height,
      resolution: info.resolution || 0.05,
      originX: info.origin?.position?.x ?? 0,
      originY: info.origin?.position?.y ?? 0,
    };
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
      if (!last || Math.hypot(pose.x - last.x, pose.y - last.y) > TRAIL_MIN_STEP_M) {
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
      const size = Math.max(2, 2 * dpr());
      ctx.fillStyle = MAP_COLORS.scan;
      for (let i = 0; i < ranges.length; i++) {
        const r = ranges[i];
        if (!Number.isFinite(r) || r < rMin || r > rMax) continue;
        const a = lyaw + a0 + i * inc;
        const { px, py } = worldToCanvas(lx + r * Math.cos(a), ly + r * Math.sin(a));
        ctx.fillRect(px - size / 2, py - size / 2, size, size);
      }
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
    canvas.style.cursor = ui === "manual" || ui === "goto" ? "crosshair" : "grab";
  }

  /** @param {"idle" | "locate" | "manual" | "goto"} next */
  function setUi(next) {
    ui = next;
    if (ui !== "manual" && ui !== "goto") goalDrag = null;
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
      panDrag = null;
      render(); // restore the cursor, surface Recenter
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
  canvas.addEventListener("wheel", onWheel, { passive: false });

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
    /** Toggle an overlay layer (subscribes/unsubscribes its topics live). */
    setLayer(name, on) {
      if (layers[name] === on) return;
      layers[name] = on;
      if (name === "trail" && !on) trail.length = 0;
      syncLayerSubs();
      draw();
    },
    destroy() {
      clearTimeout(navStaleTimer);
      clearTimeout(statusClearTimer);
      ro.disconnect();
      unsubMap();
      unsubOdom();
      unsubAmcl();
      for (const unsub of unsubPlans) unsub();
      unsubGoal();
      unsubMappingPose?.();
      for (const unsub of Object.values(layerUnsubs)) unsub();
      canvas.remove();
      controls.remove();
    },
  };
}
