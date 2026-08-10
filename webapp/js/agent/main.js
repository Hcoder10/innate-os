// @ts-check
// Agent page entry — the autonomous-control room. The Agent panel is pinned to
// the right edge in both of the page's views: directive selection, a Start/Stop
// toggle, the agent's live thinking traces + active skill + chat, and a message
// composer. Its header switches what fills the rest of the page:
//
//   Live  — the full-bleed feed reused from teleop (WebRTC video + camera/map
//           PiP + telemetry). The default.
//   Brain — the agent-loop monitor (brain/view.js, its own rail section until
//           it moved here): what the model saw, thought and called, turn by
//           turn. Deep-linked as /agent?view=brain.
//
// Keeping one panel across both is the point: you can start, stop and message
// the agent without leaving its internals, and the brain view drops the chat
// feed it used to carry because the panel next to it already is one.
//
// Connect/disconnect lifecycle and optimistic mount mirror teleop (see
// pageMount.js): the view builds immediately and panels fill in once the socket
// is up. No mic toggle here — robot speech already plays in-browser via the
// shell's ttsAudio.

import { ros } from "../rosClient.js";
import { mountPage } from "../pageMount.js";
import { getConfig } from "../config.js";
import { robotSessionFactory } from "../robotSession.js";
import { createBrainView } from "../brain/view.js";
import { createVideoStage } from "../teleop/videoStage.js";
import { createTelemetry } from "../teleop/telemetry.js";
import { createCameraSwitch } from "../teleop/cameraSwitch.js";
import { createAgentState } from "../teleop/agentState.js";
import { createAgentPanel } from "./agentPanel.js";
import { createChallengePanel } from "./challengePanel.js";

/** @typedef {"live" | "brain"} AgentView */

// Runtime feature flags (config.json, served static), same as teleop. simControls
// marks a sim deployment — used here to drop the (absent) battery readout. Fetched
// once on first import (the router's dynamic import awaits it) so the view reads
// it synchronously.
/** @type {any} */
const config = await getConfig();

// Resolved once at import time (the router's dynamic import awaits it):
// WebRTC for real robots, the Three.js SimSession in simulation (see
// robotSession.js).
const { createSession, createStage } = await robotSessionFactory();

/** @param {HTMLElement} stage */
export function mount(stage) {
  const params = new URLSearchParams(location.search);
  // ?demo runs the brain view off a scripted synthetic brain — there is no
  // robot behind it, so the socket (and its connect card) stands down.
  const demo = params.has("demo");
  const view = /** @type {AgentView} */ (demo || params.get("view") === "brain" ? "brain" : "live");
  return mountPage(stage, "cockpit agent-cockpit", (root) => buildAgentView(root, { demo, view }), { offline: demo });
}

/**
 * @param {HTMLElement} root
 * @param {{ demo: boolean, view: AgentView }} opts
 * @returns {{ destroy: () => void }}
 */
function buildAgentView(root, opts) {
  const session = createSession();

  const videoStage = createStage ? createStage(root, session) : createVideoStage(root, session);

  // Top-left column: the sim challenge panel (when the session supports it)
  // stacked above the telemetry card; on real robots it holds telemetry alone,
  // in the same corner position as before.
  const cornerStack = document.createElement("div");
  cornerStack.className = "overlay-stack-top-left";
  root.append(cornerStack);
  // Column order is DOM order: the challenge panel (sim only) first, then the
  // telemetry card under it. The stack does the positioning, so the card
  // carries no corner class of its own.
  const challengePanel = typeof session.onChallenge === "function" ? createChallengePanel(cornerStack, session) : null;
  const telemetryOverlay = document.createElement("div");
  telemetryOverlay.className = "overlay";
  cornerStack.append(telemetryOverlay);
  const agentState = createAgentState(ros);

  // The brain view is built on first open — it's the heavier half, and a
  // cockpit that never opens it should not pay for /brain/trace. Once built it
  // stays: toggling back to Live only suspends its feeds, so the turns and
  // latencies it collected are still there on the way back in.
  /** @type {ReturnType<typeof createBrainView> | null} */
  let brain = null;
  /** @type {AgentView} */
  let view = "live";

  const tabs = createViewTabs(opts.view, setView);

  const parts = [
    videoStage,
    ...(challengePanel ? [challengePanel] : []),
    createTelemetry(telemetryOverlay, ros, { showBattery: !config.simControls }),
    // Square, always-live camera tiles (own prefs key so teleop's defaults stay put).
    createCameraSwitch(root, session, ros, { storeKey: "innate.cameras.agent" }),
    createAgentPanel(root, ros, agentState, { headerExtra: tabs.el }),
    createActiveChip(root, agentState),
    { destroy: () => agentState.destroy() },
  ];

  /** @param {AgentView} next */
  function setView(next) {
    if (next === view) return;
    view = next;
    root.classList.toggle("brain-mode", next === "brain");
    if (next === "brain") {
      brain ??= createBrainView(root, { demo: opts.demo });
      brain.resume();
    } else {
      brain?.suspend();
    }
    tabs.select(next);
    // Shareable address, no navigation: the router owns history, and re-rendering
    // the route here would tear the video session down mid-switch.
    const url = new URL(location.href);
    if (next === "brain") url.searchParams.set("view", "brain");
    else url.searchParams.delete("view");
    history.replaceState({}, "", url);
    document.title = next === "brain" ? "Innate · Agent · Brain" : "Innate · Agent";
  }

  setView(opts.view);
  session.start();

  return {
    destroy() {
      for (const part of parts) part.destroy();
      brain?.destroy();
      session.destroy();
      root.innerHTML = "";
    },
  };
}

/**
 * The Live/Brain switch that lives in the Agent panel's header — the one piece
 * of chrome that stays put across both views.
 * @param {AgentView} initial
 * @param {(view: AgentView) => void} onSelect
 * @returns {{ el: HTMLElement, select: (view: AgentView) => void }}
 */
function createViewTabs(initial, onSelect) {
  /** @type {{ key: AgentView, label: string, title: string }[]} */
  const VIEWS = [
    { key: "live", label: "Live", title: "The live feed, cameras and telemetry" },
    { key: "brain", label: "Brain", title: "The agent loop: what the model saw, thought and called" },
  ];
  const el = document.createElement("div");
  el.className = "agent-views";
  el.setAttribute("role", "tablist");
  el.setAttribute("aria-label", "Agent view");

  /** @type {Map<AgentView, HTMLButtonElement>} */
  const buttons = new Map();
  for (const v of VIEWS) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "agent-view";
    btn.setAttribute("role", "tab");
    btn.title = v.title;
    btn.textContent = v.label;
    btn.addEventListener("click", () => onSelect(v.key));
    buttons.set(v.key, btn);
    el.append(btn);
  }

  /** @param {AgentView} view */
  function select(view) {
    for (const [key, btn] of buttons) {
      const on = key === view;
      btn.classList.toggle("on", on);
      btn.setAttribute("aria-selected", String(on));
    }
  }
  select(initial);

  return { el, select };
}

/**
 * Bottom-left "AGENT ACTIVE" chip — shown only while the brain is running.
 * @param {HTMLElement} root
 * @param {ReturnType<typeof import("../teleop/agentState.js").createAgentState>} agentState
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
