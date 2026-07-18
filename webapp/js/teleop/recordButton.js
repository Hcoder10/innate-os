// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Bag-recording toggle — right-rail button driving the recording_manager
// node's /recording/start|stop services (full-fidelity rosbag capture for 3D
// reconstruction). The status topic is the source of truth: the button only
// reflects what the robot reports, so a recording started by another client
// (or one still running from a previous page load) shows up and can be
// stopped from here. While recording, a readout below the button ticks the
// elapsed time; the bag size rides the tooltip.

import { RECORDING_START_SERVICE, RECORDING_STOP_SERVICE, RECORDING_STATUS_TOPIC } from "../constants.js";

// Stop must outlive the recorder's bag-finalize wait (~20s server-side).
const STOP_TIMEOUT_MS = 30000;
const START_TIMEOUT_MS = 10000;

/**
 * @param {HTMLElement} parent
 * @param {import("../rosClient.js").RosClient} rosClient
 * @returns {{ destroy: () => void }}
 */
export function createRecordButton(parent, rosClient) {
  const wrap = document.createElement("div");
  wrap.className = "record-wrap";

  const button = document.createElement("button");
  button.type = "button";
  button.className = "icon-toggle record-toggle";
  button.innerHTML =
    '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">' +
    '<circle class="rec-ring" cx="12" cy="12" r="8.5" fill="none" stroke="currentColor" stroke-width="1.5"/>' +
    '<circle class="rec-dot" cx="12" cy="12" r="4.5" fill="currentColor"/></svg>';
  button.setAttribute("aria-label", "Record rosbag");

  const readout = document.createElement("span");
  readout.className = "record-readout mono";
  readout.hidden = true;

  wrap.append(button, readout);
  parent.appendChild(wrap);

  let recording = false;
  /** @type {number | null} epoch ms, from the robot's started_at */
  let startedAtMs = null;
  let sizeBytes = 0;
  let pending = false; // a start/stop call is in flight
  let statusSeen = false; // no recorder node -> button stays disabled
  /** @type {string | null} bag path of the active recording (from status) */
  let currentPath = null;
  /** @type {string | null} bag path of the last recording saved this session */
  let lastSavedPath = null;

  /** @param {number} bytes */
  function formatSize(bytes) {
    if (bytes >= 1 << 30) return `${(bytes / (1 << 30)).toFixed(1)} GB`;
    return `${Math.round(bytes / (1 << 20))} MB`;
  }

  /**
   * The robot reports absolute paths; the part from data/ on is what an
   * operator can actually go find.
   * @param {string} path
   */
  function shortPath(path) {
    const i = path.indexOf("data/recordings");
    return i >= 0 ? path.slice(i) : path;
  }

  function render() {
    button.classList.toggle("recording", recording);
    button.disabled = pending || !statusSeen || rosClient.state !== "connected";
    button.setAttribute("aria-pressed", String(recording));
    button.title = recording
      ? `Recording to ${currentPath ? shortPath(currentPath) : "data/recordings/"} (${formatSize(sizeBytes)}) — click to stop & save`
      : !statusSeen
        ? "Recorder not available"
        : lastSavedPath
          ? `Record cameras + nav data — last recording saved to ${shortPath(lastSavedPath)}`
          : "Record cameras + nav data to data/recordings/ on the robot";

    readout.hidden = !recording;
    if (recording && startedAtMs !== null) {
      const s = Math.max(0, Math.floor((Date.now() - startedAtMs) / 1000));
      readout.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    }
  }

  button.addEventListener("click", () => {
    if (pending) return;
    pending = true;
    render();
    const call = recording
      ? rosClient.callService(RECORDING_STOP_SERVICE, {}, STOP_TIMEOUT_MS)
      : rosClient.callService(RECORDING_START_SERVICE, {}, START_TIMEOUT_MS);
    call
      .then((res) => {
        if (res?.success === false) console.warn("[record]", res?.message);
        // Success flips `recording` via the next status heartbeat, but reflect
        // it immediately so the button doesn't lag the click by up to 2s.
        else recording = !recording;
        if (recording && startedAtMs === null) startedAtMs = Date.now();
      })
      .catch((err) => console.warn("[record]", err?.message || err))
      .finally(() => {
        pending = false;
        render();
      });
  });

  const unsubStatus = rosClient.subscribe(
    RECORDING_STATUS_TOPIC,
    (payload) => {
      if (typeof payload?.data !== "string") return;
      let s;
      try {
        s = JSON.parse(payload.data);
      } catch {
        return;
      }
      statusSeen = true;
      recording = s?.recording === true;
      startedAtMs = typeof s?.started_at === "number" ? s.started_at * 1000 : null;
      sizeBytes = typeof s?.size_bytes === "number" ? s.size_bytes : 0;
      if (recording) {
        currentPath = typeof s?.path === "string" ? s.path : null;
      } else {
        // recording -> idle transition: the bag that was live is now on disk.
        if (currentPath) lastSavedPath = currentPath;
        currentPath = null;
      }
      render();
    },
    undefined,
    "std_msgs/msg/String",
  );
  const unsubState = rosClient.onStateChange(() => {
    if (rosClient.state !== "connected") statusSeen = false;
    render();
  });

  // Tick the elapsed readout between the ~2s status heartbeats.
  const ticker = setInterval(() => {
    if (recording) render();
  }, 1000);

  render();

  return {
    destroy() {
      unsubStatus();
      unsubState();
      clearInterval(ticker);
      wrap.remove();
    },
  };
}
