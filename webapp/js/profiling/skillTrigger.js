// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Skill picker + Run/Stop for the Profiling page: lets an operator start a
// learned-skill rollout without leaving the charts. Reuses the same roster
// topic and action call as the teleop skills menu (createSkillsMenu.js) —
// filtered to type "learned" since that's the only kind that produces
// anything on this page. Physical skills never carry an inputs schema (that's
// a code-skill-only concept), so unlike the teleop menu this needs no
// per-skill param form.

import { ros } from "../rosClient.js";
import { AVAILABLE_SKILLS_TOPIC, EXECUTE_SKILL_ACTION, EXECUTE_SKILL_ACTION_TYPE } from "../constants.js";

export function buildSkillTrigger() {
  const el = document.createElement("span");
  el.className = "prof-skill-trigger";

  const select = document.createElement("select");
  select.className = "prof-select";
  const placeholder = new Option("Select a learned skill…", "");
  select.add(placeholder);

  const runBtn = document.createElement("button");
  runBtn.className = "prof-btn";
  runBtn.textContent = "▶ Run";
  runBtn.disabled = true;

  el.append(select, runBtn);

  /** @type {(() => void) | null} */
  let cancel = null;

  function syncRunEnabled() {
    runBtn.disabled = !cancel && !select.value;
  }
  select.addEventListener("change", syncRunEnabled);

  const unsubSkills = ros.subscribe(AVAILABLE_SKILLS_TOPIC, (msg) => {
    const all = Array.isArray(msg?.skills) ? msg.skills : [];
    const learned = all.filter((s) => s?.id && s.type === "learned" && !s.in_training);
    const prevValue = select.value;
    select.replaceChildren(placeholder);
    for (const s of learned) select.add(new Option(s.name || s.id, s.id));
    select.value = learned.some((s) => s.id === prevValue) ? prevValue : "";
    syncRunEnabled();
  });

  function flash(text) {
    runBtn.textContent = text;
    setTimeout(() => {
      runBtn.textContent = "▶ Run";
    }, 1600);
  }

  runBtn.addEventListener("click", () => {
    if (cancel) {
      cancel();
      return;
    }
    if (!select.value) return;
    const { promise, cancel: cancelFn } = ros.sendActionGoal(EXECUTE_SKILL_ACTION, EXECUTE_SKILL_ACTION_TYPE, {
      skill_type: select.value,
      inputs: "{}",
    });
    cancel = cancelFn;
    runBtn.textContent = "■ Stop";
    select.disabled = true;
    syncRunEnabled();
    promise.then(
      (values) => {
        cancel = null;
        select.disabled = false;
        const ok = values?.success !== false && values?.success_type !== "failure";
        flash(values?.success_type === "cancelled" ? "Stopped" : ok ? "Done" : "Failed");
        syncRunEnabled();
      },
      (err) => {
        cancel = null;
        select.disabled = false;
        flash(err?.message ? "Failed" : "Stopped");
        syncRunEnabled();
      },
    );
  });

  return {
    el,
    destroy() {
      unsubSkills();
      // Mirrors createSkillsMenu: don't leave a skill running headless once its
      // controls are gone.
      if (cancel) cancel();
    },
  };
}
