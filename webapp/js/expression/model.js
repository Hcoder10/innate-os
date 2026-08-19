// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Expression model — the studio's representation of a pose, in two layers.
//
// Actuator layer: everything MARS can hold still — the 6 arm joints, the head
// pitch servo, and where the base sits relative to its neutral spot (the robot
// can drive anywhere, so base offset is a pose variable like any joint).
//
// Expression layer: a small basis of semantic axes. Each axis is a pair of
// actuator-space deltas (the −1 and +1 extremes) chosen from what the axis
// physically *means* — approach is literally distance to the person, expand is
// literally silhouette area — not from how an emotion "should look". Emotions
// are then just weight vectors over the basis (PRESETS), which is what makes
// the vocabulary searchable by sliders, optimizers, and VLM judges alike.
//
// synthesize() is the whole runtime: neutral + Σ weight·delta + manual
// offsets, clamped to actuator limits. Pure module — no DOM, no ROS.

/**
 * @typedef {"j1"|"j2"|"j3"|"j4"|"j5"|"j6"|"head"|"baseX"|"baseY"|"baseYaw"} ActuatorKey
 * @typedef {Record<ActuatorKey, number>} Pose
 * @typedef {Partial<Record<ActuatorKey, number>>} PoseDelta
 * @typedef {{ key: string, label: string, negLabel: string, posLabel: string,
 *             doc: string, neg: PoseDelta, pos: PoseDelta }} Axis
 * @typedef {Record<string, number>} Weights axis key -> weight in [-1, 1]
 * @typedef {{ key: string, label: string, doc: string, weights: Weights }} Preset
 */

// Arm ranges are the URDF joint limits (mars.urdf — what the arm can actually
// hold); head is the servo's real range in degrees (HEAD_MIN/MAX_DEG, wider
// than the URDF's stale ±20°); base offsets are studio bounds for "slightly"
// — the runtime controller will own real driving.
/** @type {{ key: ActuatorKey, label: string, min: number, max: number, unit: string, step: number }[]} */
export const ACTUATORS = [
  { key: "j1", label: "j1 · base yaw", min: -1.5708, max: 1.5708, unit: "rad", step: 0.01 },
  { key: "j2", label: "j2 · shoulder", min: -1.5708, max: 1.22, unit: "rad", step: 0.01 },
  { key: "j3", label: "j3 · elbow", min: -1.5708, max: 1.7453, unit: "rad", step: 0.01 },
  { key: "j4", label: "j4 · wrist pitch", min: -1.9199, max: 1.7453, unit: "rad", step: 0.01 },
  { key: "j5", label: "j5 · wrist roll", min: -1.5708, max: 1.5708, unit: "rad", step: 0.01 },
  { key: "j6", label: "j6 · gripper", min: 0, max: 0.8727, unit: "rad", step: 0.01 },
  { key: "head", label: "head pitch", min: -40, max: 70, unit: "°", step: 1 },
  { key: "baseX", label: "base advance", min: -0.3, max: 0.3, unit: "m", step: 0.005 },
  { key: "baseY", label: "base sidestep", min: -0.3, max: 0.3, unit: "m", step: 0.005 },
  { key: "baseYaw", label: "base turn", min: -0.7854, max: 0.7854, unit: "rad", step: 0.01 },
];

// At-ease: arm loosely folded near the SDK rest pose but off the joint limits
// (so every axis has room in both directions), head level on the person.
/** @type {Pose} */
export const NEUTRAL = {
  j1: 0.9,
  j2: -0.85,
  j3: 1.35,
  j4: -0.4,
  j5: 0,
  j6: 0.12,
  head: 8,
  baseX: 0,
  baseY: 0,
  baseYaw: 0,
};

// The person the robot is expressing *to* stands here (base frame, meters) —
// shared by the renderer's observer figure and the judge's viewpoints, so
// approach/turn/attend always read against the same target.
export const PERSON = { x: 1.0, y: 0, height: 1.65 };

// The basis. Deltas are what the +1 / −1 extreme adds to NEUTRAL; anything an
// axis doesn't mention stays untouched, so axes compose additively.
/** @type {Axis[]} */
export const AXES = [
  {
    key: "approach",
    label: "approach",
    negLabel: "withdraw",
    posLabel: "advance",
    doc: "Distance to the person — the most primitive engagement signal. Advancing commits the body toward the stimulus; withdrawing moves the vulnerable whole away from it.",
    neg: { baseX: -0.24 },
    pos: { baseX: 0.17 },
  },
  {
    key: "turn",
    label: "turn",
    negLabel: "away right",
    posLabel: "away left",
    doc: "Facing. 0 is square-on; either sign breaks the face-to-face axis. Body turned away with the head still on target reads very differently from full disengagement.",
    neg: { baseYaw: -0.7 },
    pos: { baseYaw: 0.7 },
  },
  {
    key: "sidestep",
    label: "sidestep",
    negLabel: "right",
    posLabel: "left",
    doc: "Lateral offset from the person's axis — circling, peeking, giving way.",
    neg: { baseY: -0.2 },
    pos: { baseY: 0.2 },
  },
  {
    key: "rise",
    label: "rise",
    negLabel: "shrink",
    posLabel: "tower",
    doc: "Silhouette height. The arm is most of what MARS can raise: mast-high arm plus lifted head maximizes apparent size; folding it low with the head dropped minimizes it.",
    neg: { j2: -0.5, j3: 0.35, j4: -0.8, head: -30 },
    pos: { j1: -0.7, j2: 0.75, j3: -2.75, j4: 0.3, head: 14 },
  },
  {
    key: "expand",
    label: "expand",
    negLabel: "clench",
    posLabel: "flare",
    doc: "Silhouette width. Flaring swings the arm out of the body outline and opens the gripper; clenching folds everything inside the chassis footprint and shuts the claw.",
    neg: { j1: 0.5, j3: 0.3, j4: -0.5, j6: -0.12 },
    pos: { j1: -1.5, j3: -0.85, j4: 0.4, j6: 0.6 },
  },
  {
    key: "reach",
    label: "reach",
    negLabel: "guard",
    posLabel: "offer",
    doc: "Arm extension along the robot→person line. Offering presents the effector to the person; guarding pulls it in against the chassis, protecting the protruding part.",
    neg: { j2: -0.4, j3: 0.35, j4: -0.9, j6: -0.12 },
    pos: { j1: -0.9, j2: 1.0, j3: -1.0, j4: -0.3, j6: 0.3 },
  },
  {
    key: "attend",
    label: "attend",
    negLabel: "downcast",
    posLabel: "raised",
    doc: "Gaze height. The person's face is ~1.5 m above the robot's, so sensors-on-target means head pitched well up; head at the floor abandons the target entirely.",
    neg: { head: -45 },
    pos: { head: 55 },
  },
];

// Emotions as weight vectors — derived from what the state *does* to a body,
// not from acting them out. The interesting ones are combinations single axes
// can't say: fear retreats and shrinks but keeps the sensors on the threat.
/** @type {Preset[]} */
export const PRESETS = [
  {
    key: "curious",
    label: "curious",
    doc: "Get the sensors closer to the thing without committing the body: advance a little, slight peek angle, head up and forward.",
    weights: { approach: 0.45, sidestep: 0.25, rise: 0.15, reach: 0.35, attend: 0.7 },
  },
  {
    key: "excited",
    label: "excited",
    doc: "Maximum energy: big, tall, open, pressing toward the person.",
    weights: { approach: 0.5, rise: 0.65, expand: 0.7, reach: 0.2, attend: 0.8 },
  },
  {
    key: "confident",
    label: "confident",
    doc: "Maximize apparent size and hold the ground: tall, open, square-on, no retreat.",
    weights: { approach: 0.2, rise: 0.9, expand: 0.45, attend: 0.55 },
  },
  {
    key: "affectionate",
    label: "affectionate",
    doc: "Close the distance and present the effector — contact-seeking, softly raised gaze.",
    weights: { approach: 0.65, rise: -0.1, expand: 0.15, reach: 0.8, attend: 0.5 },
  },
  {
    key: "fearful",
    label: "fearful",
    doc: "Retreat and shrink — but the head stays on the threat. Monitoring while withdrawing is what separates fear from mere disengagement.",
    weights: { approach: -0.9, turn: 0.25, rise: -0.7, expand: -0.6, reach: -0.7, attend: 0.35 },
  },
  {
    key: "sad",
    label: "sad",
    doc: "Low energy, no target focus: slumped small, gaze at the floor, slight drift away.",
    weights: { approach: -0.3, rise: -0.6, expand: -0.3, reach: -0.3, attend: -0.9 },
  },
  {
    key: "suspicious",
    label: "suspicious",
    doc: "Body turned off-axis and edged away while the head keeps watching — guarded observation.",
    weights: { approach: -0.25, turn: 0.6, sidestep: -0.3, expand: -0.2, attend: 0.4 },
  },
  {
    key: "relaxed",
    label: "relaxed",
    doc: "At ease near neutral: soft gaze on the person, nothing braced.",
    weights: { attend: 0.2 },
  },
];

/** Zero weight for every axis. @returns {Weights} */
export function zeroWeights() {
  return Object.fromEntries(AXES.map((a) => [a.key, 0]));
}

/** @param {number} v @param {number} lo @param {number} hi */
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

/** Clamp every actuator to its physical range. @param {Pose} pose @returns {Pose} */
export function clampPose(pose) {
  const out = /** @type {Pose} */ ({ ...pose });
  for (const a of ACTUATORS) out[a.key] = clamp(out[a.key], a.min, a.max);
  return out;
}

/**
 * Fold the expression layers into one actuator pose:
 * NEUTRAL + Σ axis contribution (weight toward the pos/neg extreme) + manual
 * per-actuator offsets, clamped to limits.
 * @param {Weights} weights @param {PoseDelta} [offsets] @returns {Pose}
 */
export function synthesize(weights, offsets = {}) {
  const q = /** @type {Pose} */ ({ ...NEUTRAL });
  for (const axis of AXES) {
    const w = clamp(weights[axis.key] ?? 0, -1, 1);
    if (w === 0) continue;
    const delta = w > 0 ? axis.pos : axis.neg;
    for (const [key, value] of Object.entries(delta)) {
      q[/** @type {ActuatorKey} */ (key)] += Math.abs(w) * /** @type {number} */ (value);
    }
  }
  for (const [key, value] of Object.entries(offsets)) {
    q[/** @type {ActuatorKey} */ (key)] += /** @type {number} */ (value) || 0;
  }
  return clampPose(q);
}

/** @typedef {{ id: string, name: string, weights: Weights, offsets: PoseDelta,
 *              notes: string, thumb: string | null,
 *              judge: { target: string, p: number, stamp: number } | null }} SavedPose */

/**
 * The library's export payload — weights AND the synthesized actuator vector
 * per pose, so downstream steps (PCA over collected poses, the runtime
 * controller) can consume either layer without importing this module.
 * @param {SavedPose[]} poses
 */
export function exportPayload(poses) {
  return {
    format: "innate-expression-poses",
    version: 1,
    robot: "mars",
    neutral: NEUTRAL,
    axes: AXES.map(({ key, neg, pos }) => ({ key, neg, pos })),
    poses: poses.map((p) => ({
      name: p.name,
      notes: p.notes,
      weights: p.weights,
      offsets: p.offsets,
      actuators: synthesize(p.weights, p.offsets),
      judge: p.judge,
    })),
  };
}

/**
 * Parse an exported payload back into library entries (thumbnails are not
 * exported; they re-render on load). Throws on anything that isn't ours.
 * @param {string} text @returns {{ name: string, weights: Weights, offsets: PoseDelta, notes: string }[]}
 */
export function parseImport(text) {
  const data = JSON.parse(text);
  if (data?.format !== "innate-expression-poses" || !Array.isArray(data.poses)) {
    throw new Error("not an expression-poses export");
  }
  return data.poses.map((/** @type {any} */ p) => ({
    name: String(p.name || "pose"),
    weights: sanitizeWeights(p.weights),
    offsets: sanitizeOffsets(p.offsets),
    notes: String(p.notes || ""),
  }));
}

/** Keep only known axis keys, as finite numbers in [-1, 1]. @param {any} raw @returns {Weights} */
export function sanitizeWeights(raw) {
  const w = zeroWeights();
  if (raw && typeof raw === "object") {
    for (const axis of AXES) {
      const v = Number(raw[axis.key]);
      if (Number.isFinite(v)) w[axis.key] = clamp(v, -1, 1);
    }
  }
  return w;
}

/** Keep only known actuator keys with finite values. @param {any} raw @returns {PoseDelta} */
export function sanitizeOffsets(raw) {
  /** @type {PoseDelta} */
  const out = {};
  if (raw && typeof raw === "object") {
    for (const a of ACTUATORS) {
      const v = Number(raw[a.key]);
      if (Number.isFinite(v) && v !== 0) out[a.key] = v;
    }
  }
  return out;
}
