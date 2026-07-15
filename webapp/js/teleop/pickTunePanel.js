// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Pick-tuning panel — a live workbench for iterating on the pick_any_object
// skill's POSITION and GRASP stages. A crosshair toggle under the telemetry
// card (or "o") opens it; hidden by default so the cockpit stays clean.
//
// Top: a top-down base_link view (robot at the bottom, +x forward/up) that
// plots every metric localization the skill reports on /pick_any_object/debug,
// with the sweet-spot ring, bearing/approach tolerance geometry, and the arm's
// grasp-reach clamp box drawn from the *current* slider values — so you see a
// run converge (or not) against the exact thresholds you set. Under it: a
// side (x–z) view of the grasp — the planned hover→descend ladder from the
// sliders, overlaid with the actual EE positions each stage reports, so a
// missed grasp shows exactly where the descent diverged. Below: sliders
// that publish overrides to /pick_any_object/tuning (applied live, mid-run,
// by the skill) and a compact event log.
//
// The skill also reprojects its grasp target back onto the head frame and
// sends the pixel (grab_px) on the debug topic. We draw a reticle over the
// live video at that pixel — the actual spot on the floor the fingers will
// close on — so you can watch the aim on the real image, not just the plot.
// The detection pixel from each localize event gets its own marker (green box
// corners) so you see where Gemini found the object vs where the arm will
// grab; it clears whenever the base starts moving, since a stored pixel no
// longer matches the shifted view, and reappears on the next localization.
// The head video is object-fit:contain (letterboxed, never cropped), so the
// 640×480 pixel maps to the screen with a single uniform scale + centering.
// When the ARM (wrist) camera is on the big stage instead, the same stage
// hosts the wrist-align overlays: a draggable goal box in raw wrist-image
// pixels (wrist_box_u/v) and the skill's wrist detection marker.
//
// Sync protocol: overrides persist in localStorage and are re-published every
// time the skill reports run_start, which closes the cold-start gap (the
// skill only subscribes once its first run begins). The skill acks every
// tuning message by echoing its live params; "live" in the header means the
// last echo matches our sliders.

import { PICK_DEBUG_TOPIC, PICK_TUNING_TOPIC } from "../constants.js";

const TOGGLE_KEY = "KeyO";
const STORAGE_KEY = "innate.pickTune.overrides";
const PUBLISH_DEBOUNCE_MS = 120;
const LOG_CAP = 40;

// Grasp-target clamp in pick_any_object.py's _grasp_at — drawn as the reach box.
const REACH = { xMin: 0.22, xMax: 0.4, yMax: 0.1 };

/**
 * @typedef {{ key: string, label: string, unit: string, min: number, max: number, step: number, def: number }} ParamSpec
 */

// Mirrors TUNABLE in workspace/custom_skills/pick_any_object.py — keep the
// keys and defaults in sync (defaults here are what Reset publishes).
/** @type {{ label: string, params: ParamSpec[] }[]} */
const PARAM_GROUPS = [
  {
    // Position first: box x (forward) + box y (lateral) are the two coords the
    // video drag writes, kept adjacent so the pair is obvious. box x can aim
    // past the arm's ~0.43 m grasp reach — beyond that the grasp saturates at
    // the reach clamp (positioning still works, the fingers just stop short).
    label: "pick box (image goal)",
    params: [
      { key: "sweet_x", label: "box x", unit: "m", min: 0.2, max: 1.0, step: 0.005, def: 0.3 },
      { key: "box_y", label: "box y", unit: "m", min: -0.12, max: 0.12, step: 0.005, def: 0 },
      { key: "box_half_px", label: "box half", unit: "px", min: 15, max: 120, step: 5, def: 40 },
      { key: "accept_frac", label: "accept", unit: "×", min: 0.2, max: 1, step: 0.05, def: 0.5 },
      { key: "box_steps", label: "max steps", unit: "", min: 2, max: 10, step: 1, def: 6 },
      { key: "bearing_go_deg", label: "bearing go", unit: "°", min: 1, max: 15, step: 0.5, def: 4 },
    ],
  },
  {
    // Pixel-servo gains for the optical-flow follower: rad/s (or m/s) per 100 px
    // of tracked-point error from the box centre.
    label: "follow (px servo)",
    params: [
      { key: "follow_gain_ang", label: "turn gain", unit: "", min: 0.05, max: 1.0, step: 0.05, def: 0.3 },
      { key: "follow_gain_lin", label: "drive gain", unit: "", min: 0.01, max: 0.2, step: 0.01, def: 0.06 },
    ],
  },
  {
    label: "detect",
    params: [
      { key: "tilt_deg", label: "head tilt", unit: "°", min: -35, max: -10, step: 1, def: -20 },
      { key: "settle_s", label: "settle", unit: "s", min: 0.2, max: 3, step: 0.1, def: 1.2 },
    ],
  },
  {
    label: "rotate loop",
    params: [
      { key: "rot_tol_deg", label: "tol", unit: "°", min: 0.5, max: 6, step: 0.25, def: 2.5 },
      { key: "rot_kp", label: "kp", unit: "", min: 0.3, max: 3, step: 0.1, def: 1.2 },
      { key: "rot_wz_max", label: "wz max", unit: "rad/s", min: 0.15, max: 0.8, step: 0.05, def: 0.5 },
      { key: "rot_wz_min", label: "wz min", unit: "rad/s", min: 0.05, max: 0.3, step: 0.01, def: 0.15 },
    ],
  },
  {
    label: "drive loop",
    params: [
      { key: "drive_tol_m", label: "tol", unit: "m", min: 0.005, max: 0.05, step: 0.005, def: 0.015 },
      { key: "drive_kp", label: "kp", unit: "", min: 0.1, max: 1, step: 0.05, def: 0.3 },
      { key: "drive_v_max", label: "v max", unit: "m/s", min: 0.04, max: 0.2, step: 0.01, def: 0.1 },
      { key: "drive_v_min", label: "v min", unit: "m/s", min: 0.02, max: 0.08, step: 0.005, def: 0.04 },
    ],
  },
  {
    // Wrist visual-servo descent: the hover goes to the operator's posed
    // search position (up high, wrist camera on the object), Gemini seeds the
    // object pixel there, then optical flow tracks it on wrist frames while
    // the arm steps down, correcting x/y each cycle; z drops only while the
    // point is inside the wrist box (drag it on the arm-camera view). Gains
    // are SIGNED — the wrist cam is mirrored — so flip one live if the servo
    // diverges instead of centering.
    label: "wrist servo (arm cam)",
    params: [
      { key: "wrist_steps", label: "gemini seeds", unit: "", min: 0, max: 4, step: 1, def: 2 },
      { key: "wrist_stop_z", label: "stop z", unit: "m", min: 0.02, max: 0.12, step: 0.005, def: 0.04 },
      { key: "wrist_z_step", label: "z step", unit: "m", min: 0.005, max: 0.04, step: 0.005, def: 0.01 },
      { key: "wrist_move_s", label: "step t", unit: "s", min: 0.2, max: 1.5, step: 0.1, def: 0.5 },
      { key: "wrist_pitch", label: "pitch", unit: "rad", min: 0.6, max: 1.55, step: 0.01, def: 0.82 },
      { key: "wrist_box_u", label: "box u", unit: "px", min: 0, max: 640, step: 5, def: 320 },
      { key: "wrist_box_v", label: "box v", unit: "px", min: 0, max: 480, step: 5, def: 240 },
      { key: "wrist_half_px", label: "box half", unit: "px", min: 20, max: 160, step: 5, def: 60 },
      { key: "wrist_kx", label: "fwd gain", unit: "", min: -0.12, max: 0.12, step: 0.005, def: -0.04 },
      { key: "wrist_ky", label: "lat gain", unit: "", min: -0.12, max: 0.12, step: 0.005, def: -0.04 },
      { key: "wrist_step_max", label: "step max", unit: "m", min: 0.01, max: 0.08, step: 0.005, def: 0.04 },
      { key: "wrist_settle_s", label: "settle", unit: "s", min: 0.2, max: 2, step: 0.1, def: 0.8 },
    ],
  },
  {
    label: "grasp descent",
    params: [
      { key: "grasp_x_off", label: "x offset", unit: "m", min: 0, max: 0.08, step: 0.005, def: 0.03 },
      { key: "hover_z", label: "hover z", unit: "m", min: 0.08, max: 0.25, step: 0.005, def: 0.15 },
      { key: "hover_s", label: "hover t", unit: "s", min: 0.8, max: 4, step: 0.1, def: 2 },
      { key: "descend_z1", label: "step z1", unit: "m", min: 0.02, max: 0.14, step: 0.005, def: 0.1 },
      { key: "descend_z2", label: "step z2", unit: "m", min: 0.01, max: 0.12, step: 0.005, def: 0.06 },
      { key: "descend_z3", label: "step z3", unit: "m", min: 0, max: 0.1, step: 0.005, def: 0.03 },
      { key: "floor_z", label: "floor z", unit: "m", min: 0, max: 0.05, step: 0.002, def: 0.01 },
      { key: "descend_s", label: "step t", unit: "s", min: 0.4, max: 3, step: 0.1, def: 1.2 },
      { key: "descend_abort_z", label: "abort z", unit: "m", min: 0.05, max: 0.2, step: 0.005, def: 0.12 },
      { key: "arm_pitch", label: "pitch", unit: "rad", min: 1, max: 1.55, step: 0.01, def: 1.3 },
    ],
  },
  {
    label: "grasp close · lift",
    params: [
      { key: "close_strength", label: "strength", unit: "", min: 0.2, max: 1, step: 0.05, def: 0.6 },
      { key: "close_s", label: "close t", unit: "s", min: 0.5, max: 3, step: 0.1, def: 1.5 },
      { key: "close_settle_s", label: "settle", unit: "s", min: 0, max: 2, step: 0.1, def: 0.8 },
      { key: "twist_rad", label: "twist", unit: "rad", min: 0, max: 1.4, step: 0.05, def: 0.6 },
      { key: "lift_rad", label: "lift", unit: "rad", min: 0.2, max: 1.4, step: 0.05, def: 0.6 },
    ],
  },
];

/** @type {ParamSpec[]} */
const ALL_PARAMS = PARAM_GROUPS.flatMap((g) => g.params);

// Top-down view window in base_link meters: x (forward) up, y (left) left.
// xMax spans the full box x range so the goal stays on-canvas at max reach-out.
const VIEW = { xMin: -0.08, xMax: 1.08, canvasW: 296, canvasH: 252 };

// Head-camera frame size the skill's grasp pixel is expressed in (IMG_W/IMG_H
// in pick_any_object.py). Only a fallback — the live <video>'s intrinsic size
// wins when known.
const IMG = { w: 640, h: 480 };

// Head-camera geometry, mirrored from pick_any_object.py (URDF values) so the
// pick box can be drawn from the sliders without asking the skill.
const HEAD_ORIGIN = [-0.040751, -0.0002, 0.25882];
const CAM_IN_HEAD = [0.04327, 0.0297, -0.000275];
const HFOV_DEG = 70;
const FX = IMG.w / (2 * Math.tan(((HFOV_DEG / 2) * Math.PI) / 180));

/** Camera pose (position + axes) for a head tilt. @param {number} tiltDeg */
function camPose(tiltDeg) {
  const t = (tiltDeg * Math.PI) / 180;
  const c = Math.cos(t);
  const s = Math.sin(t);
  /** @param {[number, number, number]} v */
  const rot = (v) => [c * v[0] - s * v[2], v[1], s * v[0] + c * v[2]];
  const r = rot(/** @type {[number, number, number]} */ (CAM_IN_HEAD));
  return {
    cam: [HEAD_ORIGIN[0] + r[0], HEAD_ORIGIN[1] + r[1], HEAD_ORIGIN[2] + r[2]],
    fwd: rot([1, 0, 0]),
    right: rot([0, -1, 0]),
    down: rot([0, 0, -1]),
  };
}

/** Floor point (base_link x,y, z=0) -> image pixel, or null behind the camera.
 *  Mirror of floor_to_pixel in pick_any_object.py.
 *  @param {number} x @param {number} y @param {number} tiltDeg */
function floorToPixel(x, y, tiltDeg) {
  const { cam, fwd, right, down } = camPose(tiltDeg);
  const D = [x - cam[0], y - cam[1], -cam[2]];
  const a = D[0] * fwd[0] + D[1] * fwd[1] + D[2] * fwd[2];
  if (a <= 1e-6) return null;
  const b = D[0] * right[0] + D[1] * right[1] + D[2] * right[2];
  const cc = D[0] * down[0] + D[1] * down[1] + D[2] * down[2];
  return { u: IMG.w / 2 + (b / a) * FX, v: IMG.h / 2 + (cc / a) * FX };
}

/** Image pixel -> floor point, or null above the horizon.
 *  Mirror of pixel_to_floor in pick_any_object.py.
 *  @param {number} u @param {number} v @param {number} tiltDeg */
function pixelToFloor(u, v, tiltDeg) {
  const { cam, fwd, right, down } = camPose(tiltDeg);
  const xo = (u - IMG.w / 2) / FX;
  const yo = (v - IMG.h / 2) / FX;
  const d = [0, 1, 2].map((i) => fwd[i] + xo * right[i] + yo * down[i]);
  if (d[2] >= -1e-6) return null;
  const t = -cam[2] / d[2];
  return { x: cam[0] + t * d[0], y: cam[1] + t * d[1] };
}

/**
 * @param {HTMLElement} parent cockpit root — owns the toggle button + panel.
 * @param {import("../rosClient.js").RosClient} rosClient
 * @param {import("../webrtcSession.js").WebRtcSession} [session]
 *   Video session — tells us which camera fills the big stage: head-camera
 *   overlays (reticle, pick box) only show on "main", wrist-align overlays
 *   only on "arm" (a floor pixel is meaningless on the wrist cam and vice
 *   versa).
 * @returns {{ destroy: () => void }}
 */
export function createPickTunePanel(parent, rosClient, session) {
  const toggleBtn = document.createElement("button");
  toggleBtn.type = "button";
  toggleBtn.className = "picktune-toggle";
  toggleBtn.title = "Pick tuning (o)";
  toggleBtn.setAttribute("aria-label", "Toggle pick-tuning panel");
  toggleBtn.innerHTML =
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<circle cx="12" cy="12" r="7"/><line x1="12" y1="2" x2="12" y2="6"/>' +
    '<line x1="12" y1="18" x2="12" y2="22"/><line x1="2" y1="12" x2="6" y2="12"/>' +
    '<line x1="18" y1="12" x2="22" y2="12"/></svg>';

  const wrap = document.createElement("div");
  wrap.className = "picktune";
  wrap.hidden = true;

  const head = document.createElement("div");
  head.className = "picktune-head";
  const title = document.createElement("span");
  title.className = "microlabel";
  title.textContent = "pick tuning";
  const sync = document.createElement("span");
  sync.className = "picktune-sync mono";
  sync.textContent = "defaults";
  const hint = document.createElement("span");
  hint.className = "profiling-hint mono";
  hint.textContent = "o";
  head.append(title, sync, hint);

  const canvas = document.createElement("canvas");
  canvas.className = "picktune-canvas";

  const sideCanvas = document.createElement("canvas");
  sideCanvas.className = "picktune-canvas picktune-canvas-side";

  const paramsEl = document.createElement("div");
  paramsEl.className = "picktune-params";

  const footer = document.createElement("div");
  footer.className = "picktune-footer";
  const resetBtn = document.createElement("button");
  resetBtn.type = "button";
  resetBtn.className = "sim-button";
  resetBtn.textContent = "Reset defaults";
  footer.append(resetBtn);

  const logHead = document.createElement("p");
  logHead.className = "microlabel";
  logHead.textContent = "events";
  const logEl = document.createElement("div");
  logEl.className = "picktune-log mono";

  wrap.append(head, canvas, sideCanvas, paramsEl, footer, logHead, logEl);
  parent.append(toggleBtn, wrap);

  // Reticle drawn over the live video at the skill's grasp pixel. Lives in the
  // video stage (not the panel) so it shows whether or not the panel is open,
  // and its coordinate origin is the stage — the same box the <video> fills.
  const videoStage = /** @type {HTMLElement | null} */ (parent.querySelector(".video-stage"));
  const reticle = document.createElement("div");
  reticle.className = "picktune-grab";
  reticle.hidden = true;
  reticle.innerHTML =
    '<svg viewBox="0 0 48 48" width="48" height="48" fill="none" stroke="currentColor" ' +
    'stroke-width="1.6" aria-hidden="true">' +
    '<circle cx="24" cy="24" r="13"/>' +
    '<line x1="24" y1="3" x2="24" y2="14"/><line x1="24" y1="34" x2="24" y2="45"/>' +
    '<line x1="3" y1="24" x2="14" y2="24"/><line x1="34" y1="24" x2="45" y2="24"/>' +
    '<circle cx="24" cy="24" r="2.2" fill="currentColor" stroke="none"/></svg>' +
    '<span class="picktune-grab-tag mono">grasp target</span>';
  if (videoStage) videoStage.appendChild(reticle);

  // Detection marker: box corners around the pixel Gemini reported, in the
  // detection color, so "seen here" and "grabbing here" read apart at a glance.
  const seenMark = document.createElement("div");
  seenMark.className = "picktune-grab picktune-seen";
  seenMark.hidden = true;
  seenMark.innerHTML =
    '<svg viewBox="0 0 48 48" width="48" height="48" fill="none" stroke="currentColor" ' +
    'stroke-width="1.6" aria-hidden="true">' +
    '<path d="M8 16 V8 H16"/><path d="M32 8 H40 V16"/>' +
    '<path d="M40 32 V40 H32"/><path d="M16 40 H8 V32"/></svg>' +
    '<span class="picktune-grab-tag mono">detected</span>';
  if (videoStage) videoStage.appendChild(seenMark);

  // The pick box: the positioning goal square, drawn whenever the head video
  // is up (it's the standing aim frame, not per-run state). Turns green while
  // the skill reports the detection inside it.
  const boxGoal = document.createElement("div");
  boxGoal.className = "picktune-boxgoal";
  boxGoal.hidden = true;
  // Inner accept box (accept_frac of the outer): the point must land in THIS to
  // stop positioning. Greens when the skill reports inside; the outer stays a
  // dim guide. Sized in placeReticle from the accept_frac slider.
  boxGoal.innerHTML =
    '<span class="picktune-boxgoal-tag mono">pick box</span>' +
    '<div class="picktune-boxaccept"></div>';
  const boxAccept = /** @type {HTMLElement} */ (boxGoal.querySelector(".picktune-boxaccept"));
  if (videoStage) videoStage.appendChild(boxGoal);

  // The wrist box: the wrist-align goal square, shown when the ARM camera is
  // on the big stage. Same idea as the pick box, but in raw wrist-image pixels
  // (wrist_box_u/v) — no floor projection, the wrist cam has none. Draggable.
  const wristBox = document.createElement("div");
  wristBox.className = "picktune-boxgoal";
  wristBox.hidden = true;
  wristBox.innerHTML = '<span class="picktune-boxgoal-tag mono">wrist box</span>';
  if (videoStage) videoStage.appendChild(wristBox);

  // Wrist detection marker: where Gemini found the object on the wrist image.
  const wristMark = document.createElement("div");
  wristMark.className = "picktune-grab picktune-seen";
  wristMark.hidden = true;
  wristMark.innerHTML =
    '<svg viewBox="0 0 48 48" width="48" height="48" fill="none" stroke="currentColor" ' +
    'stroke-width="1.6" aria-hidden="true">' +
    '<path d="M8 16 V8 H16"/><path d="M32 8 H40 V16"/>' +
    '<path d="M40 32 V40 H32"/><path d="M16 40 H8 V32"/></svg>' +
    '<span class="picktune-grab-tag mono">wrist detect</span>';
  if (videoStage) videoStage.appendChild(wristMark);

  // ---- state ---------------------------------------------------------------

  /** Slider overrides (partial, key -> value). @type {Record<string, number>} */
  let overrides = loadOverrides();
  /** Params last echoed by the skill, or null before any echo. @type {Record<string, number> | null} */
  let skillParams = null;
  /** Localizations of the current run, in arrival order. @type {{ x: number, y: number, final: boolean }[]} */
  let points = [];
  /** @type {{ tx: number, ty: number } | null} */
  let grasp = null;
  /** EE positions the grasp stages report, in arrival order. @type {{ x: number, z: number, kind: "hover"|"descend"|"lift", ok?: boolean }[]} */
  let armTrail = [];
  /** Gripper joint before/after the close, from the "close" event. @type {{ open: number | undefined, closed: number | undefined } | null} */
  let closeJ6 = null;
  /** Skill's grasp target in 640×480 image px, or null when none is active. @type {{ u: number, v: number } | null} */
  let grabPx = null;
  /** Latest detection pixel from localize; null after base motion shifts the view. @type {{ u: number, v: number } | null} */
  let seenPx = null;
  /** Latest wrist-camera detection pixel from wrist_align. @type {{ u: number, v: number } | null} */
  let wristPx = null;
  let running = false;
  /** @type {number | null} */
  let publishTimer = null;
  /** @type {Map<string, { row: HTMLElement, slider: HTMLInputElement, value: HTMLElement }>} */
  const rows = new Map();

  /** @returns {Record<string, number>} */
  function loadOverrides() {
    /** @type {Record<string, number>} */
    const out = {};
    try {
      const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "{}");
      for (const spec of ALL_PARAMS) {
        const v = parsed?.[spec.key];
        if (typeof v === "number" && Number.isFinite(v)) out[spec.key] = v;
      }
    } catch {
      // Corrupt storage — start from code defaults.
    }
    return out;
  }

  function saveOverrides() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(overrides));
    } catch {
      // Storage unavailable (private mode) — tuning still works, just not remembered.
    }
  }

  /** Effective value of a param: the override, else the code default. @param {ParamSpec} spec */
  function valueOf(spec) {
    return overrides[spec.key] ?? spec.def;
  }

  // ---- publishing ------------------------------------------------------------

  /**
   * Publish parameter values to the skill, debounced so slider drags don't
   * flood rosbridge. `payload` is merged skill-side (unknown keys ignored).
   * @param {Record<string, number>} payload
   */
  function publishTuning(payload) {
    if (publishTimer !== null) clearTimeout(publishTimer);
    publishTimer = window.setTimeout(() => {
      publishTimer = null;
      // The skill advertises/subscribes these topics at skills-server load
      // (see guidelines() in pick_any_object.py), so this publish only fails
      // when the server itself is down — the run_start republish then recovers.
      if (rosClient.state === "connected") {
        rosClient.publish(PICK_TUNING_TOPIC, { data: JSON.stringify(payload) });
      }
    }, PUBLISH_DEBOUNCE_MS);
  }

  function publishOverrides() {
    if (Object.keys(overrides).length > 0) publishTuning(overrides);
  }

  // ---- params UI -------------------------------------------------------------

  for (const group of PARAM_GROUPS) {
    const groupLabel = document.createElement("p");
    groupLabel.className = "microlabel picktune-group";
    groupLabel.textContent = group.label;
    paramsEl.appendChild(groupLabel);
    for (const spec of group.params) paramsEl.appendChild(buildRow(spec));
  }

  /** @param {ParamSpec} spec */
  function buildRow(spec) {
    const row = document.createElement("div");
    row.className = "picktune-param";

    const label = document.createElement("span");
    label.className = "picktune-param-label";
    label.textContent = spec.label;

    const slider = document.createElement("input");
    slider.type = "range";
    slider.className = "picktune-slider";
    slider.min = String(spec.min);
    slider.max = String(spec.max);
    slider.step = String(spec.step);
    slider.value = String(valueOf(spec));

    const value = document.createElement("span");
    value.className = "picktune-param-value mono";

    slider.addEventListener("input", () => {
      const v = Number(slider.value);
      if (v === spec.def) delete overrides[spec.key];
      else overrides[spec.key] = v;
      saveOverrides();
      // Send the full override set (idempotent merge) so a lost message never
      // leaves the skill holding a stale sibling value.
      publishTuning(Object.keys(overrides).length > 0 ? overrides : { [spec.key]: v });
      syncRow(spec);
      syncHeader();
      draw();
    });

    row.append(label, slider, value);
    rows.set(spec.key, { row, slider, value });
    syncRow(spec);
    return row;
  }

  /** Repaint one row: value text + dirty highlight. @param {ParamSpec} spec */
  function syncRow(spec) {
    const r = rows.get(spec.key);
    if (!r) return;
    const v = valueOf(spec);
    const decimals = spec.step >= 1 ? 0 : spec.step >= 0.05 ? 2 : 3;
    r.value.textContent = `${v.toFixed(decimals)}${spec.unit}`;
    r.row.classList.toggle("dirty", spec.key in overrides);
  }

  /** Header pill: defaults / edited / live (skill echo matches sliders). */
  function syncHeader() {
    const dirty = Object.keys(overrides).length > 0;
    let text = dirty ? "edited" : "defaults";
    let live = false;
    if (skillParams) {
      live = ALL_PARAMS.every((spec) => {
        const echoed = skillParams?.[spec.key];
        return typeof echoed !== "number" || Math.abs(echoed - valueOf(spec)) < 1e-6;
      });
      if (live) text = dirty ? "live · edited" : "live · defaults";
    }
    sync.textContent = text;
    sync.classList.toggle("live", live);
  }

  resetBtn.addEventListener("click", () => {
    overrides = {};
    saveOverrides();
    // Explicit full-defaults publish so a running skill reverts immediately.
    publishTuning(Object.fromEntries(ALL_PARAMS.map((s) => [s.key, s.def])));
    for (const spec of ALL_PARAMS) {
      const r = rows.get(spec.key);
      if (r) r.slider.value = String(spec.def);
      syncRow(spec);
    }
    syncHeader();
    draw();
  });

  // ---- top-down canvas ---------------------------------------------------

  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  canvas.width = VIEW.canvasW * dpr;
  canvas.height = VIEW.canvasH * dpr;
  const ctx = canvas.getContext("2d");
  const scale = VIEW.canvasH / (VIEW.xMax - VIEW.xMin); // px per meter

  // Side (x–z) view window for the grasp: forward right, up up. Spans the
  // reach box plus margin; z covers floor to a little above max hover.
  const SIDE = { xMin: 0.14, xMax: 0.48, zMin: -0.02, zMax: 0.28, canvasH: 150 };
  sideCanvas.width = VIEW.canvasW * dpr;
  sideCanvas.height = SIDE.canvasH * dpr;
  const sctx = sideCanvas.getContext("2d");

  /** base_link (x fwd, y left) -> canvas px. @param {number} x @param {number} y */
  function toPx(x, y) {
    return {
      px: VIEW.canvasW / 2 - y * scale,
      py: VIEW.canvasH - (x - VIEW.xMin) * scale,
    };
  }

  function draw() {
    if (!ctx || wrap.hidden) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, VIEW.canvasW, VIEW.canvasH);
    const css = getComputedStyle(wrap);
    const cHair = css.getPropertyValue("--hairline") || "rgb(255 255 255 / 8%)";
    const cHairStrong = css.getPropertyValue("--hairline-strong") || "rgb(255 255 255 / 16%)";
    const cMuted = css.getPropertyValue("--muted") || "#8a8a93";
    const cAccent = css.getPropertyValue("--accent") || "#e8a33d";
    const cAccentDim = css.getPropertyValue("--accent-dim") || "rgb(232 163 61 / 40%)";
    const cOk = css.getPropertyValue("--ok") || "#56c28c";

    // 0.1 m grid.
    ctx.lineWidth = 1;
    ctx.strokeStyle = cHair;
    ctx.beginPath();
    for (let gx = 0; gx <= VIEW.xMax; gx += 0.1) {
      const { py } = toPx(gx, 0);
      ctx.moveTo(0, py);
      ctx.lineTo(VIEW.canvasW, py);
    }
    const yHalf = VIEW.canvasW / 2 / scale;
    for (let gy = 0; gy <= yHalf; gy += 0.1) {
      for (const s of gy === 0 ? [0] : [-1, 1]) {
        const { px } = toPx(0, gy * s);
        ctx.moveTo(px, 0);
        ctx.lineTo(px, VIEW.canvasH);
      }
    }
    ctx.stroke();

    const sweetX = valueOf(ALL_PARAMS.find((p) => p.key === "sweet_x") ?? PARAM_GROUPS[0].params[0]);
    const bearingGo = ((overrides.bearing_go_deg ?? 4) * Math.PI) / 180;

    // Bearing-go wedge from the robot origin.
    const origin = toPx(0, 0);
    ctx.strokeStyle = cHairStrong;
    ctx.setLineDash([3, 4]);
    ctx.beginPath();
    for (const s of [-1, 1]) {
      const end = toPx(0.72 * Math.cos(bearingGo), 0.72 * Math.sin(bearingGo) * s);
      ctx.moveTo(origin.px, origin.py);
      ctx.lineTo(end.px, end.py);
    }
    ctx.stroke();

    // Sweet-spot ring (distance target).
    ctx.strokeStyle = cAccentDim;
    ctx.beginPath();
    ctx.arc(origin.px, origin.py, sweetX * scale, -Math.PI / 2 - 0.5, -Math.PI / 2 + 0.5);
    ctx.stroke();
    ctx.setLineDash([]);

    // Pick box footprint: the image-goal square back-projected onto the floor.
    // Outer = the drawn box (box_half_px); inner = the accept box the detection
    // must land in to stop positioning (accept_frac × outer, solid).
    const center = floorToPixel(sweetX, paramValue("box_y"), paramValue("tilt_deg"));
    if (center) {
      const half = paramValue("box_half_px");
      const tilt = paramValue("tilt_deg");
      /** @param {number} h @param {string} color */
      const footprint = (h, color) => {
        const corners = [
          [-h, -h],
          [h, -h],
          [h, h],
          [-h, h],
        ].map(([du, dv]) => pixelToFloor(center.u + du, center.v + dv, tilt));
        if (!corners.every(Boolean)) return;
        ctx.strokeStyle = color;
        ctx.beginPath();
        corners.forEach((pt, i) => {
          const { px, py } = toPx(/** @type {any} */ (pt).x, /** @type {any} */ (pt).y);
          if (i === 0) ctx.moveTo(px, py);
          else ctx.lineTo(px, py);
        });
        ctx.closePath();
        ctx.stroke();
      };
      ctx.setLineDash([3, 3]);
      footprint(half, cAccentDim);
      ctx.setLineDash([]);
      footprint(half * paramValue("accept_frac"), cAccent);
    }

    // Arm-reach clamp box (grasp tx/ty limits).
    const boxA = toPx(REACH.xMax, REACH.yMax);
    const boxB = toPx(REACH.xMin, -REACH.yMax);
    ctx.strokeStyle = cMuted;
    ctx.setLineDash([2, 3]);
    ctx.strokeRect(boxA.px, boxA.py, boxB.px - boxA.px, boxB.py - boxA.py);
    ctx.setLineDash([]);

    // Aim-point crosshair (box centre on the floor).
    const sweet = toPx(sweetX, paramValue("box_y"));
    ctx.strokeStyle = cAccent;
    ctx.beginPath();
    ctx.moveTo(sweet.px - 5, sweet.py);
    ctx.lineTo(sweet.px + 5, sweet.py);
    ctx.moveTo(sweet.px, sweet.py - 5);
    ctx.lineTo(sweet.px, sweet.py + 5);
    ctx.stroke();

    // Robot base at origin with a forward notch.
    ctx.strokeStyle = cMuted;
    const half = 0.09 * scale;
    ctx.strokeRect(origin.px - half, origin.py - 0.06 * scale, half * 2, 0.12 * scale);
    ctx.beginPath();
    ctx.moveTo(origin.px, origin.py - 0.06 * scale);
    ctx.lineTo(origin.px, origin.py - 0.1 * scale);
    ctx.stroke();

    // Localization trail: older estimates dim, the latest bright; the run's
    // final (position_done) estimate ringed in green.
    ctx.strokeStyle = cHairStrong;
    ctx.beginPath();
    points.forEach((pt, i) => {
      const { px, py } = toPx(pt.x, pt.y);
      if (i === 0) ctx.moveTo(px, py);
      else ctx.lineTo(px, py);
    });
    ctx.stroke();
    points.forEach((pt, i) => {
      const { px, py } = toPx(pt.x, pt.y);
      const last = i === points.length - 1;
      ctx.fillStyle = last ? cAccent : cMuted;
      ctx.beginPath();
      ctx.arc(px, py, last ? 4 : 2.5, 0, Math.PI * 2);
      ctx.fill();
      if (pt.final) {
        ctx.strokeStyle = cOk;
        ctx.beginPath();
        ctx.arc(px, py, 6.5, 0, Math.PI * 2);
        ctx.stroke();
      }
    });

    // Grasp target (post-clamp tx/ty) as a hollow diamond.
    if (grasp) {
      const { px, py } = toPx(grasp.tx, grasp.ty);
      ctx.strokeStyle = cAccent;
      ctx.beginPath();
      ctx.moveTo(px, py - 5);
      ctx.lineTo(px + 5, py);
      ctx.lineTo(px, py + 5);
      ctx.lineTo(px - 5, py);
      ctx.closePath();
      ctx.stroke();
    }

    drawSide();
    placeReticle();
  }

  /** Effective value by key (override else code default). @param {string} key */
  function paramValue(key) {
    const spec = ALL_PARAMS.find((s) => s.key === key);
    return spec ? valueOf(spec) : 0;
  }

  /** base_link (x fwd, z up) -> side-canvas px. @param {number} x @param {number} z */
  function toSidePx(x, z) {
    return {
      px: ((x - SIDE.xMin) / (SIDE.xMax - SIDE.xMin)) * VIEW.canvasW,
      py: SIDE.canvasH - ((z - SIDE.zMin) / (SIDE.zMax - SIDE.zMin)) * SIDE.canvasH,
    };
  }

  /** Side view: planned hover→descend ladder from the sliders, the abort
   *  threshold, and the EE positions the running grasp actually reported. */
  function drawSide() {
    if (!sctx || wrap.hidden) return;
    sctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    sctx.clearRect(0, 0, VIEW.canvasW, SIDE.canvasH);
    const css = getComputedStyle(wrap);
    const cHair = css.getPropertyValue("--hairline") || "rgb(255 255 255 / 8%)";
    const cHairStrong = css.getPropertyValue("--hairline-strong") || "rgb(255 255 255 / 16%)";
    const cMuted = css.getPropertyValue("--muted") || "#8a8a93";
    const cText = css.getPropertyValue("--text") || "#e7e7ea";
    const cAccent = css.getPropertyValue("--accent") || "#e8a33d";
    const cOk = css.getPropertyValue("--ok") || "#56c28c";
    const cDanger = css.getPropertyValue("--danger") || "#d96a5a";

    // Grid: 0.1 m in x, 0.05 m in z.
    sctx.lineWidth = 1;
    sctx.strokeStyle = cHair;
    sctx.beginPath();
    for (let gx = Math.ceil(SIDE.xMin * 10) / 10; gx <= SIDE.xMax; gx += 0.1) {
      const { px } = toSidePx(gx, 0);
      sctx.moveTo(px, 0);
      sctx.lineTo(px, SIDE.canvasH);
    }
    for (let gz = 0.05; gz <= SIDE.zMax; gz += 0.05) {
      const { py } = toSidePx(0, gz);
      sctx.moveTo(0, py);
      sctx.lineTo(VIEW.canvasW, py);
    }
    sctx.stroke();

    // The floor, and the descend-abort threshold above it.
    const floor = toSidePx(SIDE.xMin, 0);
    sctx.strokeStyle = cHairStrong;
    sctx.beginPath();
    sctx.moveTo(0, floor.py);
    sctx.lineTo(VIEW.canvasW, floor.py);
    sctx.stroke();
    const abortY = toSidePx(0, paramValue("descend_abort_z")).py;
    sctx.strokeStyle = cDanger;
    sctx.setLineDash([2, 4]);
    sctx.beginPath();
    sctx.moveTo(0, abortY);
    sctx.lineTo(VIEW.canvasW, abortY);
    sctx.stroke();
    sctx.setLineDash([]);

    // Planned ladder at the run's clamped tx (else derived from the sliders):
    // hover → z1 → z2 → z3 → floor, with the wrist-pitch pointer at hover.
    const sweetX = paramValue("sweet_x");
    const lx = grasp ? grasp.tx : Math.max(REACH.xMin, Math.min(REACH.xMax, sweetX - paramValue("grasp_x_off")));
    const hoverZ = paramValue("hover_z");
    const ladder = [hoverZ, paramValue("descend_z1"), paramValue("descend_z2"), paramValue("descend_z3"), paramValue("floor_z")];
    sctx.strokeStyle = cHairStrong;
    sctx.setLineDash([2, 3]);
    sctx.beginPath();
    const top = toSidePx(lx, hoverZ);
    const bottom = toSidePx(lx, ladder[ladder.length - 1]);
    sctx.moveTo(top.px, top.py);
    sctx.lineTo(bottom.px, bottom.py);
    sctx.stroke();
    sctx.setLineDash([]);
    for (const z of ladder) {
      const { px, py } = toSidePx(lx, z);
      sctx.strokeStyle = z === hoverZ ? cAccent : cMuted;
      sctx.beginPath();
      if (z === hoverZ) {
        sctx.arc(px, py, 4, 0, Math.PI * 2);
      } else {
        sctx.moveTo(px - 4, py);
        sctx.lineTo(px + 4, py);
      }
      sctx.stroke();
    }
    // Pitch pointer: the wrist approach direction at the hover point.
    const pitch = paramValue("arm_pitch");
    sctx.strokeStyle = cAccent;
    sctx.beginPath();
    sctx.moveTo(top.px, top.py);
    const tip = toSidePx(lx + 0.05 * Math.cos(pitch), hoverZ - 0.05 * Math.sin(pitch));
    sctx.lineTo(tip.px, tip.py);
    sctx.stroke();

    // Actual EE trail from the run's hover/descend/lift events.
    sctx.strokeStyle = cHairStrong;
    sctx.beginPath();
    armTrail.forEach((pt, i) => {
      const { px, py } = toSidePx(pt.x, pt.z);
      if (i === 0) sctx.moveTo(px, py);
      else sctx.lineTo(px, py);
    });
    sctx.stroke();
    armTrail.forEach((pt, i) => {
      const { px, py } = toSidePx(pt.x, pt.z);
      const last = i === armTrail.length - 1;
      sctx.fillStyle =
        pt.kind === "descend" && pt.ok === false ? cDanger : pt.kind === "lift" ? cOk : last ? cAccent : cText;
      sctx.beginPath();
      sctx.arc(px, py, last ? 4 : 2.5, 0, Math.PI * 2);
      sctx.fill();
      if (pt.kind === "lift") {
        sctx.strokeStyle = cOk;
        sctx.beginPath();
        sctx.arc(px, py, 6.5, 0, Math.PI * 2);
        sctx.stroke();
      }
    });

    // Close outcome, bottom-left: gripper joint before → after the squeeze.
    if (closeJ6) {
      sctx.fillStyle = cMuted;
      sctx.font = "10px " + (css.getPropertyValue("--font-mono") || "monospace");
      const fmt = (/** @type {number | undefined} */ v) => (typeof v === "number" ? v.toFixed(3) : "?");
      sctx.fillText(`close j6 ${fmt(closeJ6.open)} → ${fmt(closeJ6.closed)}`, 6, SIDE.canvasH - 6);
    }
  }

  // ---- grasp reticle over the live video ----------------------------------

  /** True when the head camera fills the big stage (a floor pixel only maps
   *  onto that view). Defaults to true when no session was passed. */
  function headCamPrimary() {
    return !session || session.primaryCamera?.name === "main";
  }

  /** True when the wrist ("arm") camera fills the big stage — the view the
   *  wrist-align box and detection marker live on. */
  function wristCamPrimary() {
    return session?.primaryCamera?.name === "arm";
  }

  /** Letterbox geometry of the live video inside the stage (object-fit:
   *  contain → one uniform scale + symmetric offsets), or null when no video
   *  is up. Camera-agnostic — both cams are 640×480 and the video's intrinsic
   *  size wins; callers gate on WHICH camera is primary.
   *  @returns {{ s: number, offX: number, offY: number } | null} */
  function stageGeom() {
    const video = /** @type {HTMLVideoElement | null} */ (videoStage?.querySelector("video"));
    if (!video || !video.srcObject) return null;
    const boxW = videoStage ? videoStage.clientWidth : 0;
    const boxH = videoStage ? videoStage.clientHeight : 0;
    if (!boxW || !boxH) return null;
    const iw = video.videoWidth || IMG.w;
    const ih = video.videoHeight || IMG.h;
    const s = Math.min(boxW / iw, boxH / ih);
    return { s, offX: (boxW - iw * s) / 2, offY: (boxH - ih * s) / 2 };
  }

  /** Position (or hide) the image-pixel markers and the goal boxes over the
   *  video — head-camera overlays when the head cam is primary, wrist-camera
   *  overlays when the arm cam is. */
  function placeReticle() {
    const g = stageGeom();
    const headG = g && headCamPrimary() ? g : null;
    const wristG = g && wristCamPrimary() ? g : null;
    const markers = [
      { el: reticle, px: grabPx, geom: headG },
      { el: seenMark, px: seenPx, geom: headG },
      { el: wristMark, px: wristPx, geom: wristG },
    ];
    for (const { el, px, geom } of markers) {
      if (!px || !geom) {
        el.hidden = true;
        continue;
      }
      el.style.left = `${geom.offX + px.u * geom.s}px`;
      el.style.top = `${geom.offY + px.v * geom.s}px`;
      el.hidden = false;
    }
    // The wrist-align goal square, straight from the pixel sliders.
    if (wristG) {
      const side = 2 * paramValue("wrist_half_px") * wristG.s;
      wristBox.style.left = `${wristG.offX + paramValue("wrist_box_u") * wristG.s}px`;
      wristBox.style.top = `${wristG.offY + paramValue("wrist_box_v") * wristG.s}px`;
      wristBox.style.width = `${side}px`;
      wristBox.style.height = `${side}px`;
      wristBox.hidden = false;
    } else {
      wristBox.hidden = true;
    }
    // The pick box goal square, from the current sliders.
    const center = headG ? floorToPixel(paramValue("sweet_x"), paramValue("box_y"), paramValue("tilt_deg")) : null;
    if (!headG || !center) {
      boxGoal.hidden = true;
      return;
    }
    const side = 2 * paramValue("box_half_px") * headG.s;
    boxGoal.style.left = `${headG.offX + center.u * headG.s}px`;
    boxGoal.style.top = `${headG.offY + center.v * headG.s}px`;
    boxGoal.style.width = `${side}px`;
    boxGoal.style.height = `${side}px`;
    // Inner accept box, centered, sized as a fraction of the outer.
    const acceptPct = `${paramValue("accept_frac") * 100}%`;
    boxAccept.style.width = acceptPct;
    boxAccept.style.height = acceptPct;
    boxGoal.hidden = false;
  }

  // Video letterbox geometry shifts on any stage resize; the reticle's validity
  // also flips when the primary camera or the stream changes (switch to wrist
  // cam, stream drop). Re-place on both so a mid-grasp camera switch is instant,
  // not delayed to the next debug event.
  const onResize = () => placeReticle();
  window.addEventListener("resize", onResize);
  const unsubSession = session ? session.onChange(() => placeReticle()) : null;

  // ---- drag the pick box on the video ---------------------------------------
  // Dragging converts the cursor back to a floor point (pixelToFloor) and
  // writes it into the same tunables the skill aims at: sweet_x (forward) and
  // box_y (lateral). Sliders, overrides, and the skill stay in sync — the box
  // is the tool, the params are the truth.

  /** Set a param as if its slider was moved: snap to step, clamp to range,
   *  update the row UI and the override map. @param {string} key @param {number} v */
  function setParamValue(key, v) {
    const spec = ALL_PARAMS.find((p) => p.key === key);
    const r = rows.get(key);
    if (!spec || !r) return;
    const snapped = Math.max(spec.min, Math.min(spec.max, Math.round(v / spec.step) * spec.step));
    r.slider.value = String(snapped);
    if (Math.abs(snapped - spec.def) < 1e-9) delete overrides[key];
    else overrides[key] = snapped;
    syncRow(spec);
  }

  let draggingBox = false;

  /** @param {PointerEvent} e */
  function dragBoxTo(e) {
    const g = stageGeom();
    if (!g || !videoStage) return;
    const rect = videoStage.getBoundingClientRect();
    const u = (e.clientX - rect.left - g.offX) / g.s;
    const v = (e.clientY - rect.top - g.offY) / g.s;
    const f = pixelToFloor(u, v, paramValue("tilt_deg"));
    if (!f) return; // above the horizon — no floor point there
    setParamValue("sweet_x", f.x);
    setParamValue("box_y", f.y);
    saveOverrides();
    // Explicit effective values: a drag back to defaults must still reach a
    // skill that holds the old aim.
    publishTuning({ ...overrides, sweet_x: paramValue("sweet_x"), box_y: paramValue("box_y") });
    syncHeader();
    draw(); // re-derives the box from the (clamped) params — cursor stays honest
    placeReticle();
  }

  boxGoal.addEventListener("pointerdown", (e) => {
    if (!stageGeom()) return;
    draggingBox = true;
    boxGoal.classList.add("dragging");
    boxGoal.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  boxGoal.addEventListener("pointermove", (e) => {
    if (draggingBox) dragBoxTo(e);
  });
  const endBoxDrag = () => {
    draggingBox = false;
    boxGoal.classList.remove("dragging");
  };
  boxGoal.addEventListener("pointerup", endBoxDrag);
  boxGoal.addEventListener("pointercancel", endBoxDrag);

  // Drag the wrist box on the arm-camera view — writes wrist_box_u/v (raw
  // wrist-image pixels) into the same slider/override machinery.
  let draggingWrist = false;

  /** @param {PointerEvent} e */
  function dragWristTo(e) {
    const g = stageGeom();
    if (!g || !videoStage) return;
    const rect = videoStage.getBoundingClientRect();
    setParamValue("wrist_box_u", (e.clientX - rect.left - g.offX) / g.s);
    setParamValue("wrist_box_v", (e.clientY - rect.top - g.offY) / g.s);
    saveOverrides();
    // Explicit effective values — a drag back to defaults must still reach a
    // skill that holds the old aim.
    publishTuning({ ...overrides, wrist_box_u: paramValue("wrist_box_u"), wrist_box_v: paramValue("wrist_box_v") });
    syncHeader();
    placeReticle();
  }

  wristBox.addEventListener("pointerdown", (e) => {
    if (!stageGeom()) return;
    draggingWrist = true;
    wristBox.classList.add("dragging");
    wristBox.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  wristBox.addEventListener("pointermove", (e) => {
    if (draggingWrist) dragWristTo(e);
  });
  const endWristDrag = () => {
    draggingWrist = false;
    wristBox.classList.remove("dragging");
  };
  wristBox.addEventListener("pointerup", endWristDrag);
  wristBox.addEventListener("pointercancel", endWristDrag);

  // ---- event log + debug feed ---------------------------------------------

  /** @param {string} line @param {boolean} [warn] */
  function logLine(line, warn = false) {
    const el = document.createElement("div");
    el.className = "picktune-log-line" + (warn ? " warn" : "");
    el.textContent = line;
    logEl.appendChild(el);
    while (logEl.childElementCount > LOG_CAP) logEl.firstElementChild?.remove();
    logEl.scrollTop = logEl.scrollHeight;
  }

  /** @param {number | undefined} n @param {number} [d] */
  const num = (n, d = 2) => (typeof n === "number" ? n.toFixed(d) : "?");

  /** @param {any} ev */
  function onDebugEvent(ev) {
    switch (ev.ev) {
      case "run_start":
        points = [];
        grasp = null;
        armTrail = [];
        closeJ6 = null;
        grabPx = null;
        seenPx = null;
        wristPx = null;
        boxGoal.classList.remove("inside");
        wristBox.classList.remove("inside");
        running = true;
        skillParams = ev.params ?? skillParams;
        logLine(`run start "${ev.prompt ?? ""}"`);
        // The skill just (re)subscribed — reassert our overrides so a fresh
        // instance never runs on stale/default values while sliders say otherwise.
        publishOverrides();
        break;
      case "run_end":
        running = false;
        grabPx = null;
        seenPx = null;
        wristPx = null;
        boxGoal.classList.remove("inside");
        wristBox.classList.remove("inside");
        logLine("run end");
        break;
      case "params":
        // No log line — the header's live/edited pill reflects it, and after
        // hot-reloads retired skill instances echo too (identical acks).
        skillParams = ev.params ?? null;
        break;
      case "localize":
        seenPx = Array.isArray(ev.px) ? { u: ev.px[0], v: ev.px[1] } : null;
        if (Array.isArray(ev.xy)) {
          points.push({ x: ev.xy[0], y: ev.xy[1], final: false });
          logLine(`loc px(${num(ev.px?.[0], 0)},${num(ev.px?.[1], 0)}) → (${num(ev.xy[0], 3)}, ${num(ev.xy[1], 3)})`);
        } else {
          logLine("loc — not found", true);
        }
        break;
      case "servo":
        // High-rate (~10 Hz) optical-flow tracking updates during the follow.
        // Glide the marker to the tracked point and colour the box; no log line
        // (per-tick logging would swamp the event feed).
        if (Array.isArray(ev.px)) seenPx = { u: ev.px[0], v: ev.px[1] };
        boxGoal.classList.toggle("inside", ev.inside === true);
        break;
      case "position":
        boxGoal.classList.toggle("inside", ev.inside === true);
        logLine(
          `pos ${num(ev.step, 0)}: px(${num(ev.px?.[0], 0)},${num(ev.px?.[1], 0)}) ` +
            (ev.inside ? "IN box" : "out — correcting"),
          false,
        );
        break;
      case "position_done":
        if (Array.isArray(ev.xy)) {
          const last = points[points.length - 1];
          if (last && last.x === ev.xy[0] && last.y === ev.xy[1]) last.final = true;
          const n = ev.attempts ?? ev.steps;
          logLine(`pos done — in box${typeof n === "number" ? ` (${n} re-detects)` : ""}`);
        } else {
          boxGoal.classList.remove("inside");
          logLine(`pos failed — ${ev.reason ?? "lost object"}`, true);
        }
        break;
      case "rotate":
        seenPx = null; // view is about to shift — the stored pixel goes stale
        logLine(`rot ${num(ev.angle_deg, 1)}°`);
        break;
      case "rotate_done":
        logLine(`rot done err ${num(ev.err_deg, 1)}° in ${num(ev.s, 1)}s`);
        break;
      case "drive":
        seenPx = null; // same — the base is moving under the camera
        logLine(`drv ${num(ev.dist_m, 3)}m`);
        break;
      case "drive_done":
        logLine(`drv done err ${num(ev.err_m, 3)}m in ${num(ev.s, 1)}s`);
        break;
      case "grasp":
        // A fresh attempt (the deep retry re-enters here) — clear the old trail.
        grasp = { tx: ev.tx, ty: ev.ty };
        armTrail = [];
        closeJ6 = null;
        grabPx = Array.isArray(ev.grab_px) ? { u: ev.grab_px[0], v: ev.grab_px[1] } : null;
        // Pin the "detected" marker to the object point so the amber grasp
        // target and the green object marker sit side by side — their gap is
        // the reach clamp (grasp aims short when the object is out of reach).
        if (Array.isArray(ev.obj_px)) seenPx = { u: ev.obj_px[0], v: ev.obj_px[1] };
        logLine(
          `grasp tx ${num(ev.tx, 3)} ty ${num(ev.ty, 3)}${ev.deep ? " (deep)" : ""}` +
            (ev.clamped ? " — REACH-CLAMPED: grasp short of object (lower box x)" : ""),
          ev.clamped === true,
        );
        break;
      case "hover":
        if (Array.isArray(ev.ee)) armTrail.push({ x: ev.ee[0], z: ev.ee[2], kind: "hover" });
        logLine(`hover ee z ${num(ev.ee?.[2], 3)}`);
        break;
      case "wrist_seed":
        // A Gemini look on the wrist image (initial seed or re-seed on loss).
        wristPx = Array.isArray(ev.px) ? { u: ev.px[0], v: ev.px[1] } : null;
        if (wristPx) {
          logLine(`wrist seed px(${num(ev.px?.[0], 0)},${num(ev.px?.[1], 0)}) @z ${num(ev.z, 2)}`);
        } else {
          logLine("wrist seed — not seen", true);
        }
        break;
      case "wrist_servo":
        // Per-cycle optical-flow tracking during the servo descent: glide the
        // marker, colour the box, extend the side-view EE trail. No log line
        // (a full descent is ~10-20 cycles and would swamp the feed).
        if (Array.isArray(ev.px)) wristPx = { u: ev.px[0], v: ev.px[1] };
        wristBox.classList.toggle("inside", ev.inside === true);
        grasp = { tx: ev.tx, ty: ev.ty };
        if (Array.isArray(ev.ee)) armTrail.push({ x: ev.ee[0], z: ev.ee[2], kind: "descend", ok: true });
        break;
      case "wrist_done":
        // Refined grasp target — re-pin the head-view reticle to match.
        grasp = { tx: ev.tx, ty: ev.ty };
        grabPx = Array.isArray(ev.grab_px) ? { u: ev.grab_px[0], v: ev.grab_px[1] } : grabPx;
        logLine(
          `wrist done @z ${num(ev.z, 2)} — ${ev.reason ?? "?"} (tx ${num(ev.tx, 3)} ty ${num(ev.ty, 3)})`,
          ev.reason !== "reached stop z",
        );
        break;
      case "descend":
        if (Array.isArray(ev.ee)) armTrail.push({ x: ev.ee[0], z: ev.ee[2], kind: "descend", ok: ev.ok !== false });
        logLine(`des → ${num(ev.z, 3)} ee z ${num(ev.ee?.[2], 3)}${ev.ok === false ? " contact" : ""}`, ev.ok === false);
        break;
      case "grasp_abort":
        logLine(`grasp abort — ${ev.reason ?? "?"} (ee z ${num(ev.ee_z, 3)})`, true);
        break;
      case "close":
        closeJ6 = { open: ev.j6_open, closed: ev.j6 };
        logLine(`close j6 ${num(ev.j6_open, 3)} → ${num(ev.j6, 3)}`);
        break;
      case "twist":
        logLine(`twist j4 ${num(ev.j4, 2)}`);
        break;
      case "lift":
        if (Array.isArray(ev.ee)) armTrail.push({ x: ev.ee[0], z: ev.ee[2], kind: "lift" });
        logLine(`lift ee z ${num(ev.ee?.[2], 3)} j6 ${num(ev.j6, 3)}${ev.ik_fallback ? " (ik fallback)" : ""}`);
        break;
      case "verify":
        logLine(
          `verify ${ev.held ? "HELD" : "not held"} — floor ${ev.floor_clear ? "clear" : "object"}, j6 ${num(ev.j6, 3)}`,
          !ev.held,
        );
        break;
      default:
        logLine(String(ev.ev ?? "?"));
    }
    toggleBtn.classList.toggle("running", running);
    syncHeader();
    draw();
    placeReticle();
  }

  const unsubDebug = rosClient.subscribe(PICK_DEBUG_TOPIC, (msg) => {
    if (typeof msg?.data !== "string") return;
    try {
      onDebugEvent(JSON.parse(msg.data));
    } catch {
      // Not JSON — ignore.
    }
  });

  // ---- open / close ---------------------------------------------------------

  /** @param {boolean} open */
  function setOpen(open) {
    wrap.hidden = !open;
    toggleBtn.classList.toggle("active", open);
    toggleBtn.setAttribute("aria-pressed", String(open));
    if (open) draw();
  }

  toggleBtn.addEventListener("click", () => setOpen(Boolean(wrap.hidden)));

  /** @param {KeyboardEvent} e */
  function onKeyDown(e) {
    if (e.code !== TOGGLE_KEY || e.repeat || e.metaKey || e.ctrlKey || e.altKey) return;
    const el = document.activeElement;
    if (el instanceof HTMLElement && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable)) {
      return;
    }
    setOpen(Boolean(wrap.hidden));
  }
  window.addEventListener("keydown", onKeyDown);

  syncHeader();

  return {
    destroy() {
      if (publishTimer !== null) clearTimeout(publishTimer);
      unsubDebug();
      if (unsubSession) unsubSession();
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("resize", onResize);
      reticle.remove();
      seenMark.remove();
      boxGoal.remove();
      wristBox.remove();
      wristMark.remove();
      toggleBtn.remove();
      wrap.remove();
    },
  };
}
