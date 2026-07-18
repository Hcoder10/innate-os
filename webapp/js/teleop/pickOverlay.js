// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Pick overlays — aim feedback for the pick_any_object skill, drawn over the
// video stage between run_start and run_end on /pick_any_object/debug.
// Head-camera overlays (grasp reticle, detection marker, pick box) only make
// sense while the "main" stream is on the stage; the wrist-align overlays only
// while "arm" is. The wrist goal box is draggable — dropping it publishes
// wrist_box_u/v on the tuning topic, so aiming the servo is literally dragging
// the target on the video.

import { PICK_DEBUG_TOPIC, PICK_TUNING_TOPIC } from "../constants.js";

// Live copies of the skill's wrist-aim params, seeded with the TUNABLE
// defaults from pick_any_object.py and resynced from every run_start/params
// debug event — live tuning overrides survive in the running skill between
// runs, so the defaults alone would draw the box where the skill no longer
// aims. The head-camera pick box needs no seed and no mirrored camera model:
// the skill projects it and sends it ready to draw ([cu, cv, half, accept]
// image px, the `box` event field).
const P = {
  wrist_box_u: 320,
  wrist_box_v: 240,
  wrist_half_px: 60,
};

// Fallback only — the live <video>'s intrinsic size wins when known.
const IMG = { w: 640, h: 480 };

// Publishing every pointermove floods the tuning topic (and the skill logs
// each applied dict); ~8 Hz while dragging is plenty, plus a final on release.
const DRAG_PUBLISH_MS = 125;

const SVG_OPEN =
  '<svg viewBox="0 0 48 48" width="48" height="48" fill="none" stroke="currentColor" ' +
  'stroke-width="1.6" aria-hidden="true">';
// Crosshair reticle: the "grabbing HERE" shape.
const RETICLE_SVG =
  '<circle cx="24" cy="24" r="13"/>' +
  '<line x1="24" y1="3" x2="24" y2="14"/><line x1="24" y1="34" x2="24" y2="45"/>' +
  '<line x1="3" y1="24" x2="14" y2="24"/><line x1="34" y1="24" x2="45" y2="24"/>' +
  '<circle cx="24" cy="24" r="2.2" fill="currentColor" stroke="none"/>';
// Box corners: the "found it HERE" detection shape.
const CORNERS_SVG =
  '<path d="M8 16 V8 H16"/><path d="M32 8 H40 V16"/>' +
  '<path d="M40 32 V40 H32"/><path d="M16 40 H8 V32"/>';

/** Fold a params payload from the debug topic into P (unknown keys ignored).
 *  @param {any} params */
function syncParams(params) {
  if (!params || typeof params !== "object") return;
  for (const k of Object.keys(P)) {
    if (typeof params[k] === "number" && Number.isFinite(params[k])) P[k] = params[k];
  }
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

  /** Marker pinned to an image pixel: an icon + a small tag label.
   *  @param {string} className @param {string} svgBody @param {string} label */
  function makeMarker(className, svgBody, label) {
    const el = document.createElement("div");
    el.className = className;
    el.hidden = true;
    el.innerHTML = `${SVG_OPEN}${svgBody}</svg><span class="picktune-grab-tag mono">${label}</span>`;
    if (videoStage) videoStage.appendChild(el);
    return el;
  }

  // The reticle marks where the fingers will close; the corner markers where
  // Gemini saw the object — "seen here" and "grabbing here" read apart.
  const reticle = makeMarker("picktune-grab", RETICLE_SVG, "grasp target");
  const seenMark = makeMarker("picktune-grab picktune-seen", CORNERS_SVG, "detected");
  const wristMark = makeMarker("picktune-grab picktune-seen", CORNERS_SVG, "wrist detect");

  // The pick box: the positioning goal square. Greens while the skill reports
  // the detection inside it; the inner accept box is what actually stops the base.
  const boxGoal = document.createElement("div");
  boxGoal.className = "picktune-boxgoal";
  boxGoal.hidden = true;
  boxGoal.innerHTML =
    '<span class="picktune-boxgoal-tag mono">pick box</span>' +
    '<div class="picktune-boxaccept"></div>';
  const boxAccept = /** @type {HTMLElement} */ (boxGoal.querySelector(".picktune-boxaccept"));
  if (videoStage) videoStage.appendChild(boxGoal);

  // The wrist box: the wrist-align goal square in raw wrist-image pixels.
  // Draggable — releasing it publishes the new center as a tuning override,
  // which the skill echoes back as a params event.
  const wristBox = document.createElement("div");
  wristBox.className = "picktune-boxgoal picktune-wristbox";
  wristBox.hidden = true;
  wristBox.innerHTML = '<span class="picktune-boxgoal-tag mono">wrist box</span>';
  if (videoStage) videoStage.appendChild(wristBox);

  // ---- state ---------------------------------------------------------------

  /** Skill's grasp target in 640×480 image px, or null when none is active. @type {{ u: number, v: number } | null} */
  let grabPx = null;
  /** Latest detection pixel from localize; null after base motion shifts the view. @type {{ u: number, v: number } | null} */
  let seenPx = null;
  /** Latest wrist-camera detection pixel from wrist_align. @type {{ u: number, v: number } | null} */
  let wristPx = null;
  /** Pick box from the skill, [cu, cv, half, accept] head-image px. @type {number[] | null} */
  let headBox = null;
  let running = false;

  /** Absorb aim info from any box-bearing debug event: the params dict for
   *  the wrist keys, the skill-projected pick box as is. @param {any} ev */
  function readAim(ev) {
    syncParams(ev.params);
    if (Array.isArray(ev.box) && ev.box.length >= 4) headBox = ev.box;
  }

  // ---- overlays over the live video ----------------------------------------

  /** True when the head camera fills the big stage (a floor pixel only maps
   *  onto that view). Defaults to true when no session was passed. */
  function headCamPrimary() {
    return !session || session.primaryCamera?.name === "main";
  }

  /** True when the wrist ("arm") camera fills the big stage. */
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
   *  video. The goal boxes only show while a run is active; a stale aim frame
   *  over an idle robot reads as noise. */
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
    // The pick box goal square — drawn exactly where the skill said it is.
    if (!running || !headG || !headBox) {
      boxGoal.hidden = true;
      return;
    }
    const [cu, cv, half, accept] = headBox;
    const side = 2 * half * headG.s;
    boxGoal.style.left = `${headG.offX + cu * headG.s}px`;
    boxGoal.style.top = `${headG.offY + cv * headG.s}px`;
    boxGoal.style.width = `${side}px`;
    boxGoal.style.height = `${side}px`;
    const acceptPct = `${(accept / half) * 100}%`;
    boxAccept.style.width = acceptPct;
    boxAccept.style.height = acceptPct;
    boxGoal.hidden = false;
  }

  // ---- wrist box dragging ---------------------------------------------------

  /** Pointer-grab offset from the box center (image px), or null when idle.
   *  @type {{ du: number, dv: number } | null} */
  let drag = null;
  let lastDragPublish = 0;

  function publishWristCenter() {
    rosClient.publish(PICK_TUNING_TOPIC, {
      data: JSON.stringify({
        wrist_box_u: Math.round(P.wrist_box_u),
        wrist_box_v: Math.round(P.wrist_box_v),
      }),
    });
  }

  /** Pointer position -> image px, or null without stage geometry.
   *  @param {PointerEvent} e */
  function pointerToImagePx(e) {
    const g = stageGeom();
    if (!g || !videoStage) return null;
    const rect = videoStage.getBoundingClientRect();
    return {
      u: (e.clientX - rect.left - g.offX) / g.s,
      v: (e.clientY - rect.top - g.offY) / g.s,
    };
  }

  wristBox.addEventListener("pointerdown", (e) => {
    const px = pointerToImagePx(e);
    if (!px) return;
    e.preventDefault();
    wristBox.setPointerCapture(e.pointerId);
    drag = { du: px.u - P.wrist_box_u, dv: px.v - P.wrist_box_v };
    wristBox.classList.add("dragging");
  });

  wristBox.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const px = pointerToImagePx(e);
    if (!px) return;
    P.wrist_box_u = Math.max(0, Math.min(IMG.w, px.u - drag.du));
    P.wrist_box_v = Math.max(0, Math.min(IMG.h, px.v - drag.dv));
    placeOverlays();
    const now = performance.now();
    if (now - lastDragPublish >= DRAG_PUBLISH_MS) {
      lastDragPublish = now;
      publishWristCenter();
    }
  });

  /** @param {PointerEvent} e */
  function endDrag(e) {
    if (!drag) return;
    drag = null;
    wristBox.classList.remove("dragging");
    wristBox.releasePointerCapture(e.pointerId);
    publishWristCenter(); // final position always lands, throttle or not
  }
  wristBox.addEventListener("pointerup", endDrag);
  wristBox.addEventListener("pointercancel", endDrag);

  // Letterbox geometry shifts on stage resize; overlay validity flips on
  // camera/stream changes. Re-place on both so a mid-grasp camera switch is
  // instant, not delayed to the next debug event.
  const onResize = () => placeOverlays();
  window.addEventListener("resize", onResize);
  const unsubSession = session ? session.onChange(() => placeOverlays()) : null;

  // ---- debug feed ---------------------------------------------------------

  /** @param {any} ev */
  function onDebugEvent(ev) {
    switch (ev.ev) {
      case "run_start":
      case "run_end":
        running = ev.ev === "run_start";
        grabPx = null;
        seenPx = null;
        wristPx = null;
        boxGoal.classList.remove("inside");
        wristBox.classList.remove("inside");
        readAim(ev); // run_start carries the skill's full live dict + box
        break;
      case "params":
        // Tuning ack — move the boxes live. Skip while dragging: the echo of
        // an in-flight drag would yank the box backwards under the pointer.
        if (!drag) readAim(ev);
        break;
      case "localize":
        seenPx = Array.isArray(ev.px) ? { u: ev.px[0], v: ev.px[1] } : null;
        break;
      case "servo":
        // ~10 Hz optical-flow tracking during the follow: glide the marker.
        if (Array.isArray(ev.px)) seenPx = { u: ev.px[0], v: ev.px[1] };
        boxGoal.classList.toggle("inside", ev.inside === true);
        readAim(ev);
        break;
      case "position":
        boxGoal.classList.toggle("inside", ev.inside === true);
        readAim(ev);
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
        // Pin the detection marker to the object point: its gap to the grasp
        // target is the reach clamp (grasp aims short when out of reach).
        if (Array.isArray(ev.obj_px)) seenPx = { u: ev.obj_px[0], v: ev.obj_px[1] };
        break;
      case "wrist_seed":
        // A Gemini look on the wrist image (initial seed or re-seed on loss).
        wristPx = Array.isArray(ev.px) ? { u: ev.px[0], v: ev.px[1] } : null;
        break;
      case "wrist_descend":
        // Per-cycle tracking during the wrist descent.
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
