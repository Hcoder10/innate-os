// @ts-check

/** @type {Array<{label: string, value: "running" | "completed" | "failed" | "interrupted" | null}>} */
const STATES = [
  { label: "off", value: null },
  { label: "running", value: "running" },
  { label: "done", value: "completed" },
  { label: "failed", value: "failed" },
  { label: "stopped", value: "interrupted" },
];

const SKILLS = [
  {
    label: "Navigate to position",
    name: "navigate_to_position",
    args: { x: 1.25, y: -0.4, theta_degrees: 89.18, local_frame: false },
    reason: "Navigation failed because no collision-free path could be found.",
  },
  {
    label: "Search memory",
    name: "search_memory",
    args: { query: "the AirPods case I last saw near the living room couch" },
    reason: "Spatial memory was unavailable.",
  },
  {
    label: "Head emotion",
    name: "head_emotion",
    args: { emotion: "very_happy", repeat: 2 },
    reason: "The head did not reach the requested pose.",
  },
  {
    label: "Turn in place",
    name: "turn_in_place",
    args: { angle_degrees: -90, speed: 0.5 },
    reason: "The robot stopped turning before reaching -90 degrees.",
  },
  {
    label: "Move straight",
    name: "move_straight",
    args: { distance: 1.5, speed: 0.15 },
    reason: "The robot stopped before reaching the requested distance.",
  },
  {
    label: "Arm move to XYZ",
    name: "arm_move_to_xyz",
    args: { x: 0.3, y: -0.05, z: 0.2, roll: 0, pitch: 0, yaw: 1.57, duration: 3 },
    reason: "The requested pose was outside the arm workspace.",
  },
  {
    label: "Arm circle motion",
    name: "arm_circle_motion",
    args: {
      center_x: 0.3,
      center_y: -0.05,
      center_z: 0.2,
      radius: 0.1,
      num_loops: 2,
      points_per_loop: 16,
      duration_per_point: 0.5,
    },
    reason: "The circular path crossed the edge of the arm workspace.",
  },
  {
    label: "Pick up chess piece",
    name: "pick_up_piece_simple",
    args: { square: "e2", place_square: "e4", piece: "pawn", is_capture: false, speed: 1 },
    reason: "The piece could not be grasped securely.",
  },
  {
    label: "Send email",
    name: "send_email",
    args: {
      subject: "Robot mission update",
      message: "I found the requested object beside the living room couch.",
      recipients: ["alex@example.com"],
    },
    reason: "The email service rejected the message.",
  },
  {
    label: "Send picture via email",
    name: "send_picture_via_email",
    args: {
      subject: "Photo from MARS",
      message: "Here is the object I found during the search.",
      recipient: "alex@example.com",
    },
    reason: "The camera image could not be attached to the email.",
  },
  {
    label: "Navigate with vision",
    name: "navigate_with_vision",
    args: { instruction: "Find the orange block near the couch and stop directly in front of it" },
    reason: "The visual navigation model could not identify a safe route.",
  },
  {
    label: "Pick any object",
    name: "pick_any_object",
    args: { prompt: "the bright orange block lying beside the living room couch" },
    reason: "No reachable object matched the prompt.",
  },
  {
    label: "Wave",
    name: "wave",
    args: {},
    reason: "The recorded motion could not be started.",
  },
];

/**
 * @param {HTMLElement} root
 * @param {(sample: {
 *   name: string,
 *   args: Record<string, unknown>,
 *   status: "running" | "completed" | "failed" | "interrupted",
 *   reason?: string
 * } | null) => void} preview
 */
export function createSkillCardPreviewControl(root, preview) {
  /** @type {"running" | "completed" | "failed" | "interrupted" | null} */
  let currentStatus = null;
  let skillIndex = 0;
  const control = document.createElement("aside");
  control.className = "agent-skill-preview-control";
  control.setAttribute("aria-label", "Skill card preview");

  const previousSkill = skillButton("‹", "Previous preview skill");
  const skillName = document.createElement("span");
  skillName.className = "agent-skill-preview-name";
  skillName.textContent = SKILLS[skillIndex].label;
  const nextSkill = skillButton("›", "Next preview skill");

  function renderPreview() {
    if (!currentStatus) {
      preview(null);
      return;
    }
    preview({ ...SKILLS[skillIndex], status: currentStatus });
  }

  /** @param {number} direction */
  function cycleSkill(direction) {
    skillIndex = (skillIndex + direction + SKILLS.length) % SKILLS.length;
    skillName.textContent = SKILLS[skillIndex].label;
    renderPreview();
  }

  const buttons = STATES.map(({ label: text, value }) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "agent-skill-preview-option mono";
    if (value) button.classList.add(value);
    button.textContent = text;
    button.setAttribute("aria-pressed", String(value === null));
    button.addEventListener("click", () => {
      for (const option of buttons) option.setAttribute("aria-pressed", String(option === button));
      currentStatus = value;
      renderPreview();
    });
    return button;
  });

  previousSkill.addEventListener("click", () => cycleSkill(-1));
  nextSkill.addEventListener("click", () => cycleSkill(1));
  control.append(previousSkill, skillName, nextSkill, ...buttons);
  root.append(control);
  preview(null);

  return {
    destroy() {
      preview(null);
      control.remove();
    },
  };
}

/** @param {string} text @param {string} label */
function skillButton(text, label) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "agent-skill-preview-cycle";
  button.textContent = text;
  button.setAttribute("aria-label", label);
  return button;
}
