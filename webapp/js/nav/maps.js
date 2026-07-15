// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Maps panel + mapping session for the Nav page. Ports the mobile app's
// mapping workflow (innate-controller-app MapDataContext/MappingTab/
// RecordMapScreen) onto the webapp:
//
// - list maps (/nav/available_maps), switch the active one, delete
// - "New map" flips the robot into mapping mode (/nav/change_mode); while
//   mapping, a banner over the scene carries the record hint plus
//   Save-as/Discard, and the map widget swaps to /mapping_pose
// - map-free toggle (navigation without a map)
//
// Mode is driven by the /nav/current_mode topic, not by what this client last
// requested — a mapping session started from the mobile app shows up here
// with the same Save/Discard controls, and vice versa. UX guards mirror the
// mobile app: confirm before switch/delete/discard (destructive to
// localization or files), never delete the last remaining map.

import { ros } from "../rosClient.js";
import {
  NAV_AVAILABLE_MAPS_TOPIC,
  NAV_CHANGE_MAP_SERVICE,
  NAV_CHANGE_MODE_SERVICE,
  NAV_CURRENT_MAP_TOPIC,
  NAV_CURRENT_MODE_TOPIC,
  NAV_DELETE_MAP_SERVICE,
  NAV_SAVE_MAP_SERVICE,
} from "../constants.js";

// Mode changes tear down and bring up whole Nav2 lifecycle stacks.
const MODE_CHANGE_TIMEOUT_MS = 60_000;
// Matches mode_manager's save_map validation (".yaml" is appended there).
const MAP_NAME_RE = /^[A-Za-z0-9_-]+$/;

/**
 * @param {HTMLElement} host sidebar container for the panel.
 * @param {HTMLElement} scene the map stage — the mapping banner overlays it.
 * @param {{ onMappingChange: (on: boolean) => void }} opts fires on every
 *   mapping-mode edge (from the /nav/current_mode topic, so sessions started
 *   by other clients fire it too) — the page mounts/unmounts the drive kit
 *   and swaps the widget's pose source there.
 * @returns {{ destroy: () => void }}
 */
export function createNavMaps(host, scene, opts) {
  // ---- panel skeleton ------------------------------------------------------
  const section = document.createElement("section");
  section.className = "nav-panel";
  const head = document.createElement("div");
  head.className = "telemetry-head";
  const label = document.createElement("p");
  label.className = "microlabel";
  label.textContent = "Maps";
  const newBtn = document.createElement("button");
  newBtn.type = "button";
  newBtn.className = "maps-new";
  newBtn.textContent = "+ New map";
  head.append(label, newBtn);

  const list = document.createElement("div");
  list.className = "maps-list";

  const mapfreeRow = document.createElement("label");
  mapfreeRow.className = "nav-row maps-mapfree";
  const mapfreeLabel = document.createElement("span");
  mapfreeLabel.className = "nav-row-label";
  mapfreeLabel.textContent = "map-free mode";
  const mapfreeToggle = document.createElement("input");
  mapfreeToggle.type = "checkbox";
  mapfreeRow.append(mapfreeLabel, mapfreeToggle);

  // Outcome of the last action (errors especially — service failures carry
  // useful messages like "A mode or map change is in progress").
  const status = document.createElement("div");
  status.className = "maps-status mono";
  status.hidden = true;

  section.append(head, list, mapfreeRow, status);
  host.appendChild(section);

  // ---- mapping banner over the scene --------------------------------------
  const banner = document.createElement("div");
  banner.className = "mapping-banner";
  banner.hidden = true;
  const bannerText = document.createElement("span");
  bannerText.className = "mapping-banner-text";
  bannerText.textContent = "Recording map — drive slowly to cover the space";
  const nameInput = document.createElement("input");
  nameInput.type = "text";
  nameInput.className = "mapping-name mono";
  nameInput.placeholder = "map name";
  nameInput.hidden = true;
  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.className = "mapping-btn";
  saveBtn.textContent = "Save as…";
  const discardBtn = document.createElement("button");
  discardBtn.type = "button";
  discardBtn.className = "mapping-btn danger";
  discardBtn.textContent = "Discard";
  banner.append(bannerText, nameInput, saveBtn, discardBtn);
  scene.appendChild(banner);

  // ---- state ---------------------------------------------------------------
  /** @type {string[]} */
  let maps = [];
  let currentMap = "";
  let mode = "";
  let busy = false; // one service call at a time; mode changes take a while
  let destroyed = false;

  /** @param {"ok" | "fail" | "hint"} kind @param {string} text */
  function setStatus(kind, text) {
    status.hidden = false;
    status.dataset.kind = kind;
    status.textContent = text;
  }

  /**
   * Run one mode_manager service call with the shared busy state.
   * @param {string} service @param {object} args @param {string} doing
   * @returns {Promise<boolean>} success
   */
  async function call(service, args, doing) {
    if (busy) return false;
    busy = true;
    setStatus("hint", `${doing}…`);
    renderAll();
    try {
      const res = await ros.callService(service, args, MODE_CHANGE_TIMEOUT_MS);
      if (destroyed) return false;
      if (res?.success) {
        status.hidden = true;
        return true;
      }
      setStatus("fail", res?.message || `${doing} failed`);
      return false;
    } catch (err) {
      if (!destroyed) setStatus("fail", `${doing} failed — ${err instanceof Error ? err.message : String(err)}`);
      return false;
    } finally {
      busy = false;
      if (!destroyed) renderAll();
    }
  }

  // ---- rendering -----------------------------------------------------------
  function renderList() {
    list.innerHTML = "";
    if (maps.length === 0) {
      const empty = document.createElement("div");
      empty.className = "maps-empty";
      empty.textContent = "No maps yet — record one to navigate.";
      list.appendChild(empty);
      return;
    }
    for (const name of maps) {
      const row = document.createElement("div");
      row.className = "maps-row";
      const active = name === currentMap;
      if (active) row.classList.add("is-active");

      const pick = document.createElement("button");
      pick.type = "button";
      pick.className = "maps-pick";
      pick.disabled = busy || mode === "mapping" || active;
      pick.title = active ? "Active map" : `Switch to ${name}`;
      const dot = document.createElement("span");
      dot.className = "maps-dot";
      const text = document.createElement("span");
      text.className = "maps-name mono";
      text.textContent = name;
      pick.append(dot, text);
      pick.addEventListener("click", async () => {
        // Switching maps drops localization on the old one — same confirm the
        // mobile app requires.
        if (!window.confirm(`Switch to ${name}? The robot will need to re-localize.`)) return;
        await call(NAV_CHANGE_MAP_SERVICE, { map_name: name }, `Switching to ${name}`);
      });

      const del = document.createElement("button");
      del.type = "button";
      del.className = "maps-delete";
      del.innerHTML =
        '<svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"><path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.7 8.5h5.6l.7-8.5"/></svg>';
      const last = maps.length === 1;
      del.disabled = busy || mode === "mapping" || last;
      del.title = last ? "Cannot delete the last remaining map" : `Delete ${name}`;
      del.addEventListener("click", async () => {
        if (!window.confirm(`Delete ${name}? This cannot be undone.`)) return;
        await call(NAV_DELETE_MAP_SERVICE, { map_name: name }, `Deleting ${name}`);
      });

      row.append(pick, del);
      list.appendChild(row);
    }
  }

  function renderAll() {
    const mapping = mode === "mapping";
    banner.hidden = !mapping;
    if (!mapping) nameInput.hidden = true;
    newBtn.disabled = busy || mapping;
    saveBtn.disabled = busy;
    discardBtn.disabled = busy;
    nameInput.disabled = busy;
    mapfreeToggle.checked = mode === "mapfree";
    mapfreeToggle.disabled = busy || mapping;
    renderList();
  }

  // ---- actions -------------------------------------------------------------
  newBtn.addEventListener("click", async () => {
    const msg =
      "Start recording a new map?\n\n" +
      "The robot leaves navigation mode and you drive it around to cover the space. Current navigation stops.";
    if (!window.confirm(msg)) return;
    await call(NAV_CHANGE_MODE_SERVICE, { mode: "mapping" }, "Starting mapping");
    // No local session flag: the banner follows /nav/current_mode.
  });

  saveBtn.addEventListener("click", async () => {
    // First press reveals the name field; second press (or Enter) saves.
    if (nameInput.hidden) {
      nameInput.hidden = false;
      nameInput.focus();
      return;
    }
    const name = nameInput.value.trim();
    if (!MAP_NAME_RE.test(name)) {
      setStatus("fail", "Name must be letters, digits, _ or - (no spaces)");
      nameInput.focus();
      return;
    }
    const overwrite = maps.includes(`${name}.yaml`);
    if (overwrite && !window.confirm(`${name}.yaml already exists. Overwrite it?`)) return;
    // The mobile app's sequence: save, then back to navigation, then make the
    // new map active. Each step reports its own failure.
    if (!(await call(NAV_SAVE_MAP_SERVICE, { map_name: name, overwrite }, `Saving ${name}`))) return;
    nameInput.hidden = true;
    nameInput.value = "";
    if (!(await call(NAV_CHANGE_MODE_SERVICE, { mode: "navigation" }, "Returning to navigation"))) return;
    if (await call(NAV_CHANGE_MAP_SERVICE, { map_name: `${name}.yaml` }, `Loading ${name}`)) {
      setStatus("ok", `Saved ${name}.yaml — use Locate if the robot isn't where the map thinks`);
    }
  });

  discardBtn.addEventListener("click", async () => {
    if (maps.length === 0) {
      // Nowhere to go back to — mirror the mobile app's "you'll need a map
      // before the robot can navigate" framing, but map-free is a valid out.
      if (!window.confirm("Discard this recording and switch to map-free mode? The robot will have no map."))
        return;
      await call(NAV_CHANGE_MODE_SERVICE, { mode: "mapfree" }, "Switching to map-free");
      return;
    }
    if (!window.confirm("Discard this recording? The map progress is lost.")) return;
    await call(NAV_CHANGE_MODE_SERVICE, { mode: "navigation" }, "Returning to navigation");
  });

  nameInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") saveBtn.click();
    if (e.key === "Escape") nameInput.hidden = true;
  });

  mapfreeToggle.addEventListener("change", async () => {
    const toMapfree = mapfreeToggle.checked;
    if (!toMapfree && maps.length === 0) {
      setStatus("fail", "No saved maps — record one first");
      mapfreeToggle.checked = true;
      return;
    }
    // mode_manager persists the last map, so returning to navigation reloads
    // it server-side — no client bookkeeping needed.
    const ok = await call(
      NAV_CHANGE_MODE_SERVICE,
      { mode: toMapfree ? "mapfree" : "navigation" },
      toMapfree ? "Switching to map-free" : "Returning to navigation",
    );
    if (!ok) mapfreeToggle.checked = !toMapfree;
  });

  // ---- topic subscriptions -------------------------------------------------
  const unsubs = [
    ros.subscribe(NAV_AVAILABLE_MAPS_TOPIC, (msg) => {
      if (typeof msg?.data !== "string") return;
      try {
        const parsed = JSON.parse(msg.data);
        if (Array.isArray(parsed?.available_maps)) {
          maps = parsed.available_maps;
          renderAll();
        }
      } catch {
        // torn payload — keep the last good roster
      }
    }, 0, "std_msgs/msg/String"),
    ros.subscribe(NAV_CURRENT_MAP_TOPIC, (msg) => {
      if (typeof msg?.data !== "string" || msg.data === currentMap) return;
      currentMap = msg.data;
      renderAll();
    }, 0, "std_msgs/msg/String"),
    ros.subscribe(NAV_CURRENT_MODE_TOPIC, (msg) => {
      if (typeof msg?.data !== "string" || !msg.data || msg.data === mode) return;
      const wasMapping = mode === "mapping";
      mode = msg.data;
      const isMapping = mode === "mapping";
      if (isMapping !== wasMapping) opts.onMappingChange(isMapping);
      renderAll();
    }, 0, "std_msgs/msg/String"),
  ];

  renderAll();

  return {
    destroy() {
      destroyed = true;
      for (const unsub of unsubs) unsub();
      section.remove();
      banner.remove();
    },
  };
}
