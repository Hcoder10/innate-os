// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Nav page entry — a Foxglove-style live view of everything navigation.
//
// Composition (one-directional: topics → store → views → store actions):
//   navStore.js        mode/maps/currentMap truth + the mode_manager actions
//   mapWidget (shared) the scene: grid, pose, route, scan/costmap/trail layers
//   mapsPanel.js       sidebar roster (switch/delete/map-free/new)
//   mappingSession.js  recording banner over the scene (finish → name → save)
//   panels.js          sensor readouts   plots.js  rolling strip charts
//   driveKit.js        teleop controls mounted only while mapping
//
// This module only composes: it owns the layout, the layer chips, the busy
// veil (mode/map changes block for tens of seconds — the page says so instead
// of going dead), and the one cross-cutting reaction — reshaping the page
// when mapping starts or ends, no matter which client started it.

import { mountPage } from "../pageMount.js";
import { createMap } from "../map/mapWidget.js";
import { createNavStore } from "./navStore.js";
import { createMapsPanel } from "./mapsPanel.js";
import { createMappingSession } from "./mappingSession.js";
import { createNavPanels } from "./panels.js";
import { createNavPlots } from "./plots.js";
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
  // ---- layout --------------------------------------------------------------
  const head = document.createElement("div");
  head.className = "page-head";
  const heading = document.createElement("h1");
  heading.className = "page-title";
  heading.textContent = "Nav";
  const chips = document.createElement("div");
  chips.className = "layer-chips";
  head.append(heading, chips);

  const grid = document.createElement("div");
  grid.className = "nav-grid";
  const scene = document.createElement("div");
  scene.className = "nav-scene";
  const side = document.createElement("aside");
  side.className = "nav-side";
  grid.append(scene, side);
  root.append(head, grid);

  // Blocking feedback while a mode/map change is in flight (they bring whole
  // Nav2 lifecycle stacks up and down — up to a minute).
  const veil = document.createElement("div");
  veil.className = "nav-veil";
  veil.hidden = true;
  const veilText = document.createElement("span");
  veilText.className = "nav-veil-text mono";
  veil.appendChild(veilText);
  scene.appendChild(veil);

  // ---- store + scene ---------------------------------------------------------
  const store = createNavStore();

  const savedZoom = Number(localStorage.getItem(ZOOM_KEY));
  const map = createMap(scene, {
    zoom: savedZoom > 0 ? savedZoom : DEFAULT_ZOOM_M,
    onZoomChange: (m) => localStorage.setItem(ZOOM_KEY, String(m)),
    layers: { scan: true, costmap: true, trail: true },
  });

  // ---- layer chips -----------------------------------------------------------
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

  // ---- mapping reaction --------------------------------------------------------
  // Mapping reshapes the page: the scene shows only the growing map + live
  // scan (the costmap belongs to the previous map; chips lock so the view
  // can't drift mid-recording), the widget swaps to /mapping_pose, and the
  // teleop drive kit mounts — you drive the robot to build the map.
  /** @type {Record<string, boolean> | null} chip states to restore after mapping */
  let savedLayers = null;
  /** @type {{ destroy: () => void } | null} */
  let driveKit = null;
  let kitGen = 0; // guards the async kit mount against a fast mapping exit

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

  let wasMapping = false;
  const unsubStore = store.onChange((s) => {
    veil.hidden = !s.busy;
    if (s.busy) veilText.textContent = `${s.busy}…`;
    const mapping = s.mode === "mapping";
    if (mapping !== wasMapping) {
      wasMapping = mapping;
      onMappingChange(mapping);
    }
  });

  // ---- sidebar -----------------------------------------------------------------
  // Maps (the interactive panel) on top, then numeric readouts, then the
  // rolling plots. Each in its own host so no module's teardown can clear
  // another's DOM.
  const mapsHost = document.createElement("div");
  const readoutHost = document.createElement("div");
  const plotHost = document.createElement("div");
  side.append(mapsHost, readoutHost, plotHost);

  const parts = [
    createMapsPanel(mapsHost, store),
    createMappingSession(scene, store),
    createNavPanels(readoutHost, store),
    createNavPlots(plotHost),
  ];

  return {
    destroy() {
      kitGen++; // cancel any in-flight drive-kit mount
      driveKit?.destroy();
      for (const part of parts) part.destroy();
      unsubStore();
      store.destroy();
      map.destroy();
      root.innerHTML = "";
    },
  };
}
