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
// → Save or Discard → label ✓/✗ (set_episode_outcome). Leaving it unlabeled and
// navigating away is fine too — the Datasets page can set/change any episode's
// outcome later, so there's no separate "skip" affordance here.

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

/** GetTaskMetadata's enriched episodes carry episode_id as the display string
 * "episode_0", not a number (see task_manager.cpp get_enriched_metadata_for_task)
 * — sending that string straight into SetEpisodeOutcome's int32 field corrupts
 * it. Same extraction datasets/episodeList.js uses. @param {any} ep */
function numericEpisodeId(ep) {
  const m = /(\d+)/.exec(String(ep.episode_id)) || /(\d+)/.exec(ep.file_name || "");
  return m ? Number(m[1]) : 0;
}

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
  el.append(main, discard, fail);

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
    const id = numericEpisodeId(episodes[episodes.length - 1]);
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
