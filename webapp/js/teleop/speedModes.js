// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Manual-drive speed mode — Slow / Med / Fast segmented picker on the right rail.
//
// Speed mode is *robot* state, not page state: it is a ROS parameter on mars_app
// that scales the motion_control caps for every teleop client. So the buttons are
// driven by /robot/info (which republishes the live scale every second), not by
// what this page last clicked — change it from the mobile app and this updates,
// and vice versa. A click optimistically paints the new mode so the control feels
// immediate, then lets the next /robot/info confirm or correct it.

import {
  ROBOT_INFO_TOPIC,
  SET_PARAMETERS_SERVICE,
  SPEED_SCALE_PARAM,
  PARAMETER_DOUBLE,
  SPEED_MODES,
} from "../constants.js";

// A click's optimistic paint has to survive long enough for the robot to apply the
// parameter and emit a fresh /robot/info (published at 1 Hz), or the picker would
// visibly snap back to the old mode for one frame.
const OPTIMISTIC_HOLD_MS = 1500;

/**
 * Nearest preset to a raw scale, so a value set outside this picker (settings.yaml,
 * the mobile app, a hand-written set_parameters) still lights a sensible button.
 * @param {number} scale
 * @returns {(typeof SPEED_MODES)[number]}
 */
function modeForScale(scale) {
  let best = SPEED_MODES[0];
  for (const mode of SPEED_MODES) {
    if (Math.abs(mode.scale - scale) < Math.abs(best.scale - scale)) best = mode;
  }
  return best;
}

/**
 * @param {HTMLElement} parent
 * @param {import("../rosClient.js").RosClient} rosClient
 * @returns {{ el: HTMLElement, destroy: () => void }}
 */
export function createSpeedModes(parent, rosClient) {
  const wrap = document.createElement("div");
  wrap.className = "speed-modes";

  const title = document.createElement("span");
  title.className = "sm-label";
  title.textContent = "SPEED";
  wrap.appendChild(title);

  const group = document.createElement("div");
  group.className = "sm-group";
  group.setAttribute("role", "radiogroup");
  group.setAttribute("aria-label", "Manual drive speed");

  /** @type {Map<string, HTMLButtonElement>} */
  const buttons = new Map();
  /** @type {string | null} */
  let selectedId = null;
  let optimisticUntil = 0;

  /** @param {string} id */
  function paint(id) {
    selectedId = id;
    for (const [modeId, button] of buttons) {
      const on = modeId === id;
      button.classList.toggle("on", on);
      button.setAttribute("aria-checked", String(on));
    }
  }

  for (const mode of SPEED_MODES) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "sm-btn";
    button.textContent = mode.label;
    button.setAttribute("role", "radio");
    button.setAttribute("aria-checked", "false");
    button.addEventListener("click", () => {
      if (mode.id === selectedId) return;
      const previousId = selectedId;
      paint(mode.id);
      optimisticUntil = performance.now() + OPTIMISTIC_HOLD_MS;
      rosClient
        .callService(SET_PARAMETERS_SERVICE, {
          parameters: [{ name: SPEED_SCALE_PARAM, value: { type: PARAMETER_DOUBLE, double_value: mode.scale } }],
        })
        .then((response) => {
          // The service resolves even when the node rejects the value, so check
          // the per-parameter result rather than trusting the call succeeded.
          if (response?.results?.[0]?.successful === false) {
            throw new Error(response.results[0].reason || "rejected");
          }
        })
        .catch((err) => {
          // An older robot has no speed_scale parameter at all. Roll back rather
          // than leave the picker claiming a mode the robot isn't in.
          console.warn("Speed mode change failed:", err);
          optimisticUntil = 0;
          if (previousId) paint(previousId);
        });
    });
    buttons.set(mode.id, button);
    group.appendChild(button);
  }

  wrap.appendChild(group);
  parent.appendChild(wrap);

  const unsub = rosClient.subscribe(
    ROBOT_INFO_TOPIC,
    (payload) => {
      if (typeof payload?.data !== "string") return;
      /** @type {RobotInfo} */
      let info;
      try {
        info = JSON.parse(payload.data);
      } catch {
        return;
      }
      // Absent on robot software without speed modes — leave the picker blank
      // rather than guessing a mode the robot may not honour.
      if (typeof info.drive_speed_scale !== "number") return;
      if (performance.now() < optimisticUntil) return;
      paint(modeForScale(info.drive_speed_scale).id);
    },
    undefined,
    "std_msgs/msg/String",
  );

  return {
    el: wrap,
    destroy() {
      unsub();
      wrap.remove();
    },
  };
}
