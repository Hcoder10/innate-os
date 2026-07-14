// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Camera Calibration page — drives the mars_cam stereo_calibrator's interactive
// ChArUco stereo calibration over the RunStereoCalibration action. Start opens
// the goal and keeps it running while the operator moves the board in view of
// the live feed and clicks Capture (one enter_events publish per click); live
// feedback after each capture shows progress + whether the board was seen, plus
// the two coverage-dot debug images. The goal resolves (RMS errors) once enough
// images are captured, Stop cancels it, or the server's capture watchdog times
// out. Only MODE_MANUAL exists today, so the goal always sends mode: 0.

import { ros } from "../rosClient.js";
import { mountPage } from "../pageMount.js";
import { WebRtcSession } from "../webrtcSession.js";
import { createVideoStage } from "../teleop/videoStage.js";
import {
  RUN_STEREO_CALIBRATION_ACTION,
  RUN_STEREO_CALIBRATION_ACTION_TYPE,
  STEREO_CALIB_CAPTURE_TOPIC,
  STEREO_CALIB_DEFAULT_NUM_IMAGES,
  STEREO_CALIB_DEFAULT_MIN_CORNERS,
  MAIN_CAMERA_DEPTH_TOPIC,
} from "../constants.js";

// How long to wait for a depth frame before concluding "no calibration file
// detected yet" (a late arrival afterward still flips the indicator — see
// the MAIN_CAMERA_DEPTH_TOPIC subscription below).
const CALIB_FILE_CHECK_TIMEOUT_MS = 7000;

/** @param {HTMLElement} stage */
export function mount(stage) {
  return mountPage(stage, "calib", buildView);
}

/**
 * @typedef {Object} FeedbackState
 * @property {number} imagesCaptured
 * @property {number} target
 * @property {number} captureAttempts
 * @property {boolean | null} cornersFound
 * @property {string} message
 *
 * @typedef {Object} ResultState
 * @property {boolean} success
 * @property {string} message
 * @property {number} imagesCaptured
 * @property {number} leftRms
 * @property {number} rightRms
 * @property {number} stereoRms
 */

/**
 * @param {HTMLElement} root
 * @returns {{ destroy: () => void }}
 */
function buildView(root) {
  const session = new WebRtcSession(ros);

  // ---- header ---------------------------------------------------------
  const head = document.createElement("div");
  head.className = "page-head";
  const title = document.createElement("h1");
  title.className = "page-title";
  title.textContent = "Camera Calibration";
  const fileStatus = document.createElement("span");
  fileStatus.className = "calib-file-status microlabel";
  fileStatus.textContent = "Calibration file: checking…";
  head.append(title, fileStatus);

  // ---- grid: live feed | controls & feedback ---------------------------
  const grid = document.createElement("div");
  grid.className = "calib-grid";
  const videoWrap = document.createElement("div");
  videoWrap.className = "calib-video";
  const side = document.createElement("aside");
  side.className = "calib-side";
  grid.append(videoWrap, side);
  root.append(head, grid);

  const videoStage = createVideoStage(videoWrap, session);
  session.start();

  // ---- controls -----------------------------------------------------------
  const controls = document.createElement("div");
  controls.className = "calib-panel";

  const numField = fieldRow("Images to capture", String(STEREO_CALIB_DEFAULT_NUM_IMAGES));
  const minField = fieldRow("Min corners per capture", String(STEREO_CALIB_DEFAULT_MIN_CORNERS));

  const saveRow = document.createElement("label");
  saveRow.className = "calib-checkbox-row";
  const saveCheckbox = document.createElement("input");
  saveCheckbox.type = "checkbox";
  const saveText = document.createElement("span");
  saveText.textContent =
    "Save calibration when done (backs up the existing calibration file, then writes the new one)";
  saveRow.append(saveCheckbox, saveText);

  const startBtn = document.createElement("button");
  startBtn.type = "button";
  startBtn.className = "calib-btn calib-btn-primary";
  startBtn.textContent = "Start Calibration";

  const actionRow = document.createElement("div");
  actionRow.className = "calib-action-row";
  const captureBtn = document.createElement("button");
  captureBtn.type = "button";
  captureBtn.className = "calib-btn";
  captureBtn.textContent = "Capture";
  const stopBtn = document.createElement("button");
  stopBtn.type = "button";
  stopBtn.className = "calib-btn calib-btn-stop";
  stopBtn.textContent = "Stop";
  actionRow.append(captureBtn, stopBtn);

  const statusLine = document.createElement("p");
  statusLine.className = "calib-status microlabel";

  controls.append(numField.row, minField.row, saveRow, startBtn, actionRow, statusLine);

  // ---- live feedback --------------------------------------------------------
  const feedback = document.createElement("div");
  feedback.className = "calib-panel calib-feedback";

  const progressRow = statRow("Captured");
  const attemptsRow = statRow("Capture attempts");

  const boardBadge = document.createElement("span");
  boardBadge.className = "calib-badge";

  const feedbackMessage = document.createElement("p");
  feedbackMessage.className = "calib-feedback-message";

  const coverageRow = document.createElement("div");
  coverageRow.className = "calib-coverage-row";
  const leftCoverage = coverageTile("Left");
  const rightCoverage = coverageTile("Right");
  coverageRow.append(leftCoverage.tile, rightCoverage.tile);

  feedback.append(progressRow.row, attemptsRow.row, boardBadge, feedbackMessage, coverageRow);

  // ---- result ---------------------------------------------------------------
  const result = document.createElement("div");
  result.className = "calib-panel calib-result";

  side.append(controls, feedback, result);

  // ---- state ------------------------------------------------------------
  /** @type {{ cancel: () => void, canceling: boolean } | null} */
  let activeRun = null;
  /** @type {FeedbackState | null} */
  let fb = null;
  /** @type {(ResultState & { rejected?: boolean }) | null} */
  let lastResult = null;
  // "Does the robot have a calibration file loaded?" — no dedicated service for
  // this exists, so it's inferred from whether depth frames are flowing: the
  // depth estimator only publishes once a valid calibration is loaded (it logs
  // "Camera is uncalibrated" and stops otherwise). null = still checking.
  /** @type {boolean | null} */
  let calibFileDetected = null;

  /** @param {string} label @param {string} defaultValue */
  function fieldRow(label, defaultValue) {
    const row = document.createElement("label");
    row.className = "calib-field-row";
    const l = document.createElement("span");
    l.className = "calib-field-label";
    l.textContent = label;
    const input = document.createElement("input");
    input.type = "text";
    input.inputMode = "numeric";
    input.className = "calib-input mono";
    input.value = defaultValue;
    row.append(l, input);
    return { row, input };
  }

  /** @param {string} label */
  function statRow(label) {
    const row = document.createElement("div");
    row.className = "calib-stat-row";
    const l = document.createElement("span");
    l.className = "microlabel";
    l.textContent = label;
    const value = document.createElement("span");
    value.className = "calib-stat-value mono";
    row.append(l, value);
    return { row, value };
  }

  /** @param {string} label */
  function coverageTile(label) {
    const tile = document.createElement("div");
    tile.className = "calib-coverage-tile";
    const cap = document.createElement("span");
    cap.className = "microlabel calib-coverage-label";
    cap.textContent = label;
    const img = document.createElement("img");
    img.alt = `${label} coverage`;
    img.hidden = true;
    const empty = document.createElement("p");
    empty.className = "calib-coverage-empty microlabel";
    empty.textContent = "no capture yet";
    tile.append(cap, img, empty);
    return { tile, img, empty };
  }

  /**
   * Decode a sensor_msgs/CompressedImage-like feedback entry into an <img> src.
   * The rosbridge-compatible server's wire format for a uint8[] field wasn't
   * confirmed up front — standard rosbridge convention (and this codebase's
   * ttsAudio.js) base64-encodes byte arrays as a string, but handle a raw
   * array of byte values too in case this path differs.
   * @param {any} img
   * @returns {string | null}
   */
  function imageDataUrl(img) {
    if (!img) return null;
    const format = typeof img.format === "string" && img.format ? img.format.split(";")[0].trim() : "jpeg";
    const mime = `image/${format || "jpeg"}`;
    const data = img.data;
    if (typeof data === "string" && data) return `data:${mime};base64,${data}`;
    if (Array.isArray(data) && data.length) {
      const bytes = Uint8Array.from(data);
      return URL.createObjectURL(new Blob([/** @type {BlobPart} */ (bytes)], { type: mime }));
    }
    return null;
  }

  /** @param {{ tile: HTMLElement, img: HTMLImageElement, empty: HTMLElement }} coverage @param {string} url */
  function setCoverageImage(coverage, url) {
    const prevBlob = coverage.img.dataset.blobUrl;
    if (prevBlob) URL.revokeObjectURL(prevBlob);
    if (url.startsWith("blob:")) coverage.img.dataset.blobUrl = url;
    else delete coverage.img.dataset.blobUrl;
    coverage.img.src = url;
    coverage.img.hidden = false;
    coverage.empty.hidden = true;
  }

  /** @param {any} values action_feedback payload */
  function applyCoverageImages(values) {
    /** @type {string[]} */
    const names = Array.isArray(values?.image_names) ? values.image_names : [];
    /** @type {any[]} */
    const images = Array.isArray(values?.images) ? values.images : [];
    names.forEach((name, i) => {
      const url = imageDataUrl(images[i]);
      if (!url) return;
      if (name === "left_coverage") setCoverageImage(leftCoverage, url);
      else if (name === "right_coverage") setCoverageImage(rightCoverage, url);
    });
  }

  /** @param {any} values */
  function onFeedback(values) {
    if (!fb) return;
    if (typeof values?.images_captured === "number") fb.imagesCaptured = values.images_captured;
    if (typeof values?.capture_attempts === "number") fb.captureAttempts = values.capture_attempts;
    if (typeof values?.corners_found === "boolean") fb.cornersFound = values.corners_found;
    if (typeof values?.message === "string") fb.message = values.message;
    applyCoverageImages(values);
    render();
  }

  /** @param {string} value @param {number} fallback */
  function parsePositiveInt(value, fallback) {
    const n = parseInt(value, 10);
    return Number.isFinite(n) && n > 0 ? n : fallback;
  }

  function startCalibration() {
    if (activeRun || ros.state !== "connected") return;
    const numImages = parsePositiveInt(numField.input.value, STEREO_CALIB_DEFAULT_NUM_IMAGES);
    const minCorners = parsePositiveInt(minField.input.value, STEREO_CALIB_DEFAULT_MIN_CORNERS);
    const saveCalibration = saveCheckbox.checked;

    lastResult = null;
    fb = { imagesCaptured: 0, target: numImages, captureAttempts: 0, cornersFound: null, message: "" };

    const { promise, cancel } = ros.sendActionGoal(
      RUN_STEREO_CALIBRATION_ACTION,
      RUN_STEREO_CALIBRATION_ACTION_TYPE,
      { mode: 0, num_images: numImages, min_corners: minCorners, save_calibration: saveCalibration },
      { onFeedback },
    );
    activeRun = { cancel, canceling: false };
    render();

    promise.then(
      (values) => {
        activeRun = null;
        lastResult = {
          success: values?.success !== false,
          message: typeof values?.message === "string" ? values.message : "",
          imagesCaptured: typeof values?.images_captured === "number" ? values.images_captured : fb?.imagesCaptured ?? 0,
          leftRms: typeof values?.left_rms === "number" ? values.left_rms : 0,
          rightRms: typeof values?.right_rms === "number" ? values.right_rms : 0,
          stereoRms: typeof values?.stereo_rms === "number" ? values.stereo_rms : 0,
        };
        render();
      },
      (err) => {
        activeRun = null;
        lastResult = {
          success: false,
          rejected: true,
          message: err?.message || "Calibration goal was rejected",
          imagesCaptured: fb?.imagesCaptured ?? 0,
          leftRms: 0,
          rightRms: 0,
          stereoRms: 0,
        };
        render();
      },
    );
  }

  captureBtn.addEventListener("click", () => {
    if (!activeRun || activeRun.canceling) return;
    ros.publish(STEREO_CALIB_CAPTURE_TOPIC, { data: true });
  });

  stopBtn.addEventListener("click", () => {
    if (!activeRun || activeRun.canceling) return;
    activeRun.canceling = true;
    activeRun.cancel();
    render();
  });

  startBtn.addEventListener("click", startCalibration);

  /** @param {ResultState & { rejected?: boolean }} r */
  function renderResult(r) {
    result.replaceChildren();
    const banner = document.createElement("p");
    banner.className = "calib-result-banner" + (r.success ? " ok" : " bad");
    banner.textContent = r.success ? "Calibration succeeded" : "Calibration failed";
    const msg = document.createElement("p");
    msg.className = "calib-feedback-message";
    msg.textContent = r.message;
    result.append(banner, msg);
    if (r.success) {
      const stats = document.createElement("div");
      stats.className = "calib-result-stats";
      /** @param {string} label @param {string} value */
      const stat = (label, value) => {
        const el = document.createElement("div");
        el.className = "calib-stat-row";
        const l = document.createElement("span");
        l.className = "microlabel";
        l.textContent = label;
        const v = document.createElement("span");
        v.className = "calib-stat-value mono";
        v.textContent = value;
        el.append(l, v);
        return el;
      };
      stats.append(
        stat("Images captured", String(r.imagesCaptured)),
        stat("Left RMS", r.leftRms.toFixed(4)),
        stat("Right RMS", r.rightRms.toFixed(4)),
        stat("Stereo RMS", r.stereoRms.toFixed(4)),
      );
      result.append(stats);
    }
  }

  function render() {
    startBtn.disabled = !!activeRun || ros.state !== "connected";
    captureBtn.disabled = !activeRun || activeRun.canceling;
    stopBtn.disabled = !activeRun || activeRun.canceling;
    stopBtn.textContent = activeRun?.canceling ? "Stopping…" : "Stop";

    statusLine.textContent = activeRun
      ? "Calibration running — move the board and click Capture"
      : ros.state === "connected"
        ? "Idle"
        : "Not connected";

    feedback.hidden = fb === null;
    if (fb) {
      progressRow.value.textContent = `${fb.imagesCaptured} / ${fb.target}`;
      attemptsRow.value.textContent = String(fb.captureAttempts);
      boardBadge.classList.toggle("ok", fb.cornersFound === true);
      boardBadge.classList.toggle("bad", fb.cornersFound === false);
      boardBadge.textContent =
        fb.cornersFound === true
          ? "Board detected"
          : fb.cornersFound === false
            ? "Board not detected"
            : "Waiting for first capture";
      feedbackMessage.textContent = fb.message;
    }

    result.hidden = lastResult === null;
    if (lastResult) renderResult(lastResult);

    fileStatus.textContent =
      calibFileDetected === null
        ? "Calibration file: checking…"
        : calibFileDetected
          ? "Calibration file: detected"
          : "Calibration file: not detected";
    fileStatus.classList.toggle("ok", calibFileDetected === true);
    fileStatus.classList.toggle("bad", calibFileDetected === false);
  }

  const unsubState = ros.onStateChange(() => render());

  // A depth frame arriving at any point (even after the initial check window)
  // flips the indicator — a calibration saved mid-session shouldn't need a
  // page reload to be noticed.
  /** @type {number | null} */
  let calibCheckTimer = null;
  const unsubDepthCheck = ros.subscribe(
    MAIN_CAMERA_DEPTH_TOPIC,
    () => {
      if (calibFileDetected === true) return;
      calibFileDetected = true;
      if (calibCheckTimer !== null) {
        clearTimeout(calibCheckTimer);
        calibCheckTimer = null;
      }
      render();
    },
    5000,
  );
  calibCheckTimer = setTimeout(() => {
    calibCheckTimer = null;
    if (calibFileDetected === null) {
      calibFileDetected = false;
      render();
    }
  }, CALIB_FILE_CHECK_TIMEOUT_MS);

  render();

  return {
    destroy() {
      unsubState();
      unsubDepthCheck();
      if (calibCheckTimer !== null) clearTimeout(calibCheckTimer);
      // A run left going while the operator navigates away must not keep
      // capturing with no owner — cancel it, mirroring the skills menu.
      if (activeRun) activeRun.cancel();
      if (leftCoverage.img.dataset.blobUrl) URL.revokeObjectURL(leftCoverage.img.dataset.blobUrl);
      if (rightCoverage.img.dataset.blobUrl) URL.revokeObjectURL(rightCoverage.img.dataset.blobUrl);
      videoStage.destroy();
      session.destroy();
      root.innerHTML = "";
    },
  };
}
