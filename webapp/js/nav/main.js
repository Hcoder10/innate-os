// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Nav page entry — a Foxglove-style live view of everything navigation:
// the map widget as the scene (occupancy grid, robot pose, route, goal) with
// its overlay layers on (laser scan, global costmap, odometry trail), and a
// telemetry sidebar of raw sensor readouts (pose, velocity, lidar, nav state,
// per-topic receive rates). All over the shared rosbridge socket — same data
// path as the rest of the app, no extra bridge.

import { mountPage } from "../pageMount.js";
import { createMap } from "../map/mapWidget.js";
import { createNavPanels } from "./panels.js";
import { createNavPlots } from "./plots.js";

// Scene default: robot-centred window, wheel-zoomable (Foxglove-like), not
// the fit-whole-grid mode — remembered across visits.
const ZOOM_KEY = "innate.navZoom";
const DEFAULT_ZOOM_M = 14;

/** @type {Array<{ key: import("../map/mapWidget.js").LayerName, label: string }>} */
const LAYERS = [
  { key: "scan", label: "Scan" },
  { key: "costmap", label: "Costmap" },
  { key: "trail", label: "Trail" },
];

/** @param {HTMLElement} stage */
export function mount(stage) {
  return mountPage(stage, "nav-page", buildView);
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
  heading.textContent = "Nav";
  head.appendChild(heading);

  // Layer toggles, Foxglove-style: each chip live-toggles its overlay (and
  // its subscription) on the scene.
  const chips = document.createElement("div");
  chips.className = "layer-chips";
  head.appendChild(chips);

  const grid = document.createElement("div");
  grid.className = "nav-grid";
  const scene = document.createElement("div");
  scene.className = "nav-scene";
  const side = document.createElement("aside");
  side.className = "nav-side";
  grid.append(scene, side);
  root.append(head, grid);

  const savedZoom = Number(localStorage.getItem(ZOOM_KEY));
  const map = createMap(scene, {
    zoom: savedZoom > 0 ? savedZoom : DEFAULT_ZOOM_M,
    onZoomChange: (m) => localStorage.setItem(ZOOM_KEY, String(m)),
    layers: { scan: true, costmap: true, trail: true },
  });

  for (const { key, label } of LAYERS) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "layer-chip is-on";
    chip.textContent = label;
    chip.addEventListener("click", () => {
      const on = !chip.classList.contains("is-on");
      chip.classList.toggle("is-on", on);
      map.setLayer(key, on);
    });
    chips.appendChild(chip);
  }

  // Numeric readouts first, then the rolling plots beneath them: the numbers
  // answer "what is it now", the plots "how did it get here". Each gets its own
  // container so neither module's teardown can clear the other's DOM.
  const readoutHost = document.createElement("div");
  const plotHost = document.createElement("div");
  side.append(readoutHost, plotHost);
  const panels = createNavPanels(readoutHost);
  const plots = createNavPlots(plotHost);

  return {
    destroy() {
      plots.destroy();
      panels.destroy();
      map.destroy();
      root.innerHTML = "";
    },
  };
}
