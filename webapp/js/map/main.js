// @ts-check
// Map page — a plain 2D <canvas> rendering the nav occupancy grid (/map) plus
// the robot's pose (/odom), the planner's route (/plan), and click-to-navigate
// (publishes /goal_pose). Standalone for now; the widget can be embedded into
// teleop later. No three.js — the map is 2D, so a canvas + putImageData is all
// it needs.

import { ros } from "../rosClient.js";
import { initShell } from "../shell.js";
import { mountPage } from "../pageMount.js";
import { MAP_TOPIC, ODOM_TOPIC, PLAN_TOPIC, GOAL_POSE_TOPIC } from "../constants.js";

initShell("map", "../");

const stage = /** @type {HTMLElement} */ (document.getElementById("stage"));
mountPage(stage, "map-view", buildMap);

/**
 * @param {HTMLElement} root
 * @returns {{ destroy: () => void }}
 */
function buildMap(root) {
  const canvas = document.createElement("canvas");
  canvas.className = "map-canvas";
  root.appendChild(canvas);
  const ctx = canvas.getContext("2d");

  const goalBtn = document.createElement("button");
  goalBtn.className = "map-goal-btn sim-button";
  goalBtn.textContent = "Set Goal";
  const controls = document.createElement("div");
  controls.className = "map-controls";
  controls.appendChild(goalBtn);
  root.appendChild(controls);

  // Offscreen 1px-per-cell buffer; scaled to the canvas on draw (crisp + cheap).
  const off = document.createElement("canvas");
  const offCtx = off.getContext("2d");

  /** @type {{ width: number, height: number, resolution: number, originX: number, originY: number } | null} */
  let grid = null;
  /** @type {{ x: number, y: number, yaw: number } | null} */
  let pose = null;
  /** @type {Array<{ x: number, y: number }> | null} world-frame plan points */
  let plan = null;

  // Last draw's grid→canvas placement, so pointer handlers can invert it.
  /** @type {{ ox: number, oy: number, scale: number } | null} */
  let view = null;

  // Goal-setting: click sets the position, drag sets the heading.
  let goalMode = false;
  /** @type {{ start: { x: number, y: number }, cur: { x: number, y: number } } | null} */
  let goalDrag = null;
  /** @type {{ x: number, y: number, yaw: number } | null} the active goal */
  let goalMarker = null;
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

  /** @param {any} msg nav_msgs/Odometry */
  function onOdom(msg) {
    const p = msg?.pose?.pose;
    const x = p?.position?.x;
    const y = p?.position?.y;
    const q = p?.orientation;
    if (typeof x !== "number" || typeof y !== "number" || !q) return;
    const yaw = Math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z));
    pose = { x, y, yaw };
    draw();
  }

  // The planner republishes /plan (~1 Hz) the whole time it's driving to a goal,
  // and stops once it arrives. So a lull in /plan means navigation has ended —
  // that's when we drop the goal marker and the route, rather than on a timer.
  const NAV_STALE_MS = 4000;

  function armNavStale() {
    clearTimeout(navStaleTimer);
    navStaleTimer = setTimeout(() => {
      goalMarker = null;
      plan = null;
      draw();
    }, NAV_STALE_MS);
  }

  /** @param {any} msg nav_msgs/Path */
  function onPlan(msg) {
    const poses = msg?.poses;
    if (!Array.isArray(poses)) return;
    const pts = [];
    for (const ps of poses) {
      const pos = ps?.pose?.position;
      if (typeof pos?.x === "number" && typeof pos?.y === "number") pts.push({ x: pos.x, y: pos.y });
    }
    if (pts.length) {
      plan = pts;
      armNavStale(); // route still streaming → keep the goal visible
    } else {
      plan = null; // empty path = navigation finished/aborted
      goalMarker = null;
      clearTimeout(navStaleTimer);
    }
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

    const pad = 16 * dpr();
    const scale = Math.min((canvas.width - 2 * pad) / grid.width, (canvas.height - 2 * pad) / grid.height);
    const drawW = grid.width * scale;
    const drawH = grid.height * scale;
    const ox = (canvas.width - drawW) / 2;
    const oy = (canvas.height - drawH) / 2;
    view = { ox, oy, scale };
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(off, ox, oy, drawW, drawH);

    if (plan && plan.length >= 2) {
      ctx.strokeStyle = "#00b7ff";
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

    // Goal: a green dot, plus a heading arrow while dragging.
    const goalAt = goalDrag ? { x: goalDrag.start.x, y: goalDrag.start.y } : goalMarker;
    if (goalAt) {
      const { px, py } = worldToCanvas(goalAt.x, goalAt.y);
      const r = Math.max(4, 6 * dpr());
      ctx.fillStyle = "#00ff88";
      ctx.beginPath();
      ctx.arc(px, py, r, 0, Math.PI * 2);
      ctx.fill();
      const yaw = goalDrag ? Math.atan2(goalDrag.cur.y - goalDrag.start.y, goalDrag.cur.x - goalDrag.start.x) : goalMarker?.yaw;
      if (typeof yaw === "number") {
        ctx.strokeStyle = "#00ff88";
        ctx.lineWidth = 2 * dpr();
        ctx.beginPath();
        ctx.moveTo(px, py);
        ctx.lineTo(px + Math.cos(yaw) * r * 2.4, py - Math.sin(yaw) * r * 2.4);
        ctx.stroke();
      }
    }

    if (pose) {
      const { px, py } = worldToCanvas(pose.x, pose.y);
      const rad = Math.max(4, 6 * dpr());
      ctx.fillStyle = "#e8a33d";
      ctx.beginPath();
      ctx.arc(px, py, rad, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = "#e8a33d";
      ctx.lineWidth = 2 * dpr();
      ctx.beginPath();
      ctx.moveTo(px, py);
      ctx.lineTo(px + Math.cos(pose.yaw) * rad * 2.4, py - Math.sin(pose.yaw) * rad * 2.4);
      ctx.stroke();
    }
  }

  /** @param {boolean} on */
  function setGoalMode(on) {
    goalMode = on;
    goalBtn.classList.toggle("is-active", on);
    goalBtn.textContent = on ? "Click map…" : "Set Goal";
    canvas.style.cursor = on ? "crosshair" : "";
    if (!on) goalDrag = null;
  }

  /** @param {number} x @param {number} y @param {number} yaw */
  function publishGoal(x, y, yaw) {
    const qz = Math.sin(yaw / 2);
    const qw = Math.cos(yaw / 2);
    const now = Date.now();
    ros.publish(GOAL_POSE_TOPIC, {
      header: { stamp: { sec: Math.floor(now / 1000), nanosec: (now % 1000) * 1_000_000 }, frame_id: "map" },
      pose: { position: { x, y, z: 0 }, orientation: { x: 0, y: 0, z: qz, w: qw } },
    });
    plan = null; // drop the stale route; the new one streams in on /plan
    goalMarker = { x, y, yaw };
    armNavStale(); // hold the goal until the route starts, then while it runs
  }

  /** @param {PointerEvent} e */
  function onPointerDown(e) {
    if (!goalMode || !grid || !view) return;
    e.preventDefault();
    const { px, py } = eventToCanvas(e);
    const w = canvasToWorld(px, py);
    goalDrag = { start: w, cur: w };
    canvas.setPointerCapture(e.pointerId);
    draw();
  }

  /** @param {PointerEvent} e */
  function onPointerMove(e) {
    if (!goalDrag) return;
    const { px, py } = eventToCanvas(e);
    goalDrag.cur = canvasToWorld(px, py);
    draw();
  }

  /** @param {PointerEvent} e */
  function onPointerUp(e) {
    if (!goalDrag) return;
    const { start, cur } = goalDrag;
    goalDrag = null;
    const dx = cur.x - start.x;
    const dy = cur.y - start.y;
    // Short drag → no meaningful heading, just face "east".
    const yaw = Math.hypot(dx, dy) > 0.1 ? Math.atan2(dy, dx) : 0;
    publishGoal(start.x, start.y, yaw);
    setGoalMode(false);
    draw();
  }

  goalBtn.addEventListener("click", () => setGoalMode(!goalMode));
  canvas.addEventListener("pointerdown", onPointerDown);
  canvas.addEventListener("pointermove", onPointerMove);
  canvas.addEventListener("pointerup", onPointerUp);

  fit();
  const onResize = () => fit();
  window.addEventListener("resize", onResize);
  const unsubMap = ros.subscribe(MAP_TOPIC, onMap, 250);
  const unsubOdom = ros.subscribe(ODOM_TOPIC, onOdom, 100);
  const unsubPlan = ros.subscribe(PLAN_TOPIC, onPlan, 250);

  return {
    destroy() {
      clearTimeout(navStaleTimer);
      window.removeEventListener("resize", onResize);
      unsubMap();
      unsubOdom();
      unsubPlan();
      root.innerHTML = "";
    },
  };
}
