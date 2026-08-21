// @ts-check
// Agent page entry — the autonomous-control room. Same full-bleed live feed as
// teleop (WebRTC video + camera/map PiP + telemetry), but the right edge hosts
// one liquid-glass Agent panel: directive selection, a Start/Stop toggle, the
// agent's live thinking traces + active skill + chat, and a message composer.
//
// The page has two stages behind that one panel: the live camera view, and the
// Brain monitor (the agent loop instrumented turn by turn) which flips in over
// it via a stage-level inspector button — controls and chat stay docked in both.
// The monitor is built on first open and kept until the page unmounts, so its
// turn history survives flipping back and forth. /brain deep-links here with
// the monitor open.
//
// Connect/disconnect lifecycle and optimistic mount mirror teleop (see
// pageMount.js): the view builds immediately and panels fill in once the socket
// is up. The centered hold-to-talk control is the sim's voice input; robot
// speech comes back through the shell's ttsAudio.

import { ros } from "../rosClient.js";
import { mountPage } from "../pageMount.js";
import { getConfig } from "../config.js";
import { robotSessionFactory } from "../robotSession.js";
import { createVideoStage } from "../teleop/videoStage.js";
import { createTelemetry } from "../teleop/telemetry.js";
import { createCameraSwitch } from "../teleop/cameraSwitch.js";
import { sharedAgentState } from "../teleop/agentState.js";
import { createAgentPanel } from "./agentPanel.js";
import { createChallengePanel } from "./challengePanel.js";
import { createAgentMicControl } from "./agentMicControl.js";
import { createSkillCardPreviewControl } from "./skillPreviewControl.js";
import { createTelemetryPreviewControl } from "../teleop/telemetryPreviewControl.js";
import { createAgentThemeControl } from "./themeControl.js";

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
const MIN_AGENT_VIEW_WIDTH = 1281;

/** @param {HTMLElement} stage */
export function mount(stage) {
  const className = config.simControls
    ? "cockpit agent-cockpit agent-sim"
    : "cockpit agent-cockpit";
  return mountPage(stage, className, buildAgentView);
}

/**
 * @param {HTMLElement} root
 * @returns {{ destroy: () => void }}
 */
function buildAgentView(root) {
  const session = createSession();
  const widthGuard = createWidthGuard(
    root,
    config.simControls ? "Simulator" : "Camera view",
  );

  const feedFrame = document.createElement("div");
  feedFrame.className = "agent-feed-frame";
  root.append(feedFrame);
  const videoStage = createStage
    ? createStage(feedFrame, session)
    : createVideoStage(feedFrame, session);
  const feedSurface = feedFrame.querySelector(".video-stage");
  const sceneSetup = feedFrame.querySelector(".sim-debug-stack");
  if (sceneSetup) root.append(sceneSetup);
  const feedDebug =
    config.simControls && feedSurface instanceof HTMLElement
      ? createFeedDebugOverlay(root, feedFrame, feedSurface)
      : null;

  const cornerStack = document.createElement("div");
  cornerStack.className = "overlay-stack-top-left";
  root.append(cornerStack);
  const agentState = sharedAgentState();

  const cameraSwitch = createCameraSwitch(root, session, ros, {
    storeKey: "innate.cameras.agent",
    stripParent: cornerStack,
  });
  const telemetryOverlay = document.createElement("div");
  telemetryOverlay.className = "overlay telemetry-overlay agent-telemetry-overlay";
  root.append(telemetryOverlay);

  // The Brain monitor's layer sits between the camera overlays and the panel
  // (DOM order + z-index): opening it covers the stage but never the controls.
  const brainLayer = document.createElement("div");
  brainLayer.className = "agent-brain brain-page";
  brainLayer.hidden = true;
  root.append(brainLayer);

  const stageViewToggle = document.createElement("button");
  stageViewToggle.type = "button";
  stageViewToggle.className = "agent-stage-view-toggle";
  stageViewToggle.innerHTML =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 12h4l2-5 4 10 2-5h6"/></svg><span></span>';
  const stageViewLabel = /** @type {HTMLElement} */ (stageViewToggle.querySelector("span"));
  stageViewToggle.addEventListener("click", () => setView(view === "live" ? "brain" : "live"));
  const themeControl = createAgentThemeControl(root);
  root.append(themeControl.element, stageViewToggle);

  /** @param {"live" | "brain"} next */
  function renderStageView(next) {
    const brain = next === "brain";
    stageViewToggle.classList.toggle("active", brain);
    stageViewToggle.setAttribute("aria-pressed", String(brain));
    stageViewToggle.setAttribute("aria-label", brain ? "Back to live camera" : "Inspect brain activity");
    stageViewToggle.title = brain
      ? "Return to the robot's live camera"
      : "Inspect model frames, tools, latency, and turn history";
    stageViewLabel.textContent = brain ? "Back to Live" : "Inspect Brain";
  }
  renderStageView("live");

  /** @type {{ destroy: () => void, setVisible: (visible: boolean) => void } | null} */
  let monitor = null;
  let monitorLoading = false;
  let unmounted = false;
  /** @type {"live" | "brain"} */
  let view = "live";
  /** @param {"live" | "brain"} next */
  function setView(next) {
    if (next === view) return;
    view = next;
    if (next === "brain" && !monitor && !monitorLoading) {
      // The monitor is its own sizeable module, fetched on first open so Agent
      // mounts that never look inside don't pay for it. Kept once built (its
      // turn history survives flips); hidden, it pauses its animation loop and
      // camera fallback via setVisible.
      monitorLoading = true;
      void import("../brain/main.js")
        .then((m) => {
          if (unmounted) return;
          monitor = m.createBrainMonitor(brainLayer, { onRequestClose: () => setView("live") });
          monitor.setVisible(view === "brain");
        })
        .catch(() => {
          monitorLoading = false; // a failed fetch can retry on the next flip
        });
    }
    monitor?.setVisible(next === "brain");
    brainLayer.hidden = next !== "brain";
    root.classList.toggle("brain-open", next === "brain");
    micControl?.setEnabled(next === "live");
    renderStageView(next);
  }

  /** @type {ReturnType<typeof createAgentMicControl> | null} */
  let micControl = null;
  const panel = createAgentPanel(root, ros, agentState, {
    enableMic: Boolean(config.simControls),
    onMicState: (state) => {
      micControl?.setCaptureState(state);
      micControl?.setAudioFeedback({
        level: state.on ? state.level : 0,
        waveform: state.waveform,
      });
    },
  });
  const simSession = /** @type {any} */ (session);
  const challengePanel =
    typeof simSession.onChallenge === "function" ? createChallengePanel(root, simSession) : null;
  const previewStack = config.simControls ? document.createElement("div") : null;
  if (previewStack) {
    previewStack.className = "agent-preview-stack";
    feedFrame.append(previewStack);
  }
  const isSceneSurface = (/** @type {EventTarget | null} */ target) =>
    target instanceof Element &&
    (target.matches(".video-stage > canvas, .video-stage > video") || target.classList.contains("video-stage"));
  const onScenePointerDown = (/** @type {PointerEvent} */ event) => {
    if (!event.isPrimary || event.button !== 0 || !isSceneSurface(event.target)) return;
    challengePanel?.dismiss();
    root.dispatchEvent(new Event("innate:stage-background-click"));
  };
  root.addEventListener("pointerdown", onScenePointerDown);
  const telemetry = createTelemetry(telemetryOverlay, ros, { showBattery: !config.simControls });
  if (config.simControls) {
    micControl = createAgentMicControl(panel.micMount, {
      startListening: panel.startMic,
      stopListening: panel.stopMic,
    });
  }
  const telemetryPreview = previewStack
    ? createTelemetryPreviewControl(previewStack, telemetry.preview)
    : null;
  const skillPreview = previewStack
    ? createSkillCardPreviewControl(previewStack, panel.previewSkill)
    : null;

  const parts = [
    videoStage,
    widthGuard,
    ...(feedDebug ? [feedDebug] : []),
    ...(challengePanel ? [challengePanel] : []),
    ...(telemetryPreview ? [telemetryPreview] : []),
    telemetry,
    // Square, always-live camera tiles (own prefs key so teleop's defaults stay put).
    cameraSwitch,
    ...(micControl ? [micControl] : []),
    ...(skillPreview ? [skillPreview] : []),
    ...(previewStack ? [{ destroy: () => previewStack.remove() }] : []),
    panel,
    {
      destroy: () => {
        root.removeEventListener("pointerdown", onScenePointerDown);
      },
    },
    themeControl,
    { destroy: () => stageViewToggle.remove() },
    {
      destroy: () => {
        unmounted = true; // a monitor import still in flight must not build into the dead layer
        monitor?.destroy();
      },
    },
  ];

  session.start();

  const entryPath = location.pathname.replace(/\/+$/, "");
  if (entryPath === "/brain") setView("brain");
  if (entryPath === "/brain" || entryPath === "/agent") {
    history.replaceState({}, "", "/" + location.search + location.hash);
  }

  return {
    destroy() {
      for (const part of parts) part.destroy();
      session.destroy();
      root.innerHTML = "";
    },
  };
}

/**
 * @param {HTMLElement} root
 * @param {string} viewName
 * @returns {{ destroy: () => void }}
 */
function createWidthGuard(root, viewName) {
  const guard = document.createElement("aside");
  guard.className = "agent-width-guard";
  guard.setAttribute("aria-labelledby", "agent-width-guard-title");
  guard.innerHTML = `
    <div class="agent-width-guard-card">
      <h2 id="agent-width-guard-title" class="agent-width-guard-title">${viewName} unavailable</h2>
      <p class="agent-width-guard-message">Widen your browser to continue.</p>
      <div class="agent-width-meter">
        <div class="agent-width-meter-labels">
          <span>Current <output class="agent-width-current"></output></span>
          <span>Minimum <output>${MIN_AGENT_VIEW_WIDTH} px</output></span>
        </div>
        <div
          class="agent-width-meter-track"
          role="progressbar"
          aria-label="Browser width"
          aria-valuemin="0"
          aria-valuemax="${MIN_AGENT_VIEW_WIDTH}"
        ><span></span></div>
      </div>
    </div>
  `;

  const current = /** @type {HTMLOutputElement} */ (
    guard.querySelector(".agent-width-current")
  );
  const meter = /** @type {HTMLElement} */ (
    guard.querySelector(".agent-width-meter-track")
  );
  const render = () => {
    const width = window.innerWidth;
    const progress = Math.min(width / MIN_AGENT_VIEW_WIDTH, 1);
    current.textContent = `${width} px`;
    meter.setAttribute(
      "aria-valuenow",
      String(Math.min(width, MIN_AGENT_VIEW_WIDTH)),
    );
    guard.style.setProperty("--agent-width-progress", String(progress));
  };

  window.addEventListener("resize", render);
  render();
  root.append(guard);

  return {
    destroy() {
      window.removeEventListener("resize", render);
      guard.remove();
    },
  };
}

/**
 * @param {HTMLElement} root
 * @param {HTMLElement} frame
 * @param {HTMLElement} source
 * @returns {{ destroy: () => void }}
 */
function createFeedDebugOverlay(root, frame, source) {
  const readout = document.createElement("aside");
  readout.className = "agent-feed-debug mono";
  readout.setAttribute("aria-label", "Feed dimensions");

  const browserLine = document.createElement("span");
  const viewportLine = document.createElement("span");
  const sourceLine = document.createElement("span");
  readout.append(browserLine, viewportLine, sourceLine);
  root.append(readout);

  /** @param {DOMRect} rect @returns {string} */
  function dimensions(rect) {
    const width = Math.round(rect.width);
    const height = Math.round(rect.height);
    if (!width || !height) return "hidden";
    const ratio = rect.width / rect.height;
    const name =
      Math.abs(ratio - 4 / 3) < 0.01
        ? "4:3"
        : Math.abs(ratio - 16 / 9) < 0.01
          ? "16:9"
          : `${ratio.toFixed(3)}:1`;
    return `${width}×${height} · ${name} (${ratio.toFixed(3)})`;
  }

  function render() {
    const sourceRect = source.getBoundingClientRect();
    const viewportRect = root.classList.contains("agent-sim")
      ? frame.getBoundingClientRect()
      : sourceRect;
    browserLine.textContent = `browser ${window.innerWidth}×${window.innerHeight}`;
    viewportLine.textContent = `viewport ${dimensions(viewportRect)}`;
    sourceLine.textContent = `render ${dimensions(sourceRect)}`;
  }

  const observer = new ResizeObserver(render);
  observer.observe(root);
  observer.observe(source);
  window.addEventListener("resize", render);
  render();

  return {
    destroy() {
      observer.disconnect();
      window.removeEventListener("resize", render);
      readout.remove();
    },
  };
}
