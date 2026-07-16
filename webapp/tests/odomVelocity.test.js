// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Tests for js/nav/odomVelocity.js — zero dependencies, plain node:
//   node tests/odomVelocity.test.js
// This module exists because the robot never populates Odometry.twist, so the
// Nav page differentiates the pose instead. That derivation has failure modes
// worth pinning: sign on reverse, correctness under a non-zero heading, the
// ±pi yaw wrap, and gaps that must read "unknown" rather than a huge spike.

import assert from "node:assert/strict";

// The module timestamps samples with performance.now(); drive it from a fake
// clock so the tests are deterministic rather than timing-dependent.
let clock = 0;
globalThis.performance = { now: () => clock };

const { createVelocityTracker, odomPose } = await import("../js/nav/odomVelocity.js");

let passed = 0;
/** @param {string} name @param {() => void} fn */
function test(name, fn) {
  fn();
  passed += 1;
  console.log(`ok - ${name}`);
}

/** A nav_msgs/Odometry as rws delivers it. @param {number} x @param {number} y @param {number} yaw */
function odom(x, y, yaw) {
  return {
    pose: {
      pose: {
        position: { x, y, z: 0 },
        orientation: { x: 0, y: 0, z: Math.sin(yaw / 2), w: Math.cos(yaw / 2) },
      },
    },
  };
}

/**
 * Feed a constant-velocity track and return the settled estimate.
 * @param {(i: number) => { x: number, y: number, yaw: number }} at
 */
function settle(at) {
  const tracker = createVelocityTracker();
  let out = null;
  for (let i = 0; i <= 40; i++) {
    clock = i * 100; // 10 Hz, matching the page's odom throttle
    const p = at(i);
    out = tracker.update(odom(p.x, p.y, p.yaw));
  }
  return out;
}

test("recovers a straight-line forward speed", () => {
  const v = settle((i) => ({ x: i * 0.05, y: 0, yaw: 0 })); // 0.05 m per 100 ms
  assert.ok(Math.abs(v.v - 0.5) < 1e-3, `v=${v.v}`);
  assert.ok(Math.abs(v.w) < 1e-6, `w=${v.w}`);
});

test("reverse reads negative, not |displacement|/dt", () => {
  const v = settle((i) => ({ x: -i * 0.03, y: 0, yaw: 0 }));
  assert.ok(Math.abs(v.v + 0.3) < 1e-3, `v=${v.v}`);
});

test("is frame-correct: motion along a +90deg heading is still forward", () => {
  const v = settle((i) => ({ x: 0, y: i * 0.05, yaw: Math.PI / 2 }));
  assert.ok(Math.abs(v.v - 0.5) < 1e-3, `v=${v.v}`);
});

test("recovers a spin in place without inventing linear speed", () => {
  const v = settle((i) => ({ x: 0, y: 0, yaw: i * 0.06 }));
  assert.ok(Math.abs(v.w - 0.6) < 1e-3, `w=${v.w}`);
  assert.ok(Math.abs(v.v) < 1e-6, `v=${v.v}`);
});

test("yaw wrap across +/-pi does not spike", () => {
  const tracker = createVelocityTracker();
  clock = 0;
  tracker.update(odom(0, 0, Math.PI - 0.03));
  clock = 100;
  const v = tracker.update(odom(0, 0, -Math.PI + 0.03)); // +0.06 rad across the seam
  // Naive (yaw - prev.yaw)/dt would read about -62 rad/s here.
  assert.ok(Math.abs(v.w - 0.6) < 1e-3, `w=${v.w}`);
});

test("a gap reads unknown (null), never a fabricated spike", () => {
  const tracker = createVelocityTracker();
  clock = 0;
  tracker.update(odom(0, 0, 0));
  clock = 5000; // 5 s later, 9 m away — a reconnect, not 1.8 m/s
  assert.equal(tracker.update(odom(9, 0, 0)), null);
});

test("the first sample has no interval, so it reports unknown not zero", () => {
  const tracker = createVelocityTracker();
  clock = 0;
  assert.equal(tracker.update(odom(0, 0, 0)), null);
});

test("a too-soon duplicate does not divide by ~0", () => {
  const tracker = createVelocityTracker();
  clock = 0;
  tracker.update(odom(0, 0, 0));
  clock = 100;
  tracker.update(odom(0.05, 0, 0)); // primes at 0.5 m/s
  clock = 101; // 1 ms later, same pose: must not read 0, nor explode
  const v = tracker.update(odom(0.05, 0, 0));
  assert.ok(Number.isFinite(v.v) && Math.abs(v.v - 0.5) < 1e-9, `v=${v.v}`);
});

test("odomPose rejects a message with no orientation", () => {
  assert.equal(odomPose({ pose: { pose: { position: { x: 1, y: 2 } } } }), null);
  assert.equal(odomPose(null), null);
});

console.log(`\n${passed} passed`);
