// @ts-check
// Agent page entry — the autonomous-control room. Same full-bleed live feed as
// teleop (WebRTC video + camera/map PiP + telemetry), but the right edge hosts
// one liquid-glass Agent panel: directive selection, a Start/Stop toggle, the
// agent's live thinking traces + active skill + chat, and a message composer.
//
// Connect/disconnect lifecycle and optimistic mount mirror teleop (see
// pageMount.js): the view builds immediately and panels fill in once the socket
// is up. No mic toggle here — robot speech already plays in-browser via the
// shell's ttsAudio.

import { ros } from "../rosClient.js";
import { mountPage } from "../pageMount.js";
import { WebRtcSession } from "../webrtcSession.js";
import { robotSessionFactory } from "../robotSession.js";
import { createVideoStage } from "../teleop/videoStage.js";
import { createTelemetry } from "../teleop/telemetry.js";
import { createCameraSwitch } from "../teleop/cameraSwitch.js";
import { createAgentState } from "../teleop/agentState.js";
import { createAgentPanel } from "./agentPanel.js";

// Runtime feature flags (config.json, served static), same as teleop. simControls
// marks a sim deployment — used here to drop the (absent) battery readout. Fetched
// once on first import (the router's dynamic import awaits it) so the view reads
// it synchronously.
/** @type {any} */
const config = await fetch("/config.json", { cache: "no-store" })
  .then((r) => (r.ok ? r.json() : {}))
  .catch(() => ({}));

// Resolved once at import time (the router's dynamic import awaits it):
// WebRTC for real robots, the Three.js SimSession in simulation (see
// robotSession.js).
const { createSession, createStage } = await robotSessionFactory();

/** @param {HTMLElement} stage */
export function mount(stage) {
  return mountPage(stage, "cockpit agent-cockpit", buildAgentView);
}

/**
 * @param {HTMLElement} root
 * @returns {{ destroy: () => void }}
 */
function buildAgentView(root) {
  const session = createSession();

  const videoStage = createStage ? createStage(root, session) : createVideoStage(root, session);

  const telemetryOverlay = overlay("overlay-top-left");
  root.append(telemetryOverlay);

  const agentState = createAgentState(ros);

  const parts = [
    videoStage,
    createTelemetry(telemetryOverlay, ros, { showBattery: !config.simControls }),
    // Square, always-live camera tiles (own prefs key so teleop's defaults stay put).
    createCameraSwitch(root, session, ros, { alwaysOn: true, storeKey: "innate.cameras.agent" }),
    createAgentPanel(root, ros, agentState),
    createActiveChip(root, agentState),
    { destroy: () => agentState.destroy() },
  ];

  session.start();

  return {
    destroy() {
      for (const part of parts) part.destroy();
      session.destroy();
      root.innerHTML = "";
    },
  };

  /** @param {string} className */
  function overlay(className) {
    const el = document.createElement("div");
    el.className = `overlay ${className}`;
    return el;
  }
}

/**
 * Bottom-left "AGENT ACTIVE" chip — shown only while the brain is running.
 * @param {HTMLElement} root
 * @param {ReturnType<import("../teleop/agentState.js").createAgentState>} agentState
 * @returns {{ destroy: () => void }}
 */
function createActiveChip(root, agentState) {
  const chip = document.createElement("div");
  chip.className = "agent-active-chip";
  chip.hidden = true;

  const dot = document.createElement("span");
  dot.className = "agent-active-dot";
  const label = document.createElement("div");
  label.className = "agent-active-text";
  const title = document.createElement("span");
  title.className = "agent-active-title";
  title.textContent = "Agent active";
  const sub = document.createElement("span");
  sub.className = "agent-active-sub";
  sub.textContent = "Autonomous execution in progress.";
  label.append(title, sub);
  chip.append(dot, label);
  root.append(chip);

  const unsub = agentState.subscribe((s) => {
    chip.hidden = !s.brainActive;
  });

  return {
    destroy() {
      unsub();
      chip.remove();
    },
  };
}
