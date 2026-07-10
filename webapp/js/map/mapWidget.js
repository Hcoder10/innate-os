// @ts-check
// Reusable nav-map widget — a plain 2D <canvas> rendering the occupancy grid
// (/map), robot pose (/odom), planner route (/plan), and click-to-navigate
// (sends a NavigateToPose goal via the router). Sizes itself to its container via a ResizeObserver, so
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
} from "../constants.js";

// Wheel-zoom bounds (metres of real-world width shown).
const MIN_ZOOM_M = 1;
const MAX_ZOOM_M = 60;
const ZOOM_STEP = 1.15; // per wheel notch

/**
 * @param {HTMLElement} root container the map fills (sized via ResizeObserver).
 * @param {{ zoom?: number, onZoomChange?: (meters: number) => void }} [opts] zoom = metres of real-world
 *   width to show, centred on the robot pose (keeps the map legible when small); enables scroll-to-zoom.
 *   Omit to fit the whole grid (the standalone page). onZoomChange fires after each wheel-zoom.
 * @returns {{ destroy: () => void, setZoom: (meters: number) => void }}
 */
export function createMap(root, opts = {}) {
  let zoomMeters = opts.zoom;
  const canvas = document.createElement("canvas");
  canvas.className = "map-canvas";
  root.appendChild(canvas);
  const ctx = canvas.getContext("2d");

  const goalBtn = document.createElement("button");
  goalBtn.className = "map-goal-btn sim-button";
  goalBtn.textContent = "Set Goal";
  const stopBtn = document.createElement("button");
  stopBtn.className = "map-stop-btn sim-button";
  stopBtn.textContent = "Stop";
  const controlsRow = document.createElement("div");
  controlsRow.className = "map-controls-row";
  controlsRow.appendChild(goalBtn);
  controlsRow.appendChild(stopBtn);
  // Outcome of the widget's own navigation goals: live progress while
  // driving, then success or a specific failure line (not just "failed").
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
  /** @param {"navigating" | "ok" | "fail" | "muted"} kind @param {string} text @param {boolean} [autoclear] */
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
  // The robot marker must live in the MAP frame like everything else drawn
  // here. /odom alone is the odom frame -- on a real robot that's off by the
  // whole map->odom correction (meters after drift). So: anchor at the last
  // /amcl_pose fix and compose the odometry delta accumulated since, which
  // stays smooth between AMCL updates. With no AMCL (mapfree mode), raw odom
  // IS the global frame and is used directly.
  /** @type {{ x: number, y: number, yaw: number } | null} */
  let amclPose = null;
  /** @type {{ x: number, y: number, yaw: number } | null} odom pose at the AMCL fix */
  let odomAtAmcl = null;
  /** @type {{ x: number, y: number, yaw: number } | null} latest raw odom */
  let odomPose = null;

  function composedPose() {
    if (!amclPose) return odomPose;
    if (!odomAtAmcl || !odomPose) return amclPose;
    // Base motion since the fix, expressed in the base frame at fix time...
    const ca = Math.cos(-odomAtAmcl.yaw);
    const sa = Math.sin(-odomAtAmcl.yaw);
    const dxo = odomPose.x - odomAtAmcl.x;
    const dyo = odomPose.y - odomAtAmcl.y;
    const dx = dxo * ca - dyo * sa;
    const dy = dxo * sa + dyo * ca;
    const dyaw = odomPose.yaw - odomAtAmcl.yaw;
    // ...then re-applied at the AMCL fix in the map frame.
    const c = Math.cos(amclPose.yaw);
    const s = Math.sin(amclPose.yaw);
    return { x: amclPose.x + dx * c - dy * s, y: amclPose.y + dx * s + dy * c, yaw: amclPose.yaw + dyaw };
  }
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
  // True once /nav/commanded_goal set the marker for this navigation: the
  // skill's exact target then wins over the per-replan plan endpoint.
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

  // The planner republishes /plan (~1 Hz) the whole time it's driving to a goal,
  // and stops once it arrives. So a lull in /plan means navigation has ended —
  // that's when we drop the goal marker and the route, rather than on a timer.
  const NAV_STALE_MS = 4000;

  function armNavStale() {
    clearTimeout(navStaleTimer);
    navStaleTimer = setTimeout(() => {
      goalMarker = null;
      goalIsCommanded = false;
      plan = null;
      draw();
    }, NAV_STALE_MS);
  }

  /** @param {any} msg geometry_msgs/PoseStamped — the skill's exact target */
  function onCommandedGoal(msg) {
    // Only trust map-frame targets: odom-frame (local) goals would drift
    // against the map canvas; for those the plan-endpoint fallback at least
    // matches the frame the route itself is drawn in.
    if (msg?.header?.frame_id !== "map") return;
    const pos = msg?.pose?.position;
    const q = msg?.pose?.orientation;
    if (typeof pos?.x !== "number" || typeof pos?.y !== "number" || !q) return;
    goalMarker = { x: pos.x, y: pos.y, yaw: Math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z)) };
    goalIsCommanded = true;
    armNavStale(); // the goal marks an active navigation; expire it like the route
    draw();
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
      // Fallback goal marker: the route's end. Only used until the skill's
      // exact target arrives on /nav/commanded_goal (goalIsCommanded) — the
      // endpoint wiggles with every replan, the commanded goal doesn't. Still
      // needed for navigations that bypass the skill (e.g. map clicks routed
      // straight to bt_navigator).
      if (!goalIsCommanded) {
        const end = poses[poses.length - 1]?.pose;
        const q = end?.orientation;
        if (q) {
          const yaw = Math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z));
          goalMarker = { x: pts[pts.length - 1].x, y: pts[pts.length - 1].y, yaw };
        }
      }
      armNavStale(); // route still streaming → keep the goal visible
    } else {
      plan = null; // empty path = navigation finished/aborted
      goalMarker = null;
      goalIsCommanded = false;
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

    let scale, ox, oy;
    if (zoomMeters && pose) {
      // Robot-centred zoom: show a fixed real-world window (zoomMeters across) centred on the pose, so the
      // map stays legible at thumbnail size instead of fitting the whole world into a few pixels. Anything
      // outside the window is simply clipped by the canvas bounds.
      const cellsAcross = zoomMeters / grid.resolution;
      scale = Math.min(canvas.width, canvas.height) / cellsAcross;
      const poseCol = (pose.x - grid.originX) / grid.resolution;
      const poseRowFromBottom = (pose.y - grid.originY) / grid.resolution;
      ox = canvas.width / 2 - poseCol * scale;
      oy = canvas.height / 2 - (grid.height - poseRowFromBottom) * scale;
    } else {
      // Fit the whole grid (standalone page, or before the first pose arrives).
      const pad = 16 * dpr();
      scale = Math.min((canvas.width - 2 * pad) / grid.width, (canvas.height - 2 * pad) / grid.height);
      ox = (canvas.width - grid.width * scale) / 2;
      oy = (canvas.height - grid.height * scale) / 2;
    }
    view = { ox, oy, scale };
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(off, ox, oy, grid.width * scale, grid.height * scale);

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
    // A NavigateToPose ACTION goal through the router, pinned to the map
    // planner ("navigation"): a map click is a map-frame gesture, so it must
    // never run on whatever planner the previous navigation left latched
    // (the /goal_pose topic bypasses the router and does exactly that).
    // Zero stamp = "latest" to TF. Never wall time: the sim runs on ROS sim
    // time, where a Date.now() stamp is decades in the future.
    const gen = ++goalGen;
    /** @type {{ distance_remaining?: number, number_of_recoveries?: number } | null} */
    let lastFb = null;
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
        // NavigateToPose's result message is empty, so the reason must come
        // from the last feedback: how far it got and how hard it tried.
        const d = lastFb?.distance_remaining;
        const n = lastFb?.number_of_recoveries ?? 0;
        const detail =
          typeof d === "number"
            ? ` ${d.toFixed(1)} m from the goal${n ? ` after ${n} recover${n === 1 ? "y" : "ies"}` : ""} — route blocked or robot stuck?`
            : " — no path (goal unreachable or outside the map?)";
        setStatus("fail", `Failed${detail}`);
      });
    plan = null; // drop the stale route; the new one streams in on /plan
    goalMarker = { x, y, yaw };
    goalIsCommanded = false; // a fresh click supersedes any previous skill goal
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

  goalBtn.addEventListener("click", () => setGoalMode(!goalMode));

  // Stop cancels every active navigation goal server-side, then drops the
  // local goal marker and route.
  let stopRequested = 0; // goal generation the user stopped, for status wording
  stopBtn.addEventListener("click", async () => {
    stopBtn.disabled = true;
    stopBtn.textContent = "Stopping…";
    stopRequested = goalGen;
    try {
      await ros.callService(CANCEL_NAVIGATION_SERVICE, {});
      goalMarker = null;
      goalIsCommanded = false;
      plan = null;
      setGoalMode(false);
      draw();
      stopBtn.textContent = "Stopped";
    } catch (err) {
      console.error("[map] cancel navigation failed:", err);
      stopBtn.textContent = "Stop failed";
    } finally {
      stopBtn.disabled = false;
      setTimeout(() => {
        stopBtn.textContent = "Stop";
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

  const unsubMap = ros.subscribe(MAP_TOPIC, onMap, 250);
  const unsubOdom = ros.subscribe(ODOM_TOPIC, onOdom, 100);
  const unsubAmcl = ros.subscribe(AMCL_POSE_TOPIC, onAmcl, 0, "geometry_msgs/msg/PoseWithCovarianceStamped");
  // Only the active planner publishes, so both feeds can share one handler.
  const unsubPlans = PLAN_TOPICS.map((topic) => ros.subscribe(topic, onPlan, 250, "nav_msgs/msg/Path"));
  const unsubGoal = ros.subscribe(COMMANDED_GOAL_TOPIC, onCommandedGoal, 0, "geometry_msgs/msg/PoseStamped");

  return {
    /** Swap to a saved zoom (e.g. when this widget reparents between thumbnail and full stage). */
    setZoom(meters) {
      if (typeof meters === "number" && meters > 0 && meters !== zoomMeters) {
        zoomMeters = meters;
        draw();
      }
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
      canvas.remove();
      controls.remove();
    },
  };
}
