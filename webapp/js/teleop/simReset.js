// @ts-check
// Sim-only: respawn the virtual robot at its spawn pose. Cancels any active
// navigation first, then re-seeds AMCL at the new pose — without that, the
// teleport leaves localization pointing at wherever the robot used to be and
// every map-frame goal afterwards goes somewhere random.

import { CANCEL_NAVIGATION_SERVICE, LOCALIZE_SERVICE, VIRTUAL_MARS_RESET_TOPIC } from "../constants.js";

/**
 * @param {HTMLElement} root the arm overlay — the button mounts as its top row.
 * @param {import("../rosClient.js").RosClient} rosClient
 * @returns {{ destroy: () => void }}
 */
export function createSimReset(root, rosClient) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "sim-reset-btn sim-button";
  btn.textContent = "Reset Position";
  btn.title = "Respawn the simulated robot at its spawn pose (re-seeds localization)";
  root.appendChild(btn);

  /** @type {ReturnType<typeof setTimeout> | undefined} */
  let timer;
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    btn.textContent = "Resetting…";
    try {
      await rosClient.callService(CANCEL_NAVIGATION_SERVICE, {}).catch(() => {});
      rosClient.publish(VIRTUAL_MARS_RESET_TOPIC, {});
      await new Promise((resolve) => {
        timer = setTimeout(resolve, 1500); // let the world settle at spawn
      });
      await rosClient.callService(LOCALIZE_SERVICE, {});
      btn.textContent = "Reset ✓";
    } catch (err) {
      console.error("[sim] reset failed:", err);
      btn.textContent = "Reset failed";
    }
    timer = setTimeout(() => {
      btn.textContent = "Reset Position";
      btn.disabled = false;
    }, 1500);
  });

  return {
    destroy() {
      clearTimeout(timer);
      btn.remove();
    },
  };
}
