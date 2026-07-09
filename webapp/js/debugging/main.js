// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Debugging page entry — wires the shared RosClient to the structured console
// stream: a chronological log on the left, a launch → process → node tree on the
// right (click a branch to scope the log). Disconnected: the same quiet connect
// card as teleop. Mirrors teleop's connect/cockpit lifecycle.

import { ros } from "../rosClient.js";
import { mountPage } from "../pageMount.js";
import { createConsoleSource } from "./consoleSource.js";
import { createLogStream } from "./logStream.js";
import { createSources } from "./sources.js";

/** @param {HTMLElement} stage */
export function mount(stage) {
  return mountPage(stage, "debug", buildView);
}

/**
 * @param {HTMLElement} root
 * @returns {{ destroy: () => void }}
 */
function buildView(root) {
  const head = document.createElement("div");
  head.className = "page-head";
  const heading = document.createElement("h1");
  heading.className = "page-title";
  heading.textContent = "Logs";
  head.appendChild(heading);

  // Phones: the sources pane doesn't fit next to the log -- a header button
  // toggles it as a slide-over drawer (CSS shows the button <= 700px).
  const sourcesToggle = document.createElement("button");
  sourcesToggle.type = "button";
  sourcesToggle.className = "sources-toggle";
  sourcesToggle.textContent = "Sources";
  head.appendChild(sourcesToggle);

  const grid = document.createElement("div");
  grid.className = "debug-grid";
  const mainEl = document.createElement("div");
  mainEl.className = "debug-main";
  const sideEl = document.createElement("aside");
  sideEl.className = "debug-side";
  const backdrop = document.createElement("div");
  backdrop.className = "sources-backdrop";
  grid.append(mainEl, backdrop, sideEl);
  root.append(head, grid);
  sourcesToggle.onclick = () => grid.classList.toggle("sources-open");
  backdrop.onclick = () => grid.classList.remove("sources-open");

  // One subscription, fanned out to both the log view and the sources panel.
  const source = createConsoleSource(ros);
  /** @type {ReturnType<typeof createSources>} */
  let sources;
  const log = createLogStream(mainEl, source, { onSourceClick: (rec) => sources.selectFromRecord(rec) });
  sources = createSources(sideEl, source, ros, {
    onSelect: (scope) => {
      log.setScope(scope);
      grid.classList.remove("sources-open"); // picking a source returns to the logs (phone drawer)
    },
  });

  return {
    destroy() {
      sources.destroy();
      log.destroy();
      source.destroy();
      root.innerHTML = "";
    },
  };
}
