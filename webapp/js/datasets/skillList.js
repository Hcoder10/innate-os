// @ts-check
// Skill roster — live list of recordable skills from /brain/available_skills.
// Only physical skills (those with a dataset `directory`) can have episodes, so
// code-only skills are filtered out. Click a skill to load its episodes.

import { AVAILABLE_SKILLS_TOPIC } from "../constants.js";

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
  title.textContent = "skills";
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

  function render() {
    const frag = document.createDocumentFragment();
    for (const skill of skills) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = "skill-row" + (skill.id === selectedId ? " active" : "");
      row.title = skill.directory || skill.name;

      const name = document.createElement("span");
      name.className = "skill-name";
      name.textContent = skill.name;

      const count = document.createElement("span");
      count.className = "skill-count mono";
      const n = skill.episode_count ?? 0;
      count.textContent = `${n} ep${n === 1 ? "" : "s"}`;

      row.append(name, count);
      row.addEventListener("click", () => {
        selectedId = skill.id;
        render();
        opts.onSelect(skill);
      });
      frag.appendChild(row);
    }
    listEl.replaceChildren(frag);
    tally.textContent = String(skills.length);
    if (skills.length === 0) {
      const empty = document.createElement("p");
      empty.className = "datasets-empty";
      empty.textContent = "No recorded skills yet.";
      listEl.appendChild(empty);
    }
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
  });

  render();

  return {
    destroy() {
      unsub();
      wrap.remove();
    },
  };
}
