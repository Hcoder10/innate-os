// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Collect page entry — the Teleop cockpit reused as a data-collection station.
// Same connect/cockpit lifecycle as teleop/datasets (connect card while
// disconnected; the cockpit kept across reconnecting, torn down on a real
// disconnect). The only addition over Teleop is the recording HUD overlay: it
// drives the recorder's idle → recording → completed loop while the operator
// teleoperates, and gates its Record button on the leader arm being engaged &
// streaming — fed in from the arm panel's onState callback.

import { ros } from "../rosClient.js";
import { drive } from "../driveController.js";
import { initShell } from "../shell.js";
import { WebRtcSession } from "../webrtcSession.js";
import { mountPage } from "../pageMount.js";
import { createVideoStage, createAudioToggle } from "../teleop/videoStage.js";
import { createJoystick } from "../teleop/joystick.js";
import { createKeyboardDrive, createWasdChips } from "../teleop/keyboardDrive.js";
import { createHeadTilt } from "../teleop/headTilt.js";
import { createTtsBar } from "../teleop/ttsBar.js";
import { createTelemetry } from "../teleop/telemetry.js";
import { createArmPanel } from "../teleop/armPanel.js";
import { createRecordPanel } from "./recordPanel.js";

initShell("collect", "../");

const stage = /** @type {HTMLElement} */ (document.getElementById("stage"));

mountPage(stage, "cockpit", buildCockpit);

/**
 * @param {HTMLElement} root
 * @returns {{ destroy: () => void }}
 */
function buildCockpit(root) {
  const session = new WebRtcSession(ros);

  const videoStage = createVideoStage(root, session);

  const telemetryOverlay = overlay("overlay-top-left");
  const rightRail = overlay("overlay-right");
  const chipsOverlay = overlay("overlay-bottom-left");
  const stickOverlay = overlay("overlay-joystick");
  const ttsOverlay = overlay("overlay-tts");
  const armOverlay = overlay("overlay-arm");
  const recordOverlay = overlay("overlay-record");
  root.append(telemetryOverlay, rightRail, chipsOverlay, stickOverlay, ttsOverlay, armOverlay, recordOverlay);

  /** @param {string} className */
  function overlay(className) {
    const el = document.createElement("div");
    el.className = `overlay ${className}`;
    return el;
  }

  // Head control is hidden during learned-policy recording — the camera
  // viewpoint must stay fixed across episodes — and shown only in the
  // recorded-movement wizard, where head motion is captured and replayed.
  // (Mirrors the mobile app: RecordEpisodeScreen has no head slider;
  // RecordReplayScreen's JoystickControls does.)
  const headTilt = createHeadTilt(rightRail, ros);

  // Built before the arm panel so its onState can gate recording immediately.
  // modalRoot is the full cockpit layer so the new-skill modal fills the stage
  // rather than being clipped inside the small top-center overlay.
  const recordPanel = createRecordPanel(recordOverlay, ros, {
    modalRoot: root,
    onHeadControl: (allowed) => {
      headTilt.el.hidden = !allowed;
    },
  });

  const keyboard = createKeyboardDrive(drive);
  const parts = [
    videoStage,
    createTelemetry(telemetryOverlay, ros),
    createAudioToggle(rightRail, session, videoStage.audioEl),
    headTilt,
    createWasdChips(chipsOverlay, keyboard),
    createJoystick(stickOverlay, drive),
    createTtsBar(ttsOverlay, ros),
    createArmPanel(armOverlay, ros, {
      onState: (s) => recordPanel.setArmReady(s.engaged && s.reading),
    }),
    recordPanel,
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
