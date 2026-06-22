// @ts-check
// The catalog of operator-tunable knobs the Settings page exposes — the single
// place defaults + docs live for the UI. Each entry's `path` is the full ROS
// param path in settings.yaml (node, then ros__parameters, then nested groups,
// then the key). The proxy edits settings.yaml surgically by this path, so the
// file stays a hand-editable overlay (uncomment a stanza over SSH still works).
//
// Curated subset. The `default` values below were verified against the nodes'
// actual config (robot_config.yaml, motion_control.yaml, arm_config.yaml,
// velocity_smoother.yaml) — NOT just settings.yaml.template, whose documented
// values can drift. Add knobs here to expose more; nothing else changes.
// (Note: `inflation_layer` is intentionally omitted — its default differs per
//  costmap (0.25/0.3/0.35), so there's no honest single default to show.)

/**
 * @typedef {Object} Knob
 * @property {string[]} path  Full ROS param path in settings.yaml.
 * @property {string} label
 * @property {number|boolean} default
 * @property {"float"|"int"|"bool"} type
 * @property {string} [unit]
 * @property {string} doc
 */

/** @typedef {{ section: string, note?: string, knobs: Knob[] }} Group */

const P = "ros__parameters";

/** @type {Group[]} */
export const CATALOG = [
  {
    section: "Driving speed",
    note: "Manual (teleop) and autonomous (nav) are capped independently. Neither is the hard ceiling — the safety clamp below is.",
    knobs: [
      { path: ["/**", P, "motion_control", "max_speed"], label: "Manual max speed", default: 0.4, type: "float", unit: "m/s", doc: "Top translational speed for teleop / manual driving" },
      { path: ["/**", P, "motion_control", "max_angular_speed"], label: "Manual max turn", default: 1.0, type: "float", unit: "rad/s", doc: "Top rotational speed for teleop / manual driving" },
      { path: ["/**", P, "nav", "max_speed"], label: "Autonomous max speed", default: 0.45, type: "float", unit: "m/s", doc: "Top translational speed for autonomous nav2" },
      { path: ["/**", P, "nav", "max_angular_speed"], label: "Autonomous max turn", default: 0.6, type: "float", unit: "rad/s", doc: "Top rotational speed for autonomous nav2" },
    ],
  },
  {
    section: "Safety clamp (at the motors)",
    note: "The hardware ceiling every velocity source passes through. Keep these ≥ the driving caps above.",
    knobs: [
      { path: ["bringup", P, "safety", "max_speed"], label: "Hard max speed", default: 0.4, type: "float", unit: "m/s", doc: "Hard /cmd_vel linear ceiling at the motors" },
      { path: ["bringup", P, "safety", "max_angular_speed"], label: "Hard max turn", default: 2.5, type: "float", unit: "rad/s", doc: "Hard /cmd_vel angular ceiling at the motors" },
    ],
  },
  {
    section: "Battery",
    knobs: [
      { path: ["bringup", P, "battery", "warning_percentage"], label: "Low-battery warning", default: 20, type: "int", unit: "%", doc: "Low-battery warning level" },
      { path: ["bringup", P, "battery", "critical_percentage"], label: "Critical battery", default: 10, type: "int", unit: "%", doc: "Critical-battery level" },
    ],
  },
  {
    section: "Teleop",
    knobs: [
      { path: ["joystick_controller", P, "joystick", "slow_mode_factor"], label: "Slow-mode factor", default: 0.25, type: "float", doc: "Speed multiplier while the slow-mode button is held" },
    ],
  },
  {
    section: "Arm",
    knobs: [
      { path: ["mars_arm", P, "max_jerk"], label: "Max jerk", default: 150.0, type: "float", unit: "rad/s³", doc: "Trajectory jerk limit (0 disables)" },
      { path: ["mars_arm", P, "stress_enabled"], label: "Motor stress protection", default: false, type: "bool", doc: "Motor-protection cooldown" },
    ],
  },
];
