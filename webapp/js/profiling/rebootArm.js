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
import { ARM_REBOOT_CONFIRM } from "../constants.js";
import { rebootArmAndEnableTorque } from "../armReboot.js";

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
    if (!window.confirm(ARM_REBOOT_CONFIRM)) return;
    rebooting = true;
    sync();
    const res = await rebootArmAndEnableTorque(ros);
    rebooting = false;
    sync(res.ok && res.torqueOn ? "Rebooted, torque on \u2713" : res.message);
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
