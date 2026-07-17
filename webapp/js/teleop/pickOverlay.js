// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Pick overlays — live aim feedback for the pick_any_object skill, drawn over
// the video stage while a run is active (run_start → run_end on
// /pick_any_object/debug). Nothing shows when the skill is idle.
//
// The skill reprojects its grasp target back onto the head frame and sends the
// pixel (grab_px) on the debug topic. We draw a reticle over the live video at
// that pixel — the actual spot on the floor the fingers will close on. The
// detection pixel from each localize event gets its own marker (green box
// corners) so you see where Gemini found the object vs where the arm will
// grab; it clears whenever the base starts moving, since a stored pixel no
// longer matches the shifted view, and reappears on the next localization.
// The head video is object-fit:contain (letterboxed, never cropped), so the
// 640×480 pixel maps to the screen with a single uniform scale + centering.
// When the ARM (wrist) camera is on the big stage instead, the same stage
// hosts the wrist-align overlays: the goal box in raw wrist-image pixels
// (wrist_box_u/v) and the skill's wrist detection marker.

import { PICK_DEBUG_TOPIC } from "../constants.js";

// Live copies of the skill's aim params, seeded with the TUNABLE defaults
// from pick_any_object.py. The skill broadcasts its full live dict on every
// run_start and params debug event — we resync from those so the boxes are
// drawn where the skill actually aims, not where the defaults say it would
// (live tuning overrides survive in the running skill between runs).
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

// Head-camera frame size the skill's grasp pixel is expressed in (IMG_W/IMG_H
// in pick_any_object.py). Only a fallback — the live <video>'s intrinsic size
// wins when known.
const IMG = { w: 640, h: 480 };

// Head-camera geometry, mirrored from pick_any_object.py (URDF values) so the
// pick box can be drawn without asking the skill.
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

/**
 * @param {HTMLElement} parent cockpit root — must contain the .video-stage.
 * @param {import("../rosClient.js").RosClient} rosClient
 * @param {import("../webrtcSession.js").WebRtcSession} [session]
 *   Video session — tells us which camera fills the big stage: head-camera
 *   overlays (reticle, pick box) only show on "main", wrist-align overlays
 *   only on "arm" (a floor pixel is meaningless on the wrist cam and vice
 *   versa).
 * @returns {{ destroy: () => void }}
 */
export function createPickOverlay(parent, rosClient, session) {
  const videoStage = /** @type {HTMLElement | null} */ (parent.querySelector(".video-stage"));

  // Reticle drawn over the live video at the skill's grasp pixel.
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

  // The pick box: the positioning goal square, shown only while a run is
  // active. Turns green while the skill reports the detection inside it.
  const boxGoal = document.createElement("div");
  boxGoal.className = "picktune-boxgoal";
  boxGoal.hidden = true;
  // Inner accept box (accept_frac of the outer): the point must land in THIS
  // to stop positioning. Greens when the skill reports inside; the outer stays
  // a dim guide.
  boxGoal.innerHTML =
    '<span class="picktune-boxgoal-tag mono">pick box</span>' +
    '<div class="picktune-boxaccept"></div>';
  const boxAccept = /** @type {HTMLElement} */ (boxGoal.querySelector(".picktune-boxaccept"));
  if (videoStage) videoStage.appendChild(boxGoal);

  // The wrist box: the wrist-align goal square, shown when the ARM camera is
  // on the big stage during a run. Same idea as the pick box, but in raw
  // wrist-image pixels (wrist_box_u/v) — no floor projection, the wrist cam
  // has none.
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

  /** Skill's grasp target in 640×480 image px, or null when none is active. @type {{ u: number, v: number } | null} */
  let grabPx = null;
  /** Latest detection pixel from localize; null after base motion shifts the view. @type {{ u: number, v: number } | null} */
  let seenPx = null;
  /** Latest wrist-camera detection pixel from wrist_align. @type {{ u: number, v: number } | null} */
  let wristPx = null;
  let running = false;

  // ---- overlays over the live video ----------------------------------------

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
   *  overlays when the arm cam is. The goal boxes only show while a run is
   *  active; a stale aim frame over an idle robot reads as noise. */
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
    // The wrist-align goal square.
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
    // The pick box goal square.
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
    // Inner accept box, centered, sized as a fraction of the outer.
    const acceptPct = `${P.accept_frac * 100}%`;
    boxAccept.style.width = acceptPct;
    boxAccept.style.height = acceptPct;
    boxGoal.hidden = false;
  }

  // Video letterbox geometry shifts on any stage resize; the overlays' validity
  // also flips when the primary camera or the stream changes (switch to wrist
  // cam, stream drop). Re-place on both so a mid-grasp camera switch is instant,
  // not delayed to the next debug event.
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
        // High-rate (~10 Hz) optical-flow tracking updates during the follow:
        // glide the marker to the tracked point and colour the box.
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
        // Pin the "detected" marker to the object point so the amber grasp
        // target and the green object marker sit side by side — their gap is
        // the reach clamp (grasp aims short when the object is out of reach).
        if (Array.isArray(ev.obj_px)) seenPx = { u: ev.obj_px[0], v: ev.obj_px[1] };
        break;
      case "wrist_seed":
        // A Gemini look on the wrist image (initial seed or re-seed on loss).
        wristPx = Array.isArray(ev.px) ? { u: ev.px[0], v: ev.px[1] } : null;
        break;
      case "wrist_servo":
        // Per-cycle tracking during the servo descent: glide the marker,
        // colour the box.
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
