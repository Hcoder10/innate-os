// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Pick overlays — aim feedback for the pick_any_object skill, drawn over the
// video stage between run_start and run_end on /pick_any_object/debug.
// Head-camera overlays (grasp reticle, detection marker, pick box) only make
// sense while the "main" stream is on the stage; the wrist-align overlays only
// while "arm" is.

import { PICK_DEBUG_TOPIC } from "../constants.js";

// Live copies of the skill's aim params, seeded with the TUNABLE defaults from
// pick_any_object.py and resynced from every run_start/params debug event —
// live tuning overrides survive in the running skill between runs, so the
// defaults alone would draw the boxes where the skill no longer aims.
// Display-only: never written back.
const P = {
  sweet_x: 0.3,
  box_y: 0,
  box_half_px: 40,
  accept_frac: 0.5,
  tilt_deg: -20,
  wrist_box_u: 320,
  wrist_box_v: 240,
  wrist_half_px: 60,
};

/** Fold a params payload from the debug topic into P (unknown keys ignored).
 *  @param {any} params */
function syncParams(params) {
  if (!params || typeof params !== "object") return;
  for (const k of Object.keys(P)) {
    if (typeof params[k] === "number" && Number.isFinite(params[k])) P[k] = params[k];
  }
}

// Fallback only — the live <video>'s intrinsic size wins when known.
const IMG = { w: 640, h: 480 };

// Head-camera geometry, mirrored from workspace/skill_lib/geometry.py (URDF
// values) so the pick box can be drawn without asking the skill.
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
 *  Mirror of floor_to_pixel in workspace/skill_lib/geometry.py.
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

/**
 * @param {HTMLElement} parent cockpit root — must contain the .video-stage.
 * @param {import("../rosClient.js").RosClient} rosClient
 * @param {import("../webrtcSession.js").WebRtcSession} [session]
 *   Tells us which camera fills the big stage; overlays hide on the other one.
 * @returns {{ destroy: () => void }}
 */
export function createPickOverlay(parent, rosClient, session) {
  const videoStage = /** @type {HTMLElement | null} */ (parent.querySelector(".video-stage"));

  // Grasp target: the spot on the floor the fingers will close on.
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

  // Where Gemini reported the object, so "seen here" and "grabbing here" read
  // apart at a glance.
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

  // Positioning goal. The detection must land in the inner accept box (not
  // just the outer guide) for the skill to stop driving.
  const boxGoal = document.createElement("div");
  boxGoal.className = "picktune-boxgoal";
  boxGoal.hidden = true;
  boxGoal.innerHTML =
    '<span class="picktune-boxgoal-tag mono">pick box</span>' +
    '<div class="picktune-boxaccept"></div>';
  const boxAccept = /** @type {HTMLElement} */ (boxGoal.querySelector(".picktune-boxaccept"));
  if (videoStage) videoStage.appendChild(boxGoal);

  // Wrist-align goal, in raw wrist-image pixels — no floor projection, the
  // wrist cam has none.
  const wristBox = document.createElement("div");
  wristBox.className = "picktune-boxgoal";
  wristBox.hidden = true;
  wristBox.innerHTML = '<span class="picktune-boxgoal-tag mono">wrist box</span>';
  if (videoStage) videoStage.appendChild(wristBox);

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

  /** @type {{ u: number, v: number } | null} */
  let grabPx = null;
  /** Detection pixel from localize; null after base motion shifts the view.
   *  @type {{ u: number, v: number } | null} */
  let seenPx = null;
  /** @type {{ u: number, v: number } | null} */
  let wristPx = null;
  let running = false;

  // ---- overlays over the live video ----------------------------------------

  /** Defaults to true when no session was passed. */
  function headCamPrimary() {
    return !session || session.primaryCamera?.name === "main";
  }

  function wristCamPrimary() {
    return session?.primaryCamera?.name === "arm";
  }

  /** Letterbox geometry of the live video inside the stage (object-fit:
   *  contain → one uniform scale + symmetric offsets), or null when no video
   *  is up. Camera-agnostic; callers gate on which camera is primary.
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

  /** Position (or hide) every marker and goal box. The goal boxes only show
   *  while a run is active; a stale aim frame over an idle robot reads as
   *  noise. */
  function placeOverlays() {
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
    if (running && wristG) {
      const side = 2 * P.wrist_half_px * wristG.s;
      wristBox.style.left = `${wristG.offX + P.wrist_box_u * wristG.s}px`;
      wristBox.style.top = `${wristG.offY + P.wrist_box_v * wristG.s}px`;
      wristBox.style.width = `${side}px`;
      wristBox.style.height = `${side}px`;
      wristBox.hidden = false;
    } else {
      wristBox.hidden = true;
    }
    const center = running && headG ? floorToPixel(P.sweet_x, P.box_y, P.tilt_deg) : null;
    if (!headG || !center) {
      boxGoal.hidden = true;
      return;
    }
    const side = 2 * P.box_half_px * headG.s;
    boxGoal.style.left = `${headG.offX + center.u * headG.s}px`;
    boxGoal.style.top = `${headG.offY + center.v * headG.s}px`;
    boxGoal.style.width = `${side}px`;
    boxGoal.style.height = `${side}px`;
    const acceptPct = `${P.accept_frac * 100}%`;
    boxAccept.style.width = acceptPct;
    boxAccept.style.height = acceptPct;
    boxGoal.hidden = false;
  }

  // Re-place on resize and on camera/stream changes, so a mid-grasp camera
  // switch is instant rather than delayed to the next debug event.
  const onResize = () => placeOverlays();
  window.addEventListener("resize", onResize);
  const unsubSession = session ? session.onChange(() => placeOverlays()) : null;

  // ---- debug feed ---------------------------------------------------------

  /** @param {any} ev */
  function onDebugEvent(ev) {
    switch (ev.ev) {
      case "run_start":
        syncParams(ev.params); // run_start carries the skill's full live dict
        grabPx = null;
        seenPx = null;
        wristPx = null;
        boxGoal.classList.remove("inside");
        wristBox.classList.remove("inside");
        running = true;
        break;
      case "params":
        // Tuning ack (mid-run retunes included) — move the boxes live.
        syncParams(ev.params);
        break;
      case "run_end":
        running = false;
        grabPx = null;
        seenPx = null;
        wristPx = null;
        boxGoal.classList.remove("inside");
        wristBox.classList.remove("inside");
        break;
      case "localize":
        seenPx = Array.isArray(ev.px) ? { u: ev.px[0], v: ev.px[1] } : null;
        break;
      case "servo":
        if (Array.isArray(ev.px)) seenPx = { u: ev.px[0], v: ev.px[1] };
        boxGoal.classList.toggle("inside", ev.inside === true);
        break;
      case "position":
        boxGoal.classList.toggle("inside", ev.inside === true);
        break;
      case "position_done":
        if (!Array.isArray(ev.xy)) boxGoal.classList.remove("inside");
        break;
      case "rotate":
      case "drive":
        seenPx = null; // view is about to shift — the stored pixel goes stale
        break;
      case "grasp":
        grabPx = Array.isArray(ev.grab_px) ? { u: ev.grab_px[0], v: ev.grab_px[1] } : null;
        // The gap between the two markers is the reach clamp — grasp aims
        // short when the object is out of reach.
        if (Array.isArray(ev.obj_px)) seenPx = { u: ev.obj_px[0], v: ev.obj_px[1] };
        break;
      case "wrist_seed":
        wristPx = Array.isArray(ev.px) ? { u: ev.px[0], v: ev.px[1] } : null;
        break;
      case "wrist_servo":
        if (Array.isArray(ev.px)) wristPx = { u: ev.px[0], v: ev.px[1] };
        wristBox.classList.toggle("inside", ev.inside === true);
        break;
      case "wrist_done":
        // Refined grasp target — re-pin the head-view reticle to match.
        grabPx = Array.isArray(ev.grab_px) ? { u: ev.grab_px[0], v: ev.grab_px[1] } : grabPx;
        break;
      default:
        break;
    }
    placeOverlays();
  }

  const unsubDebug = rosClient.subscribe(PICK_DEBUG_TOPIC, (msg) => {
    if (typeof msg?.data !== "string") return;
    try {
      onDebugEvent(JSON.parse(msg.data));
    } catch {
      // Not JSON — ignore.
    }
  });

  return {
    destroy() {
      unsubDebug();
      if (unsubSession) unsubSession();
      window.removeEventListener("resize", onResize);
      reticle.remove();
      seenMark.remove();
      boxGoal.remove();
      wristBox.remove();
      wristMark.remove();
    },
  };
}
