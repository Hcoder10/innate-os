// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Datasets page entry — browse recorded episodes per skill. Disconnected: the
// same quiet connect card as teleop. Connected: the master/detail view — a
// live skill roster on the left (from /brain/available_skills), the selected
// skill's episodes on the right (from /brain/recorder/get_task_metadata).
// Mirrors the logging page's connect/view lifecycle.

import { ros } from "../rosClient.js";
import { mountPage } from "../pageMount.js";
import { AVAILABLE_SKILLS_TOPIC } from "../constants.js";
import { createSkillList } from "./skillList.js";
import { createEpisodeList } from "./episodeList.js";
import { createEpisodePlayer } from "./episodePlayer.js";

/** @param {HTMLElement} stage */
export function mount(stage) {
  return mountPage(stage, "datasets", buildView);
}

/**
 * @param {HTMLElement} root
 * @returns {{ destroy: () => void }}
 */
function buildView(root) {
  const head = document.createElement("div");
  head.className = "datasets-page-head";
  const heading = document.createElement("h1");
  heading.className = "page-title";
  heading.textContent = "Datasets";
  head.appendChild(heading);

  const grid = document.createElement("div");
  grid.className = "datasets-grid";
  const sideEl = document.createElement("aside");
  sideEl.className = "datasets-side";
  const mainEl = document.createElement("div");
  mainEl.className = "datasets-main";
  // The episode list and the replay player share the main pane; only one shows.
  const listHost = document.createElement("div");
  listHost.className = "datasets-pane";
  const playerHost = document.createElement("div");
  playerHost.className = "datasets-pane";
  playerHost.hidden = true;
  mainEl.append(listHost, playerHost);
  grid.append(sideEl, mainEl);
  root.append(head, grid);

  // Live roster of datasets an eval run can be copied into ("add to training
  // dataset"). Learned skills only — eval/replay datasets are never trained.
  /** @type {Skill[]} */
  let trainTargets = [];
  const unsubSkills = ros.subscribe(AVAILABLE_SKILLS_TOPIC, (msg) => {
    const all = Array.isArray(msg?.skills) ? /** @type {Skill[]} */ (msg.skills) : [];
    trainTargets = all
      .filter((s) => s && s.directory && s.type === "learned")
      .sort((a, b) => a.name.localeCompare(b.name));
  }, undefined, "brain_messages/msg/AvailableSkills");
  const getTargets = () => trainTargets;

  /** @type {{ destroy: () => void } | null} */
  let player = null;
  function closePlayer() {
    if (player) {
      player.destroy();
      player = null;
    }
    playerHost.hidden = true;
    listHost.hidden = false;
  }
  /** @param {Skill} skill @param {EpisodeSummary} ep */
  function openPlayer(skill, ep) {
    closePlayer();
    listHost.hidden = true;
    playerHost.hidden = false;
    // Resolve neighbors LIVE at click, not frozen at open: the list keeps
    // mutating underneath us as background encodes finish (the list re-fetches on
    // each encode "done") and as episodes are deleted. Show a button only if a
    // ready neighbor exists right now, and re-resolve on click so we never jump
    // to an episode that has since been deleted or is still preparing.
    const go = (/** @type {number} */ delta) => {
      const target = episodes.neighbor(ep, delta);
      if (target) openPlayer(skill, target);
    };
    // On back, refresh the list so label/delete changes made in the player show.
    player = createEpisodePlayer(playerHost, ros, skill, ep, {
      onBack: () => {
        closePlayer();
        episodes.show(skill);
      },
      onPrev: episodes.neighbor(ep, -1) ? () => go(-1) : null,
      onNext: episodes.neighbor(ep, 1) ? () => go(1) : null,
      getTargets,
    });
  }

  const episodes = createEpisodeList(listHost, ros, { onOpen: openPlayer, getTargets });
  const skills = createSkillList(sideEl, ros, {
    onSelect: (skill) => {
      closePlayer();
      episodes.show(skill);
    },
  });

  return {
    destroy() {
      unsubSkills();
      skills.destroy();
      episodes.destroy();
      if (player) player.destroy();
      root.innerHTML = "";
    },
  };
}
