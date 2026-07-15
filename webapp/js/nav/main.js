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
import { createNavMaps } from "./maps.js";
import { createDriveKit } from "./driveKit.js";

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

  /** @type {Map<string, HTMLButtonElement>} */
  const chipEls = new Map();
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
    chipEls.set(key, chip);
    chips.appendChild(chip);
  }

  /** @param {string} key @param {boolean} on */
  function forceLayer(key, on) {
    const chip = chipEls.get(key);
    if (chip) chip.classList.toggle("is-on", on);
    map.setLayer(/** @type {import("../map/mapWidget.js").LayerName} */ (key), on);
  }

  // Mapping mode reshapes the whole page: the scene shows only the growing
  // map + live scan (costmap belongs to the previous map, and the layer
  // chips lock so the view can't drift mid-recording), the widget swaps to
  // /mapping_pose, and the teleop drive kit (main camera PiP, joystick,
  // WASD, head tilt) mounts over the scene — you drive to build the map.
  /** @type {Record<string, boolean> | null} chip states to restore after mapping */
  let savedLayers = null;
  /** @type {{ destroy: () => void } | null} */
  let driveKit = null;
  let kitGen = 0; // guards the async mount against a fast mapping exit

  /** @param {boolean} mapping */
  function onMappingChange(mapping) {
    map.setMappingMode(mapping);
    const gen = ++kitGen;
    if (mapping) {
      savedLayers = {};
      for (const [key, chip] of chipEls) {
        savedLayers[key] = chip.classList.contains("is-on");
        chip.disabled = true;
      }
      forceLayer("scan", true);
      forceLayer("costmap", false);
      forceLayer("trail", false);
      createDriveKit(scene).then((kit) => {
        if (gen !== kitGen) kit.destroy(); // mapping ended while mounting
        else driveKit = kit;
      });
    } else {
      driveKit?.destroy();
      driveKit = null;
      for (const [key, chip] of chipEls) {
        chip.disabled = false;
        if (savedLayers) forceLayer(key, savedLayers[key]);
      }
      savedLayers = null;
    }
  }

  // Maps (the interactive panel) on top, then numeric readouts, then the
  // rolling plots: the numbers answer "what is it now", the plots "how did it
  // get here". Each gets its own container so no module's teardown can clear
  // another's DOM.
  const mapsHost = document.createElement("div");
  const readoutHost = document.createElement("div");
  const plotHost = document.createElement("div");
  side.append(mapsHost, readoutHost, plotHost);
  const mapsPanel = createNavMaps(mapsHost, scene, { onMappingChange });
  const panels = createNavPanels(readoutHost);
  const plots = createNavPlots(plotHost);

  return {
    destroy() {
      kitGen++; // cancel any in-flight drive-kit mount
      driveKit?.destroy();
      plots.destroy();
      panels.destroy();
      mapsPanel.destroy();
      map.destroy();
      root.innerHTML = "";
    },
  };
}
