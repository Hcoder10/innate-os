// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Capture-episode control for the Profiling page: records the *current rollout*
// as a full dataset episode on the robot (HDF5 + MP4s via recorder_node, plus
// the per-step inference-profile JSONL via profile_recorder), then labels it.
//
// Episodes land in a dedicated "Policy Rollouts" skill dataset (created on
// first use) so evaluation runs never mix into a real skill's training data,
// and they're immediately browsable/comparable on the Datasets page.
//
// Flow: Capture (activate + new_episode, pressed while/before a rollout runs)
// → Save or Discard → label ✓/✗ (set_episode_outcome) or Skip.

import { ros } from "../rosClient.js";
import {
  ACTIVATE_PHYSICAL_PRIMITIVE_SERVICE,
  CREATE_PHYSICAL_SKILL_SERVICE,
  GET_TASK_METADATA_SERVICE,
  RECORDER_CANCEL_EPISODE_SERVICE,
  RECORDER_NEW_EPISODE_SERVICE,
  RECORDER_SAVE_EPISODE_SERVICE,
  SET_EPISODE_OUTCOME_SERVICE,
} from "../constants.js";

const EVAL_SKILL_NAME = "Policy Rollouts";

export function buildCaptureControl() {
  const el = document.createElement("span");
  el.className = "prof-capture";

  const main = document.createElement("button");
  main.className = "prof-btn prof-btn-rec";
  const discard = document.createElement("button");
  discard.className = "prof-btn";
  discard.textContent = "Discard";
  const fail = document.createElement("button");
  fail.className = "prof-btn";
  fail.textContent = "✗ Failure";
  const skip = document.createElement("button");
  skip.className = "prof-btn";
  skip.textContent = "Skip";
  el.append(main, discard, fail, skip);

  let state = "idle"; // idle | starting | capturing | saving | labeling
  let taskDir = "";
  let busy = false;

  function sync(text) {
    main.textContent =
      text ??
      { idle: "⏺ Capture episode", starting: "Starting…", capturing: "Save episode", saving: "Saving…", labeling: "✓ Success" }[
        state
      ];
    main.classList.toggle("active", state === "capturing");
    discard.style.display = state === "capturing" ? "" : "none";
    fail.style.display = state === "labeling" ? "" : "none";
    skip.style.display = state === "labeling" ? "" : "none";
  }

  function toIdle(flash) {
    state = "idle";
    sync(flash);
    if (flash) setTimeout(() => sync(), 1600);
  }

  async function ensureEvalTask() {
    if (!taskDir) {
      // Idempotent: returns the existing directory when the skill already exists.
      const created = await ros.callService(CREATE_PHYSICAL_SKILL_SERVICE, { name: EVAL_SKILL_NAME, kind: "learned" });
      if (!created?.success || !created?.skill_directory) throw new Error(created?.message || "create failed");
      taskDir = created.skill_directory;
    }
    const activated = await ros.callService(ACTIVATE_PHYSICAL_PRIMITIVE_SERVICE, { task_directory: taskDir });
    if (!activated?.success) throw new Error("activate failed");
  }

  async function labelLast(outcome) {
    const meta = await ros.callService(GET_TASK_METADATA_SERVICE, { task_directory: taskDir });
    const episodes = JSON.parse(meta?.json_metadata || "{}")?.episodes || [];
    if (!episodes.length) throw new Error("no episode to label");
    const id = episodes[episodes.length - 1].episode_id;
    const res = await ros.callService(SET_EPISODE_OUTCOME_SERVICE, {
      task_directory: taskDir,
      episode_id: id,
      outcome,
    });
    if (!res?.success) throw new Error(res?.message || "label failed");
  }

  async function guarded(fn) {
    if (busy) return;
    busy = true;
    try {
      await fn();
    } catch (err) {
      console.error("[profiling] capture:", err);
      toIdle("Failed — see console");
    } finally {
      busy = false;
    }
  }

  main.addEventListener("click", () =>
    guarded(async () => {
      if (state === "idle") {
        state = "starting";
        sync();
        await ensureEvalTask();
        const res = await ros.callService(RECORDER_NEW_EPISODE_SERVICE, {});
        if (!res?.success) throw new Error(res?.message || "new_episode failed");
        state = "capturing";
        sync();
      } else if (state === "capturing") {
        state = "saving";
        sync();
        const res = await ros.callService(RECORDER_SAVE_EPISODE_SERVICE, {});
        if (!res?.success) throw new Error(res?.message || "save failed");
        state = "labeling";
        sync();
      } else if (state === "labeling") {
        await labelLast("success");
        toIdle("Saved ✓");
      }
    })
  );
  discard.addEventListener("click", () =>
    guarded(async () => {
      await ros.callService(RECORDER_CANCEL_EPISODE_SERVICE, {});
      toIdle("Discarded");
    })
  );
  fail.addEventListener("click", () =>
    guarded(async () => {
      await labelLast("failure");
      toIdle("Saved ✗");
    })
  );
  skip.addEventListener("click", () => toIdle("Saved"));

  sync();

  return {
    el,
    destroy() {
      // Don't leave the recorder wedged mid-episode when the page unmounts.
      if (state === "capturing" || state === "starting") {
        ros.callService(RECORDER_CANCEL_EPISODE_SERVICE, {}).catch(() => {});
      }
    },
  };
}
