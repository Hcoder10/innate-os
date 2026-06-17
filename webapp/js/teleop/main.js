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
import { createConnectPanel } from "./connectPanel.js";
import { createVideoStage, createAudioToggle } from "./videoStage.js";
import { createJoystick } from "./joystick.js";
import { createKeyboardDrive, createWasdChips } from "./keyboardDrive.js";
import { createHeadTilt } from "./headTilt.js";
import { createTtsBar } from "./ttsBar.js";
import { createTelemetry } from "./telemetry.js";
import { createArmPanel } from "./armPanel.js";

initShell("teleop", "");

// Console debugging hook (also handy until the Debugging page exists).
/** @type {{ ros: typeof ros, drive: typeof drive, session: WebRtcSession | null }} */
const dbg = { ros, drive, session: null };
/** @type {any} */ (window).innate = dbg;

const stage = /** @type {HTMLElement} */ (document.getElementById("stage"));

const connectLayer = document.createElement("div");
connectLayer.className = "connect-layer";
const cockpitLayer = document.createElement("div");
cockpitLayer.className = "cockpit";
cockpitLayer.hidden = true;
stage.append(connectLayer, cockpitLayer);

createConnectPanel(connectLayer, ros);

// Served by the robot itself → connect to it straight away; the connect
// panel stays underneath as the fallback if that fails. Laptop-served
// (localhost) keeps the manual flow.
const servedHost = location.hostname;
if (servedHost && servedHost !== "localhost" && servedHost !== "127.0.0.1") {
  ros.connect(servedHost);
}

/** @type {{ destroy: () => void } | null} */
let cockpit = null;

ros.onStateChange((state) => {
  const showCockpit = state === "connected" || (state === "reconnecting" && cockpit !== null);
  connectLayer.hidden = showCockpit;
  cockpitLayer.hidden = !showCockpit;
  if (state === "connected" && !cockpit) {
    cockpit = buildCockpit(cockpitLayer);
  } else if (state === "disconnected" && cockpit) {
    cockpit.destroy();
    cockpit = null;
  }
});

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
  const parts = [
    videoStage,
    createTelemetry(telemetryOverlay, ros),
    createAudioToggle(rightRail, session, videoStage.audioEl),
    createHeadTilt(rightRail, ros),
    createWasdChips(chipsOverlay, keyboard),
    createJoystick(stickOverlay, drive),
    createTtsBar(ttsOverlay, ros),
    createArmPanel(armOverlay, ros),
    keyboard,
  ];

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
