// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Headless robot-side episode capture for eval rollouts: the same recorder
// services the collect page uses, aimed at a dedicated "Policy Rollouts"
// skill dataset (created on first use) so evaluation runs never mix into a
// real skill's training data. Owns no UI — rolloutControl.js drives it.
//
// start() → activate + new_episode, save() → persist HDF5/MP4/profile JSONL,
// discard() → cancel without writing, labelLast(outcome) → set ✓/✗ on the
// episode save() just wrote.

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

export function createEpisodeCapture() {
  let taskDir = "";

  async function ensureEvalTask() {
    if (!taskDir) {
      // Idempotent: returns the existing directory when the skill already exists.
      // "eval" kind: a rollout-capture dataset that can never be selected for
      // training (training filters to type=="learned").
      const created = await ros.callService(CREATE_PHYSICAL_SKILL_SERVICE, { name: EVAL_SKILL_NAME, kind: "eval" });
      if (!created?.success || !created?.skill_directory) throw new Error(created?.message || "create_physical_skill failed");
      taskDir = created.skill_directory;
    }
    const activated = await ros.callService(ACTIVATE_PHYSICAL_PRIMITIVE_SERVICE, { task_directory: taskDir });
    if (!activated?.success) throw new Error(activated?.message || "activate_physical_primitive failed");
  }

  return {
    /** Open a new episode on the robot (recorder starts buffering). Stamps the
     * episode as a rollout driven by `policy` (the evaluated skill's id).
     * @param {string} policy */
    async start(policy) {
      await ensureEvalTask();
      const res = await ros.callService(RECORDER_NEW_EPISODE_SERVICE, { source: "rollout", policy: policy || "" });
      if (!res?.success) throw new Error(res?.message || "new_episode failed");
    },

    /** Persist the open episode (HDF5 + MP4s + profile trace). */
    async save() {
      const res = await ros.callService(RECORDER_SAVE_EPISODE_SERVICE, {});
      if (!res?.success) throw new Error(res?.message || "save failed");
    },

    /** Drop the open episode without writing anything. Best-effort. */
    async discard() {
      await ros.callService(RECORDER_CANCEL_EPISODE_SERVICE, {}).catch(() => {});
    },

    /** Label the most recently saved episode with an outcome and failure-mode
     * tags. @param {"success"|"failure"} outcome @param {string[]} [tags] */
    async labelLast(outcome, tags = []) {
      const meta = await ros.callService(GET_TASK_METADATA_SERVICE, { task_directory: taskDir });
      const episodes = JSON.parse(meta?.json_metadata || "{}")?.episodes || [];
      if (!episodes.length) throw new Error("no episode to label");
      const id = numericEpisodeId(episodes[episodes.length - 1]);
      const res = await ros.callService(SET_EPISODE_OUTCOME_SERVICE, {
        task_directory: taskDir,
        episode_id: id,
        outcome,
        tags,
      });
      if (!res?.success) throw new Error(res?.message || "label failed");
    },
  };
}
