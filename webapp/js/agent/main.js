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
import { createAgentOnboarding } from "./onboarding.js";
import { createScriptedActionRunner } from "./onboardingWelcome.js";

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
  return mountPage(stage, "cockpit agent-cockpit", buildAgentView);
}

/**
 * @param {HTMLElement} root
 * @returns {{ destroy: () => void }}
 */
function buildAgentView(root) {
  const agentState = sharedAgentState();
  // Built before the onboarding engine, which needs to reach the stage channel
  // to put the robot back at spawn.
  const session = createSession();
  const scriptedActions = config.simControls ? createScriptedActionRunner(ros) : null;
  const onboarding = config.simControls && scriptedActions
    ? createAgentOnboarding(root, ros, {
        runner: scriptedActions,
        onHandoff: async () => {
          const { agents, currentDirective } = agentState.get();
          const preferred =
            agents.find((agent) => agent.id === "intro_agent")?.id
            || currentDirective
            || agents[0]?.id
            || "";
          if (preferred) await agentState.setDirective(preferred);
        },
        onResetWorld: () => {
          const sim = /** @type {any} */ (session);
          if (typeof sim.resetWorld !== "function") {
            // Older viewer bundle: rebuilt from sim/viewer, not served from the
            // working tree like the rest of this app.
            console.warn("[onboarding] stage has no resetWorld — rebuild sim/viewer (npm run build:lib)");
            return;
          }
          sim.resetWorld();
        },
      })
    : null;

  const videoStage = createStage ? createStage(root, session) : createVideoStage(root, session);
  const setOnboardingStep =
    "setOnboardingStep" in videoStage ? videoStage.setOnboardingStep : null;
  const stopOnboardingStageSync =
    onboarding && setOnboardingStep
      ? onboarding.onStep(setOnboardingStep)
      : null;

  // Camera views stay grouped at the top left.
  const cornerStack = document.createElement("div");
  cornerStack.className = "overlay-stack-top-left";
  root.append(cornerStack);
  const telemetryOverlay = document.createElement("div");
  telemetryOverlay.className = "overlay telemetry-overlay agent-telemetry-overlay";
  root.append(telemetryOverlay);

  const cameraSwitch = createCameraSwitch(root, session, ros, {
    storeKey: "innate.cameras.agent",
    stripParent: cornerStack,
  });

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
  root.append(stageViewToggle);

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
    ensureListening: async () => {
      if (!onboarding || onboarding.isComplete()) return false;
      return onboarding.ensureListening();
    },
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
  const isSceneSurface = (/** @type {EventTarget | null} */ target) =>
    target instanceof Element &&
    (target.matches(".video-stage > canvas, .video-stage > video") || target.classList.contains("video-stage"));
  const onScenePointerDown = (/** @type {PointerEvent} */ event) => {
    if (!event.isPrimary || event.button !== 0 || !isSceneSurface(event.target)) return;
    panel.dismissOverlays();
    challengePanel?.dismiss();
    root.dispatchEvent(new Event("innate:stage-background-click"));
  };
  root.addEventListener("pointerdown", onScenePointerDown);
  const telemetry = createTelemetry(telemetryOverlay, ros, { showBattery: !config.simControls });
  if (config.simControls) {
    micControl = createAgentMicControl(root, {
      // The tour's prompts are answered by holding the mic, whatever the user
      // says into it, so it needs both edges of the hold — not the transcript.
      startListening: () => {
        onboarding?.noteMicDown();
        return panel.startMic();
      },
      stopListening: () => {
        onboarding?.noteMicUp();
        panel.stopMic();
      },
    });
  }

  const parts = [
    ...(stopOnboardingStageSync ? [{ destroy: stopOnboardingStageSync }] : []),
    ...(onboarding ? [onboarding] : []),
    ...(scriptedActions ? [scriptedActions] : []),
    videoStage,
    ...(challengePanel ? [challengePanel] : []),
    telemetry,
    // Square, always-live camera tiles (own prefs key so teleop's defaults stay put).
    cameraSwitch,
    ...(micControl ? [micControl] : []),
    panel,
    {
      destroy: () => {
        root.removeEventListener("pointerdown", onScenePointerDown);
      },
    },
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
