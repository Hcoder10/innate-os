// @ts-check
// SPDX-License-Identifier: Apache-2.0
// The catalog of operator-tunable knobs the Settings page exposes — the single
// place defaults + docs live for the UI. Each entry's `path` is the full ROS
// param path in settings.yaml (node, then ros__parameters, then nested groups,
// then the key). The proxy edits settings.yaml surgically by this path, so the
// file stays a hand-editable overlay (uncomment a stanza over SSH still works).
//
// Curated subset. The `default` values below are the values the robot actually
// LAUNCHES with — verified against each node's loaded config, NOT just its own
// declare_parameter() fallback (which the launch layer overrides). Authoritative
// sources: the launch-file parameter blocks (brain_client.launch.py,
// input_manager.launch.py, navigation.launch.py, main_camera_driver.launch.py)
// and the config YAMLs they load (manipulation_server.yaml, innate_uninavid
// params.yaml), with robot_config.yaml / motion_control.yaml / arm_config.yaml /
// velocity_smoother.yaml for the original knobs. These can diverge from a node's
// code default AND from settings.yaml.template's comments (e.g. the launch sets
// vertical_fov=80, send_depth=false, log_everything=true — overriding config.py;
// both nodes' TTS voice default to the same env-backed id; temporal_ensemble_coeff
// is 0.0 and consecutive_stops_to_complete is 30 per the loaded YAMLs). Add knobs
// here to expose more; nothing else changes.
// (Note: `inflation_layer` is intentionally omitted — its default differs per
//  costmap (0.25/0.3/0.35), so there's no honest single default to show.)

/**
 * @typedef {Object} Knob
 * @property {string[]} path  Full ROS param path in settings.yaml.
 * @property {string} label
 * @property {number|boolean|string|string[]} default
 * @property {"float"|"int"|"bool"|"string"|"list"} type
 * @property {string} [unit]
 * @property {string} doc
 * @property {number} [min]  Lower bound for numeric knobs (defaults to 0 on sliders).
 * @property {number} [max]  Known hard maximum. When set on a numeric knob the UI renders a slider.
 * @property {number} [step] Slider step (defaults to 1).
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
      { path: ["bringup", P, "battery", "warning_percentage"], label: "Low-battery warning", default: 20, type: "int", unit: "%", doc: "Low-battery warning level", min: 0, max: 100 },
      { path: ["bringup", P, "battery", "critical_percentage"], label: "Critical battery", default: 10, type: "int", unit: "%", doc: "Critical-battery level", min: 0, max: 100 },
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
  {
    section: "Camera",
    note: "Physical main camera (hardware only).",
    knobs: [
      { path: ["main_camera_driver", P, "publish_left_width"], label: "Image width", default: 640, type: "int", unit: "px", doc: "Streamed main-camera image width" },
      { path: ["main_camera_driver", P, "publish_left_height"], label: "Image height", default: 480, type: "int", unit: "px", doc: "Streamed main-camera image height" },
      { path: ["main_camera_driver", P, "fps"], label: "Frame rate", default: 30.0, type: "float", unit: "fps", doc: "Camera frame rate" },
      { path: ["main_camera_driver", P, "jpeg_quality"], label: "JPEG quality", default: 80, type: "int", doc: "JPEG compression quality (1–100)", min: 1, max: 100 },
      { path: ["main_camera_driver", P, "auto_exposure_mode"], label: "Auto-exposure mode", default: 0, type: "int", doc: "0 = hardware AE, 1 = custom PID, 2 = manual" },
      { path: ["main_camera_driver", P, "exposure"], label: "Manual exposure", default: -1, type: "int", doc: "Manual exposure time (-1 = keep current; 1–10000)" },
      { path: ["main_camera_driver", P, "gain"], label: "Manual gain", default: -1, type: "int", doc: "Manual gain (-1 = keep current; 0–255)" },
      { path: ["main_camera_driver", P, "default_gain"], label: "Auto-exposure gain", default: 110, type: "int", doc: "Gain used in auto-exposure mode (0–255)", min: 0, max: 255 },
      { path: ["main_camera_driver", P, "target_brightness"], label: "Target brightness", default: 128.0, type: "float", doc: "Auto-exposure target brightness (0–255)", min: 0, max: 255, step: 1 },
      { path: ["main_camera_driver", P, "ae_kp"], label: "Auto-exposure Kp", default: 0.8, type: "float", doc: "Auto-exposure proportional gain" },
    ],
  },
  {
    section: "Manipulation",
    note: "Learned-skill execution. Node-level, applied at startup (restart to apply); only n_action_steps accepts a per-skill behavior_config override.",
    knobs: [
      { path: ["manipulation_server", P, "inference_hz"], label: "Inference rate", default: 25.0, type: "float", unit: "Hz", doc: "Policy inference loop rate" },
      { path: ["manipulation_server", P, "speed"], label: "Execution speed", default: 1.5, type: "float", unit: "×", doc: "Action execution speed multiplier" },
      { path: ["manipulation_server", P, "replay_base_speed_scale"], label: "Replay base speed", default: 1.0, type: "float", unit: "×", doc: "Base-speed scale for replay (1.0 = recorded speed)" },
      { path: ["manipulation_server", P, "learned_base_speed_scale"], label: "Learned base speed", default: 1.0, type: "float", unit: "×", doc: "Base-speed scale for the learned policy (1.0 = full predicted speed)" },
      { path: ["manipulation_server", P, "n_action_steps"], label: "Replan horizon", default: 0, type: "int", doc: "Replan horizon; 0 = auto (min(40, chunk_size))" },
      { path: ["manipulation_server", P, "temporal_ensemble_coeff"], label: "Action smoothing", default: 0.0, type: "float", doc: "ACT temporal-ensemble coefficient; 0 = disabled (default). 0.01 is a good value to enable it" },
    ],
  },
  {
    section: "Localization",
    note: "Grid localizer.",
    knobs: [
      { path: ["navigation_grid_localizer", P, "max_score_threshold"], label: "Match threshold", default: 0.3, type: "float", doc: "Lower = stricter match required to accept a pose" },
      { path: ["navigation_grid_localizer", P, "max_range"], label: "Max lidar range", default: 12.0, type: "float", unit: "m", doc: "Max lidar range used for matching" },
      { path: ["navigation_grid_localizer", P, "auto_localize_timeout"], label: "Auto-localize timeout", default: 30.0, type: "float", unit: "s", doc: "Seconds to keep trying auto-localization on startup" },
    ],
  },
  {
    section: "Brain runtime (vision agent)",
    note: "The voice + model fields are also set on the realtime voice loop below — change both so the chat-TTS and realtime-voice paths stay in sync.",
    knobs: [
      { path: ["brain_client_node", P, "vertical_fov"], label: "Camera vertical FOV", default: 80.0, type: "float", unit: "°", doc: "Camera vertical field of view" },
      { path: ["brain_client_node", P, "pose_image_interval"], label: "Pose-image interval", default: 0.5, type: "float", unit: "s", doc: "Seconds between pose-image sends" },
      { path: ["brain_client_node", P, "scan_stale_after_sec"], label: "Scan stale after", default: 10.0, type: "float", unit: "s", doc: "Seconds without a lidar scan before flagging stale" },
      { path: ["brain_client_node", P, "send_depth"], label: "Send depth images", default: false, type: "bool", doc: "Also send depth images to the agent" },
      { path: ["brain_client_node", P, "send_arm_camera_image"], label: "Send arm-camera image", default: true, type: "bool", doc: "Also send the arm camera image" },
      { path: ["brain_client_node", P, "log_everything"], label: "Verbose logging", default: true, type: "bool", doc: "Verbose vision-agent output logging" },
      { path: ["brain_client_node", P, "cartesia_voice_id"], label: "TTS voice ID", default: "9fdaae0b-f885-4813-b589-3c07cf9d5fea", type: "string", doc: "Cartesia TTS voice" },
      { path: ["brain_client_node", P, "openai_realtime_model"], label: "Realtime model", default: "gpt-4o-realtime-preview", type: "string", doc: "OpenAI realtime model" },
      { path: ["brain_client_node", P, "openai_transcribe_model"], label: "Transcribe model", default: "gpt-4o-mini-transcribe", type: "string", doc: "OpenAI transcription model" },
    ],
  },
  {
    section: "Voice & speech (realtime loop)",
    note: "The realtime voice / transcription loop. Mirror the brain TTS voice + models above so both speech paths match.",
    knobs: [
      { path: ["input_manager_node", P, "cartesia_voice_id"], label: "TTS voice ID", default: "9fdaae0b-f885-4813-b589-3c07cf9d5fea", type: "string", doc: "Cartesia TTS voice" },
      { path: ["input_manager_node", P, "openai_realtime_model"], label: "Realtime model", default: "gpt-4o-realtime-preview", type: "string", doc: "OpenAI realtime model" },
      { path: ["input_manager_node", P, "openai_transcribe_model"], label: "Transcribe model", default: "gpt-4o-mini-transcribe", type: "string", doc: "OpenAI transcription model" },
    ],
  },
  {
    section: "Vision-language navigation (UniNavid)",
    note: "UniNavid drives in discrete action bursts. Its own speed knobs — separate from nav and teleop; the safety clamp is still the ceiling.",
    knobs: [
      { path: ["uninavid_node", P, "forward_speed"], label: "Forward speed", default: 0.3, type: "float", unit: "m/s", doc: "FORWARD action speed" },
      { path: ["uninavid_node", P, "turn_speed"], label: "Turn speed", default: 0.8, type: "float", unit: "rad/s", doc: "LEFT / RIGHT action speed" },
      { path: ["uninavid_node", P, "cmd_duration_sec"], label: "Command duration", default: 0.1, type: "float", unit: "s", doc: "How long each movement command runs" },
      { path: ["uninavid_node", P, "image_send_hz"], label: "Image send rate", default: 49.0, type: "float", unit: "Hz", doc: "Camera frames sent to the nav model per second" },
      { path: ["uninavid_node", P, "consecutive_stops_to_complete"], label: "Stops to complete", default: 30, type: "int", doc: "Stop predictions in a row before \"reached\"" },
      { path: ["uninavid_node", P, "cmd_publish_hz"], label: "Command publish rate", default: 50.0, type: "float", unit: "Hz", doc: "cmd_vel republish rate during a move" },
      { path: ["uninavid_node", P, "poll_period_sec"], label: "Poll period", default: 0.02, type: "float", unit: "s", doc: "Action-loop poll interval" },
    ],
  },
  {
    section: "Extra agent / skill directories",
    note: "Scan agents/skills from extra absolute paths, on top of the built-in workspace dirs. Paths are scanned in place (never created); in a Docker/sim setup they must also be mounted into the container.",
    knobs: [
      { path: ["script_paths", P, "extra_agent_dirs"], label: "Extra agent dirs", default: [], type: "list", doc: "Absolute paths scanned for agents" },
      { path: ["script_paths", P, "extra_skill_dirs"], label: "Extra skill dirs", default: [], type: "list", doc: "Absolute paths scanned for skills" },
    ],
  },
];
