// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Reboot-arm button for the Profiling page. A policy rollout can drive the arm
// into an overload/error state where the servos latch off; power-cycling them
// is the recovery, and you want it without leaving the charts mid-eval. Same
// /mars/arm/reboot service the teleop arm panel uses, connection-gated and
// behind a confirm since it drops any running task and leaves the arm limp
// (torque off) until it's re-enabled.

import { ros } from "../rosClient.js";
import { ARM_REBOOT_SERVICE } from "../constants.js";

// Power-cycling walks the six servos and "takes a few seconds" — give the call
// the same generous headroom the teleop panel does.
const REBOOT_TIMEOUT_MS = 20_000;

export function buildRebootArm() {
  const btn = document.createElement("button");
  btn.className = "prof-btn prof-btn-reboot";
  btn.type = "button";

  let rebooting = false;

  function sync(flash) {
    btn.disabled = rebooting || ros.state !== "connected";
    btn.textContent = flash ?? (rebooting ? "Rebooting…" : "⟳ Reboot arm");
  }

  // Enable/disable with the connection; onStateChange fires immediately with
  // the current state, so this also sets the initial disabled state.
  const unsub = ros.onStateChange(() => {
    if (!rebooting) sync();
  });

  btn.addEventListener("click", async () => {
    if (rebooting) return;
    if (
      !window.confirm(
        "Reboot the arm servos? Any running task stops, the head recenters to level, and the arm goes limp (torque off) until you re-enable it."
      )
    ) {
      return;
    }
    rebooting = true;
    sync();
    try {
      const res = await ros.callService(ARM_REBOOT_SERVICE, {}, REBOOT_TIMEOUT_MS);
      rebooting = false;
      sync(res && res.success === false ? "Reboot failed" : "Rebooted ✓");
    } catch (err) {
      rebooting = false;
      console.error("[profiling] arm reboot:", err);
      sync("Reboot failed");
    }
    setTimeout(() => sync(), 1600);
  });

  sync();

  return {
    el: btn,
    destroy() {
      unsub();
    },
  };
}
