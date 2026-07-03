// Leader-arm tick -> MARS joint radians, matching the real robot's
// conversion so this sandbox tracks the leader arm the same way the
// simulated follower does:
//   mars_app (ros2_ws/.../mars_control/app.cpp, leader_positions_callback)
//   converts raw ticks straight to radians -- no per-joint sign flip:
//     (tick - 2048) * (2*PI/4096)
//   That's also exactly what the Genesis sim (sim/src/simulation/
//   simulation_node.py, _apply_arm_positions) applies directly to
//   joint1-joint6, unflipped, which is why "it works well in genesis".
//   (arm_control.cpp *does* negate joints 2/3/4/6, both reading the
//   follower's own encoders into /joint_states and writing goal positions
//   back out -- but that's entirely about the real follower servos' own
//   mirrored mounting, not part of the joint's kinematic convention that
//   mars.urdf/Genesis use, and has nothing to do with the leader device.)
//
// joint1 is NOT negated (unlike an earlier attempt at this): the original
// "joint1 seems flipped" observation turned out to be an artifact of the
// multi-turn wrapping bug below (see wrapTick) -- while joint1's raw tick
// was stuck at huge unwrapped values, the tiny residual motion near the
// permanently-clamped limit looked inverted. With wrapping fixed, hands-on
// testing confirmed joint1 needs no sign flip at all, same as joints 2-6.
//
// LEADER_SERVO_IDS order (1-6) maps 1:1 to joint1-joint6; the leader arm has
// no 7th joint, so joint_head isn't driven by it.
//
// Joint ranges match mars.urdf's <limit> (the canonical/unflipped
// convention -- see arm.urdf's joint6 comment for why that's the negation
// of arm_config.yaml's raw servo limits), so a leader pose outside the
// follower's physical range clamps the same way the real robot's software
// limits would. joint6 additionally drops the real gripper's small
// "squeeze past closed" allowance (canonically down to -0.3491) since this
// sandbox only wants 0 (closed) and up (open).

import { LEADER_SERVO_IDS } from "./dynamixel";

const TICKS_PER_REV = 4096;
const TICK_CENTER = 2048;

export const LEADER_JOINT_NAMES = ["joint1", "joint2", "joint3", "joint4", "joint5", "joint6"] as const;

export const LEADER_JOINT_LIMITS: Record<(typeof LEADER_JOINT_NAMES)[number], [number, number]> = {
  joint1: [-1.5708, 1.5708],
  joint2: [-1.5708, 1.22],
  joint3: [-1.5708, 1.7453],
  joint4: [-1.9199, 1.7453],
  joint5: [-1.5708, 1.5708],
  joint6: [0, 0.8727],
};

const FLIPPED_INDICES = new Set<number>(); // none needed -- see header comment

// joint1's leader servo runs in Dynamixel Extended Position Control Mode
// (multi-turn) rather than the other 5 joints' single-turn mode -- confirmed
// by testing: its raw Present Position kept climbing across full-revolution
// boundaries instead of staying within one revolution, so (tick - 2048)
// produced huge radian values that permanently clamped to the joint limit
// (looked like "doesn't react" until you unwound several full turns). Wrap
// every joint's raw tick to a single revolution before converting -- a
// no-op for the single-turn joints, and collapses joint1's accumulated
// extra turns back to its true single-turn angle.
function wrapTick(tick: number): number {
  return ((tick % TICKS_PER_REV) + TICKS_PER_REV) % TICKS_PER_REV;
}

/** Converts one SyncRead round's raw ticks (ordered per LEADER_SERVO_IDS) to joint radians, clamped to LEADER_JOINT_LIMITS. */
export function leaderTicksToJointRadians(ticks: number[]): Record<string, number> {
  const radians: Record<string, number> = {};
  for (let i = 0; i < LEADER_SERVO_IDS.length; i++) {
    const name = LEADER_JOINT_NAMES[i];
    let rad = ((wrapTick(ticks[i]) - TICK_CENTER) * (2 * Math.PI)) / TICKS_PER_REV;
    if (FLIPPED_INDICES.has(i)) rad = -rad;
    const [lower, upper] = LEADER_JOINT_LIMITS[name];
    radians[name] = Math.max(lower, Math.min(upper, rad));
  }
  return radians;
}
