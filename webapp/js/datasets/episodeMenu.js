// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Episode context menu + "add to training dataset" flow, shared by the episode
// list (right-click / row action) and the player. The promote action copies an
// eval rollout into a chosen training dataset via CopyEpisode — files (h5,
// MP4s, inference-profile trace) and provenance travel with the copy, and the
// eval episode stays where it is, so the evaluation record is never consumed.

import { COPY_EPISODE_SERVICE } from "../constants.js";

/** @type {HTMLElement | null} currently-open menu (only one at a time) */
let openMenu = null;

function closeMenu() {
  if (openMenu) {
    openMenu.remove();
    openMenu = null;
    document.removeEventListener("pointerdown", onOutside, true);
    document.removeEventListener("keydown", onKey, true);
    window.removeEventListener("blur", closeMenu);
  }
}

/** @param {PointerEvent} e */
function onOutside(e) {
  if (openMenu && !openMenu.contains(/** @type {Node} */ (e.target))) closeMenu();
}

/** @param {KeyboardEvent} e */
function onKey(e) {
  if (e.key === "Escape") closeMenu();
}

/**
 * Open the episode menu at (x, y) in viewport coordinates.
 * @param {{
 *   x: number, y: number,
 *   ros: import("../rosClient.js").RosClient,
 *   sourceDir: string, episodeId: number,
 *   targets: Skill[],
 *   onReplay?: (() => void) | null,
 *   onDelete?: (() => void) | null,
 *   onCopied?: ((target: Skill, newId: number) => void) | null,
 * }} opts
 */
export function openEpisodeMenu(opts) {
  closeMenu();
  const menu = document.createElement("div");
  menu.className = "ctx-menu";

  /** @param {string} label @param {() => void} onPick @param {string} [cls] */
  function item(label, onPick, cls = "") {
    const b = document.createElement("button");
    b.type = "button";
    b.className = `ctx-item ${cls}`.trim();
    b.textContent = label;
    b.addEventListener("click", () => {
      closeMenu();
      onPick();
    });
    menu.appendChild(b);
    return b;
  }

  function separator() {
    const s = document.createElement("div");
    s.className = "ctx-sep";
    menu.appendChild(s);
  }

  if (opts.onReplay) item("Replay episode", opts.onReplay);

  const head = document.createElement("p");
  head.className = "ctx-head microlabel";
  head.textContent = "add to training dataset";
  menu.appendChild(head);
  if (opts.targets.length) {
    for (const target of opts.targets) {
      item(target.name, () => copyTo(opts, target), "ctx-target");
    }
  } else {
    const none = document.createElement("p");
    none.className = "ctx-empty";
    none.textContent = "No training datasets yet.";
    menu.appendChild(none);
  }

  if (opts.onDelete) {
    separator();
    item("Delete episode", opts.onDelete, "danger");
  }

  document.body.appendChild(menu);
  // Clamp inside the viewport (menu is position:fixed).
  const r = menu.getBoundingClientRect();
  menu.style.left = `${Math.min(opts.x, window.innerWidth - r.width - 8)}px`;
  menu.style.top = `${Math.min(opts.y, window.innerHeight - r.height - 8)}px`;

  openMenu = menu;
  document.addEventListener("pointerdown", onOutside, true);
  document.addEventListener("keydown", onKey, true);
  window.addEventListener("blur", closeMenu);
}

// A copy is a real file copy on the robot (h5 + per-camera MP4s + profile
// trace), seconds each — and the recorder serializes them server-side. Firing
// N calls at once just lets the later ones' timers expire while they wait in
// line, so copies are chained here: each call's window covers only its own
// copy. The window itself is sized for a long episode on robot storage.
const COPY_TIMEOUT_MS = 60_000;
/** @type {Promise<void>} tail of the copy queue */
let copyChain = Promise.resolve();

/**
 * @param {{ ros: import("../rosClient.js").RosClient, sourceDir: string, episodeId: number,
 *   onCopied?: ((target: Skill, newId: number) => void) | null }} opts
 * @param {Skill} target
 */
function copyTo(opts, target) {
  toast(`Adding episode #${opts.episodeId} to ${target.name}…`, "");
  copyChain = copyChain
    .then(() =>
      opts.ros.callService(
        COPY_EPISODE_SERVICE,
        {
          source_task_directory: opts.sourceDir,
          episode_id: opts.episodeId,
          dest_task_directory: target.directory,
        },
        COPY_TIMEOUT_MS,
      ),
    )
    .then((res) => {
      if (!res?.success) throw new Error(res?.message || "copy failed");
      toast(`Episode #${opts.episodeId} added to ${target.name} as #${res.new_episode_id} ✓`, "ok");
      if (opts.onCopied) opts.onCopied(target, res.new_episode_id);
    })
    .catch((err) => {
      console.error("[datasets] copy episode failed:", err);
      // A timed-out copy isn't cancelled robot-side — it may still land.
      // Say so, or a retry quietly duplicates the episode.
      const note = /timed out/i.test(err.message) ? " — it may still finish; refresh before retrying" : "";
      toast(`Couldn't add episode #${opts.episodeId}: ${err.message}${note}`, "fail");
    });
}

/** @type {HTMLElement | null} */
let toastEl = null;
/** @type {number | null} */
let toastTimer = null;

/** Transient bottom-corner notice, replaced by each new message.
 * @param {string} text @param {"ok"|"fail"|""} kind */
function toast(text, kind) {
  if (!toastEl) {
    toastEl = document.createElement("div");
    document.body.appendChild(toastEl);
  }
  toastEl.className = `ep-toast ${kind}`.trim();
  toastEl.textContent = text;
  if (toastTimer !== null) clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => {
    toastEl?.remove();
    toastEl = null;
    toastTimer = null;
  }, 4000);
}
