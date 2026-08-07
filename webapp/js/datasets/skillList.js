// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Dataset roster — live list of recordable skills from /brain/available_skills,
// split into two always-visible sections with distinct identities:
//   "Training data"    — teleop demo datasets recorded by a human operator;
//                        what the robot learns from.
//   "Evaluation runs"  — type=="eval" rollout-capture datasets (judged policy
//                        runs saved from the Profiling page); never trained.
// They hold different kinds of episodes, so they must never read as one flat
// list. Only physical skills (those with a dataset `directory`) can have
// episodes, so code-only skills are filtered out. Click one to load its
// episodes.

import { AVAILABLE_SKILLS_TOPIC } from "../constants.js";

// Line-style icons, same stroke language as the rail (stroke currentColor).
const ICON_OPERATOR =
  '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="8" r="3.5"/><path d="M5 20c1.2-3.4 3.8-5 7-5s5.8 1.6 7 5"/></svg>';
const ICON_EVAL =
  '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12h4l2.5-6 4 12 2.5-6h5"/></svg>';

/**
 * @param {HTMLElement} parent
 * @param {import("../rosClient.js").RosClient} ros
 * @param {{ onSelect: (skill: Skill) => void }} opts
 * @returns {{ destroy: () => void }}
 */
export function createSkillList(parent, ros, opts) {
  const wrap = document.createElement("div");
  wrap.className = "skillpanel";

  const head = document.createElement("div");
  head.className = "skillpanel-head";
  const title = document.createElement("p");
  title.className = "microlabel";
  title.textContent = "datasets";
  title.title = "Datasets with a recording directory";
  const tally = document.createElement("span");
  tally.className = "skillpanel-count";
  head.append(title, tally);

  const listEl = document.createElement("div");
  listEl.className = "nodepanel-list";

  wrap.append(head, listEl);
  parent.appendChild(wrap);

  /** @type {Skill[]} */
  let skills = [];
  /** @type {string | null} */
  let selectedId = null;
  // Deep link from the Collect page's "Review" link: ?dir=<task_directory>.
  // Auto-selected once its skill appears in the roster, then cleared.
  let pendingDir = new URLSearchParams(location.search).get("dir");

  /** @param {Skill} skill @param {boolean} isEval */
  function skillRow(skill, isEval) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "skill-row" + (isEval ? " eval" : "") + (skill.id === selectedId ? " active" : "");
    row.title = skill.directory || skill.name;

    const name = document.createElement("span");
    name.className = "skill-name";
    name.textContent = skill.name;

    const count = document.createElement("span");
    count.className = "skill-count mono";
    const n = skill.episode_count ?? 0;
    count.textContent = isEval ? `${n} run${n === 1 ? "" : "s"}` : `${n} ep${n === 1 ? "" : "s"}`;

    row.append(name, count);
    row.addEventListener("click", () => {
      selectedId = skill.id;
      render();
      opts.onSelect(skill);
    });
    return row;
  }

  /** @param {string} label @param {string} icon @param {boolean} isEval */
  function sectionHead(label, icon, isEval) {
    const p = document.createElement("p");
    p.className = "skillpanel-group microlabel" + (isEval ? " eval" : "");
    p.title = isEval
      ? "Judged policy rollouts — never used for training"
      : "Operator demonstrations — what the robot trains on";
    p.innerHTML = icon;
    p.appendChild(document.createTextNode(label));
    return p;
  }

  /** @param {string} text */
  function sectionHint(text) {
    const p = document.createElement("p");
    p.className = "skillpanel-hint";
    p.textContent = text;
    return p;
  }

  function render() {
    const demos = skills.filter((s) => s.type !== "eval");
    const evals = skills.filter((s) => s.type === "eval");

    const frag = document.createDocumentFragment();
    // Both sections always render, even empty — the split between operator
    // demonstrations and policy evaluations is the page's core distinction.
    frag.appendChild(sectionHead("Training data", ICON_OPERATOR, false));
    if (demos.length) {
      for (const skill of demos) frag.appendChild(skillRow(skill, false));
    } else {
      frag.appendChild(sectionHint("No recorded skills yet — record demonstrations on the Collect page."));
    }
    frag.appendChild(sectionHead("Evaluation runs", ICON_EVAL, true));
    if (evals.length) {
      for (const skill of evals) frag.appendChild(skillRow(skill, true));
    } else {
      frag.appendChild(sectionHint("No evaluations yet — run policy rollouts from the Profiling page."));
    }
    listEl.replaceChildren(frag);
    tally.textContent = String(skills.length);
  }

  const unsub = ros.subscribe(AVAILABLE_SKILLS_TOPIC, (msg) => {
    const all = Array.isArray(msg?.skills) ? /** @type {Skill[]} */ (msg.skills) : [];
    // Only physical skills carry a dataset directory; the rest have no episodes.
    // Replay (recorded-movement) skills carry a directory too, but their dataset
    // is dropped on save — they're deterministic trajectories, not datasets — so
    // keep them out of the roster.
    skills = all
      .filter((s) => s && s.directory && s.type !== "replay")
      .sort((a, b) => a.name.localeCompare(b.name));
    // Drop the selection if its skill vanished from the roster.
    if (selectedId && !skills.some((s) => s.id === selectedId)) selectedId = null;
    render();
    // Honor a pending deep link once its skill shows up.
    if (pendingDir) {
      const match = skills.find((s) => s.directory === pendingDir);
      if (match) {
        pendingDir = null;
        selectedId = match.id;
        render();
        opts.onSelect(match);
      }
    }
  }, undefined, "brain_messages/msg/AvailableSkills");

  render();

  return {
    destroy() {
      unsub();
      wrap.remove();
    },
  };
}
