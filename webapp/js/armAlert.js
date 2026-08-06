// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Servo-protection alert — a discrete amber card pinned top-right on every
// page while /mars/arm/status reports a latched servo hardware error
// (overcurrent/overload protection). The servo stays tripped until a reboot,
// so the card carries the reboot button; it hides on its own once the status
// clears. Dismissing suppresses the card until the error changes or clears.

import { rebootArmAndEnableTorque } from "./armReboot.js";
import {
  ARM_REBOOT_CONFIRM,
  ARM_STATUS_TOPIC,
} from "./constants.js";

/**
 * @param {import("./rosClient.js").RosClient} rosClient
 * @returns {{ el: HTMLElement, destroy: () => void }}
 */
export function createArmAlert(rosClient) {
  const card = document.createElement("aside");
  card.className = "arm-alert";
  card.hidden = true;

  const head = document.createElement("div");
  head.className = "arm-alert-head";
  const dot = document.createElement("span");
  dot.className = "arm-alert-dot";
  const title = document.createElement("span");
  title.className = "microlabel";
  title.textContent = "servo protection";
  const dismissBtn = document.createElement("button");
  dismissBtn.type = "button";
  dismissBtn.className = "arm-alert-dismiss";
  dismissBtn.textContent = "×";
  dismissBtn.title = "Dismiss";
  dismissBtn.setAttribute("aria-label", "Dismiss");
  head.append(dot, title, dismissBtn);

  const text = document.createElement("p");
  text.className = "arm-alert-text";

  const rebootBtn = document.createElement("button");
  rebootBtn.type = "button";
  rebootBtn.className = "arm-button";

  card.append(head, text, rebootBtn);
  document.body.appendChild(card);

  let currentError = ""; // live hardware-error string ("" = none)
  let dismissedError = ""; // suppressed until the error changes or clears
  let rebooting = false;
  let flash = ""; // reboot-failure line, shown until the next status

  function render() {
    const active = rosClient.state === "connected" && currentError !== "" && currentError !== dismissedError;
    card.hidden = !active;
    if (!active) return;
    // The status error trails latched-flag advice after an em dash; the card
    // IS that advice, so show just the fault itself.
    text.textContent = flash || currentError.split(" — ")[0];
    text.classList.toggle("warn", flash !== "");
    rebootBtn.disabled = rebooting;
    rebootBtn.textContent = rebooting ? "Rebooting…" : "Reboot arm";
  }

  dismissBtn.addEventListener("click", () => {
    dismissedError = currentError;
    render();
  });

  rebootBtn.addEventListener("click", async () => {
    if (rebooting || !window.confirm(ARM_REBOOT_CONFIRM)) return;
    rebooting = true;
    flash = "";
    render();
    try {
      const res = await rebootArmAndEnableTorque(rosClient);
      if (!res.ok || !res.torqueOn) {
        flash = res.message;
      } else {
        // Cleared optimistically; a still-faulty servo re-latches and the
        // next status (~5 s) brings the card back.
        currentError = "";
        dismissedError = "";
      }
    } catch (err) {
      flash = err instanceof Error ? err.message : "Reboot failed";
    } finally {
      rebooting = false;
      render();
    }
  });

  const unsubStatus = rosClient.subscribe(
    ARM_STATUS_TOPIC,
    (m) => {
      if (!m || typeof m.error !== "string") return;
      // Only the latched hardware errors ("Servo N hardware error: overload,
      // ...") — a reboot clears those. Transient high-load/high-temperature
      // warnings share is_ok=false but are not reboot-fixable.
      const tripped = m.is_ok === false && /hardware error/i.test(m.error);
      const error = tripped ? m.error : "";
      if (error !== currentError) flash = "";
      currentError = error;
      if (!tripped) dismissedError = "";
      render();
    },
    undefined,
    "mars_msgs/msg/ArmStatus",
  );
  const unsubState = rosClient.onStateChange(render);
  render();

  return {
    el: card,
    destroy() {
      unsubStatus();
      unsubState();
      card.remove();
    },
  };
}
