// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// ChaseCameraControl — drag the sim's chase camera in an orbit around the robot.
//
// The chase view is rendered server-side (Genesis) and streamed as video, so
// "orbit" is a remote-rendering loop: we capture pointer drags on the video and
// publish an absolute orbit pose to /sim/chase_camera/control; the sim
// repositions its camera and the change comes back through the video stream.
//
// Wire contract (see the Python bridge): the topic is latched. The sim publishes
// the default once at startup (retained via DDS transient_local); we advertise
// with latch:true, subscribe transient_local to seed our state from that retained
// value ONCE, then become the authoritative publisher. We ignore inbound after
// seeding so the sim's echo can't snap the camera back mid-drag. No heartbeat —
// we publish only on change.

import { CHASE_CAMERA_CONTROL_TOPIC } from "../constants.js";

// Matches DEFAULT_CHASE_ORBIT in sim/src/agent/types.py (1m behind + above robot).
const DEFAULT_ORBIT = { azimuth: 0, elevation: Math.PI / 4, radius: Math.SQRT2, pan_x: 0, pan_y: 0 };

const ELEVATION_LIMIT = 1.4; // radians, keep off the poles to avoid gimbal flip
const RADIUS_MIN = 0.5;
const RADIUS_MAX = 15;
const ORBIT_RAD_PER_PX = 0.01; // drag sensitivity for azimuth/elevation
const PAN_M_PER_PX = 0.004; // shift/middle-drag target pan, scaled by radius
const ZOOM_PER_WHEEL = 0.001; // wheel zoom exponent
const PUBLISH_MIN_GAP_MS = 16; // ~60 Hz, leading-edge + trailing flush

/** @param {number} v @param {number} lo @param {number} hi */
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
/** @param {number} v */
const round4 = (v) => Math.round(v * 10000) / 10000;

/**
 * @param {HTMLElement} root Cockpit root; the `.video-stage` element lives here.
 * @param {import("../webrtcSession.js").WebRtcSession} session
 * @param {import("../rosClient.js").RosClient} ros
 * @returns {{ destroy: () => void }}
 */
export function createChaseCameraControl(root, session, ros) {
  const stage = /** @type {HTMLElement | null} */ (root.querySelector(".video-stage"));
  if (!stage) return { destroy() {} };

  const orbit = { ...DEFAULT_ORBIT };
  let seeded = false;
  let lastPublishAt = 0;
  /** @type {ReturnType<typeof setTimeout> | null} */ let flushTimer = null;
  /** @type {{ x: number, y: number, pan: boolean } | null} */ let drag = null;

  const isChaseActive = () => session.primaryCamera.name === "chase";

  // ---- publishing --------------------------------------------------------
  function publishNow() {
    if (ros.state !== "connected") return;
    lastPublishAt = performance.now();
    ros.publish(CHASE_CAMERA_CONTROL_TOPIC, {
      data: JSON.stringify({
        azimuth: round4(orbit.azimuth),
        elevation: round4(orbit.elevation),
        radius: round4(orbit.radius),
        pan_x: round4(orbit.pan_x),
        pan_y: round4(orbit.pan_y),
      }),
    });
  }

  // Leading-edge publish, capped at one per PUBLISH_MIN_GAP_MS; a trailing timer
  // flushes the final resting value so it never waits on the next interaction.
  function publishChange() {
    const elapsed = performance.now() - lastPublishAt;
    if (elapsed >= PUBLISH_MIN_GAP_MS) {
      if (flushTimer !== null) {
        clearTimeout(flushTimer);
        flushTimer = null;
      }
      publishNow();
    } else if (flushTimer === null) {
      flushTimer = setTimeout(() => {
        flushTimer = null;
        publishNow();
      }, PUBLISH_MIN_GAP_MS - elapsed);
    }
  }

  // ---- seeding from the latched topic ------------------------------------
  const unsubscribe = ros.subscribe(
    CHASE_CAMERA_CONTROL_TOPIC,
    (msg) => {
      if (seeded) return; // authoritative once connected; ignore our own echo
      try {
        const p = JSON.parse(msg?.data ?? "");
        if (p && typeof p === "object") {
          orbit.azimuth = Number(p.azimuth) || 0;
          orbit.elevation = clamp(Number(p.elevation) || 0, -ELEVATION_LIMIT, ELEVATION_LIMIT);
          orbit.radius = clamp(Number(p.radius) || DEFAULT_ORBIT.radius, RADIUS_MIN, RADIUS_MAX);
          orbit.pan_x = Number(p.pan_x) || 0;
          orbit.pan_y = Number(p.pan_y) || 0;
          seeded = true;
        }
      } catch {
        // Malformed retained value — keep the local default, seed on next input.
      }
    },
    { durability: "transient_local", queueSize: 1 },
  );

  // Advertise latched so the sim's transient_local subscriber accepts our
  // publishes. onStateChange fires immediately with the current state and again
  // on every transition, so this also re-advertises after each reconnect.
  const unsubState = ros.onStateChange((state) => {
    if (state === "connected") {
      ros.advertise(CHASE_CAMERA_CONTROL_TOPIC, "std_msgs/msg/String", { latch: true });
    }
  });

  // ---- pointer / wheel interaction ---------------------------------------
  /** @param {PointerEvent} e */
  function onPointerDown(e) {
    if (!isChaseActive() || e.button > 1) return;
    drag = { x: e.clientX, y: e.clientY, pan: e.button === 1 || e.shiftKey };
    seeded = true; // any user input makes us authoritative
    stage.setPointerCapture(e.pointerId);
    stage.classList.add("orbiting");
    e.preventDefault();
  }

  /** @param {PointerEvent} e */
  function onPointerMove(e) {
    if (!drag) return;
    const dx = e.clientX - drag.x;
    const dy = e.clientY - drag.y;
    drag.x = e.clientX;
    drag.y = e.clientY;
    if (drag.pan) {
      // Screen-right pans the target +y (robot-left), screen-up pans +x (ahead).
      const k = PAN_M_PER_PX * orbit.radius;
      orbit.pan_y += dx * k;
      orbit.pan_x += dy * k;
    } else {
      orbit.azimuth -= dx * ORBIT_RAD_PER_PX;
      orbit.elevation = clamp(orbit.elevation + dy * ORBIT_RAD_PER_PX, -ELEVATION_LIMIT, ELEVATION_LIMIT);
    }
    publishChange();
  }

  /** @param {PointerEvent} e */
  function onPointerUp(e) {
    if (!drag) return;
    drag = null;
    stage.classList.remove("orbiting");
    if (stage.hasPointerCapture(e.pointerId)) stage.releasePointerCapture(e.pointerId);
    publishNow(); // flush the resting pose
  }

  /** @param {WheelEvent} e */
  function onWheel(e) {
    if (!isChaseActive()) return;
    seeded = true;
    orbit.radius = clamp(orbit.radius * Math.exp(e.deltaY * ZOOM_PER_WHEEL), RADIUS_MIN, RADIUS_MAX);
    publishChange();
    e.preventDefault();
  }

  /** @param {MouseEvent} e */
  function onDblClick(e) {
    if (!isChaseActive()) return;
    orbit.pan_x = 0;
    orbit.pan_y = 0;
    seeded = true;
    publishNow();
    e.preventDefault();
  }

  stage.addEventListener("pointerdown", onPointerDown);
  stage.addEventListener("pointermove", onPointerMove);
  stage.addEventListener("pointerup", onPointerUp);
  stage.addEventListener("pointercancel", onPointerUp);
  stage.addEventListener("wheel", onWheel, { passive: false });
  stage.addEventListener("dblclick", onDblClick);

  return {
    destroy() {
      stage.removeEventListener("pointerdown", onPointerDown);
      stage.removeEventListener("pointermove", onPointerMove);
      stage.removeEventListener("pointerup", onPointerUp);
      stage.removeEventListener("pointercancel", onPointerUp);
      stage.removeEventListener("wheel", onWheel);
      stage.removeEventListener("dblclick", onDblClick);
      if (flushTimer !== null) clearTimeout(flushTimer);
      unsubscribe();
      unsubState();
    },
  };
}
