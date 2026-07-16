// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Measured base velocity, derived from the /odom pose.
//
// Why derive it: the robot never fills Odometry.twist. mars_bringup's
// _publish_odometry copies pose out of the I2C transform and publishes,
// leaving twist at its zero default; mars_sim_driver does the same ("pose
// only, zero twist/covariance"). So msg.twist.twist.linear.x is a permanent
// 0.0 on both robot and sim, and no other topic carries measured motion —
// every /cmd_vel* is a command, not a measurement. The pose itself is the
// only honest source, differentiated.
//
// Differentiate the RAW odom pose, never the map-frame composite: the odom
// frame is continuous by construction, whereas a map-frame pose jumps every
// time AMCL corrects, which would read as an impossible velocity spike.

// A longer gap is a lull (or a reconnect), not motion — restart rather than
// divide a large displacement by a large dt and invent a spike.
const MAX_DT_S = 1.0;
// Duplicate/near-simultaneous samples would divide by ~0.
const MIN_DT_S = 0.02;
// EMA weight for each new sample — tames encoder quantization without adding
// visible lag at the ~10 Hz this is fed.
const SMOOTHING = 0.35;

/** @param {number} a radians → (-pi, pi] */
function wrapAngle(a) {
  return Math.atan2(Math.sin(a), Math.cos(a));
}

/**
 * @param {any} msg nav_msgs/Odometry
 * @returns {{ x: number, y: number, yaw: number } | null}
 */
export function odomPose(msg) {
  const p = msg?.pose?.pose;
  const x = p?.position?.x;
  const y = p?.position?.y;
  const q = p?.orientation;
  if (typeof x !== "number" || typeof y !== "number" || !q) return null;
  return { x, y, yaw: Math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z)) };
}

/**
 * Stateful estimator: feed it /odom messages, get smoothed {v, w} back.
 * Returns null while it has no usable interval yet (first sample, too-soon
 * sample, or after a gap) — callers should treat null as "unknown", not zero.
 * @returns {{ update: (msg: any) => { v: number, w: number } | null }}
 */
export function createVelocityTracker() {
  /** @type {{ t: number, x: number, y: number, yaw: number } | null} */
  let prev = null;
  let v = 0;
  let w = 0;
  let primed = false;

  return {
    update(msg) {
      const p = odomPose(msg);
      if (!p) return null;
      // Arrival time, matching the plots' x-axis: header stamps come from the
      // robot's clock, which isn't guaranteed to agree with the browser's.
      const t = performance.now();
      if (!prev) {
        prev = { t, ...p };
        return null;
      }
      const dt = (t - prev.t) / 1000;
      if (dt < MIN_DT_S) return primed ? { v, w } : null; // too soon; keep prev, wait for a real interval
      if (dt > MAX_DT_S) {
        prev = { t, ...p };
        primed = false;
        return null; // unknown across the gap, not zero
      }
      // Signed forward speed: project the displacement onto the heading we
      // held while covering it, so reversing reads negative.
      const dx = p.x - prev.x;
      const dy = p.y - prev.y;
      const rawV = (dx * Math.cos(prev.yaw) + dy * Math.sin(prev.yaw)) / dt;
      const rawW = wrapAngle(p.yaw - prev.yaw) / dt;
      prev = { t, ...p };
      if (primed) {
        v += SMOOTHING * (rawV - v);
        w += SMOOTHING * (rawW - w);
      } else {
        v = rawV;
        w = rawW;
        primed = true;
      }
      return { v, w };
    },
  };
}
