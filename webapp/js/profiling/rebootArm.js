// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Reboot-arm button for the Profiling page. A policy rollout can drive the arm
// into an overload/error state where the servos latch off; power-cycling them
// is the recovery, and you want it without leaving the charts mid-eval. Same
// /mars/arm/reboot service the teleop arm panel uses, connection-gated and
// behind a confirm since it drops any running task. Torque re-enables
// automatically once the servos have re-initialized, matching teleop.

import { ros } from "../rosClient.js";
import { ARM_REBOOT_SERVICE, ARM_TORQUE_ON_SERVICE } from "../constants.js";

// Power-cycling walks the six servos and "takes a few seconds" — give the call
// the same generous headroom the teleop panel does.
const REBOOT_TIMEOUT_MS = 20_000;

export function buildRebootArm() {
  const btn = document.createElement("button");
  btn.className = "prof-btn prof-btn-reboot";
  btn.type = "button";

  let rebooting = false;

  /** @param {string} [flash] */
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
        "Reboot the arm servos? Any running task stops, the head recenters to level, and torque re-enables automatically after the power-cycle."
      )
    ) {
      return;
    }
    rebooting = true;
    sync();
    let outcome = "Reboot failed";
    try {
      const res = await ros.callService(ARM_REBOOT_SERVICE, {}, REBOOT_TIMEOUT_MS);
      if (!(res && res.success === false)) {
        // Torque back on by default — a rebooted arm is limp, and every eval
        // continued by re-enabling it anyway. The pause lets the servos finish
        // re-initializing first (mirrors Manipulation.recover()).
        await new Promise((resolve) => setTimeout(resolve, 2000));
        const tres = await ros.callService(ARM_TORQUE_ON_SERVICE, {}).catch(() => null);
        outcome = tres && tres.success !== false ? "Rebooted, torque on ✓" : "Rebooted — torque failed";
      }
    } catch (err) {
      console.error("[profiling] arm reboot:", err);
    }
    rebooting = false;
    sync(outcome);
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
