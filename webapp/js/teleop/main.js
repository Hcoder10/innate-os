// @ts-check
// Teleoperation page entry — wires the shared singletons to the page modules
// and owns the connected/disconnected lifecycle.
//
// Disconnected: one quiet connect card. Connected: the video is the room —
// full-bleed stage with glass overlays (telemetry top-left, head tilt and
// mic toggle on the right edge, WASD chips bottom-left, joystick + TTS
// bottom-center). On reconnecting we keep the cockpit (frozen video, badge
// pulses); only an intentional disconnect or failed connect shows the card.

import { ros } from "../rosClient.js";
import { drive } from "../driveController.js";
import { initShell } from "../shell.js";
import { WebRtcSession } from "../webrtcSession.js";
import { mountPage } from "../pageMount.js";
import { createVideoStage, createAudioToggle } from "./videoStage.js";
import { createJoystick } from "./joystick.js";
import { createKeyboardDrive, createWasdChips } from "./keyboardDrive.js";
import { createHeadTilt } from "./headTilt.js";
import { createTtsBar } from "./ttsBar.js";
import { createTelemetry } from "./telemetry.js";
import { createArmPanel } from "./armPanel.js";
import { createProfilingPanel } from "./profilingPanel.js";
import { createSkillsPanel } from "./skillsPanel.js";
import { createChatPanel } from "./chatPanel.js";
import { createRightDock } from "./rightDock.js";
import { createAgentState } from "./agentState.js";
import { createSimControls } from "./simControls.js";
import { createCameraSwitch } from "./cameraSwitch.js";

initShell("teleop", "");

// Runtime feature flags (config.json, served static). Sim-only debug controls are
// off unless a deployment opts in. Loaded before the cockpit builds.
/** @type {any} */
const config = await fetch("/config.json", { cache: "no-store" })
  .then((r) => (r.ok ? r.json() : {}))
  .catch(() => ({}));

// Console debugging hook (also handy until the Debugging page exists).
/** @type {{ ros: typeof ros, drive: typeof drive, session: WebRtcSession | null }} */
const dbg = { ros, drive, session: null };
/** @type {any} */ (window).innate = dbg;

const stage = /** @type {HTMLElement} */ (document.getElementById("stage"));

mountPage(stage, "cockpit", buildCockpit);

/**
 * Sim API base for /stack_metrics. The committed config.json points at
 * localhost — fine when the webapp is opened on the same machine, but when the
 * page is served from another host that loopback names the viewer's box, not
 * the sim. Swap in the serving host, keeping the configured port/scheme.
 * @param {string | undefined} configured
 * @returns {string}
 */
function resolveSimApiUrl(configured) {
  const base = configured || "http://localhost:8000";
  const host = location.hostname;
  if (!host || host === "localhost" || host === "127.0.0.1") return base;
  try {
    const url = new URL(base);
    url.hostname = host;
    return url.href.replace(/\/$/, "");
  } catch {
    return base;
  }
}

/**
 * @param {HTMLElement} root
 * @returns {{ destroy: () => void }}
 */
function buildCockpit(root) {
  const session = new WebRtcSession(ros);
  dbg.session = session;

  const videoStage = createVideoStage(root, session);

  const telemetryOverlay = overlay("overlay-top-left");
  const rightRail = overlay("overlay-right");
  const chipsOverlay = overlay("overlay-bottom-left");
  const stickOverlay = overlay("overlay-joystick");
  const ttsOverlay = overlay("overlay-tts");
  const armOverlay = overlay("overlay-arm");
  root.append(telemetryOverlay, rightRail, chipsOverlay, stickOverlay, ttsOverlay, armOverlay);

  /** @param {string} className */
  function overlay(className) {
    const el = document.createElement("div");
    el.className = `overlay ${className}`;
    return el;
  }

  const keyboard = createKeyboardDrive(drive);
  const parts = [videoStage, createTelemetry(telemetryOverlay, ros)];
  // Robot-mic toggle. Skipped in the sim: the simulator's WebRTC server streams
  // video only (no microphone), so the toggle would do nothing. config.simControls
  // is the sim deployment's feature flag (env-driven; false on the real robot).
  if (!config.simControls) {
    parts.push(createAudioToggle(rightRail, session, videoStage.audioEl));
  }
  parts.push(
    createHeadTilt(rightRail, ros),
    createWasdChips(chipsOverlay, keyboard),
    createJoystick(stickOverlay, drive),
    createTtsBar(ttsOverlay, ros),
    createArmPanel(armOverlay, ros),
    createProfilingPanel(root, session),
    createCameraSwitch(root, session, ros),
    keyboard,
  );

  // Shared right dock hosting the Skills + Chat panes, each with its own popup
  // toggle on the camera's right edge. Built after the parts so it tears down
  // last; the panels register into it.
  const dock = createRightDock(root);
  // Shared directive/active-skill state, used by both panels.
  const agentState = createAgentState(ros);
  parts.push(
    createSkillsPanel(dock, ros, agentState),
    createChatPanel(dock, ros, agentState),
    dock,
    { destroy: () => agentState.destroy() },
  );

  // Sim-only debug controls (Reset Position + FPS/queue) — opt-in via config.json.
  if (config.simControls) {
    parts.push(createSimControls(root, ros, resolveSimApiUrl(config.simApiUrl)));
  }

  session.start();

  return {
    destroy() {
      drive.haltAll();
      for (const part of parts) part.destroy();
      session.destroy();
      root.innerHTML = "";
    },
  };
}
