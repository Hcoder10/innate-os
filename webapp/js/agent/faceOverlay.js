// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc

import { FACE_DEBUG_TOPIC } from "../constants.js";

const DEFAULT_X_TOLERANCE = 0.18;
const DEFAULT_Y_TOLERANCE = 0.4;

/**
 * @typedef {Object} DebugFace
 * @property {number} center_x
 * @property {number} center_y
 * @property {number} width
 * @property {number} height
 */

/**
 * @typedef {Object} FaceDebug
 * @property {string} state
 * @property {DebugFace[]} faces
 * @property {number | null} selected
 * @property {number | null} x_error
 * @property {number | null} y_error
 * @property {number} lock_frames
 * @property {number} lock_needed
 * @property {number} head_tilt
 * @property {string} action
 * @property {number} x_tolerance
 * @property {number} y_tolerance
 * @property {number} min_confidence
 */

/**
 * @param {HTMLElement} root
 * @param {import("../rosClient.js").RosClient} ros
 * @returns {{ destroy: () => void }}
 */
export function createFaceOverlay(root, ros) {
  const stageElement = root.querySelector(".video-stage");
  if (!(stageElement instanceof HTMLElement)) return { destroy() {} };
  const stage = /** @type {HTMLElement} */ (stageElement);

  const canvas = document.createElement("canvas");
  canvas.className = "face-debug-overlay";
  const status = stage.querySelector(".video-status");
  stage.insertBefore(canvas, status);

  const stack = root.querySelector(".overlay-stack-top-left");
  const card = document.createElement("div");
  card.className = "overlay face-debug-card";
  card.hidden = true;
  const cardTitle = document.createElement("div");
  cardTitle.className = "face-debug-title";
  const cardErrors = document.createElement("div");
  cardErrors.className = "face-debug-row";
  const cardAction = document.createElement("div");
  cardAction.className = "face-debug-row";
  card.append(cardTitle, cardErrors, cardAction);
  if (stack instanceof HTMLElement) stack.append(card);

  const context = canvas.getContext("2d");
  if (!context) {
    card.remove();
    canvas.remove();
    return { destroy() {} };
  }
  const ctx = /** @type {CanvasRenderingContext2D} */ (context);

  /** @type {FaceDebug | null} */
  let latest = null;
  let width = 1;
  let height = 1;
  /** @type {number | null} */
  let staleTimer = null;

  function fit() {
    const bounds = stage.getBoundingClientRect();
    width = Math.max(1, bounds.width);
    height = Math.max(1, bounds.height);
    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.max(1, Math.floor(width * dpr));
    canvas.height = Math.max(1, Math.floor(height * dpr));
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    draw();
  }

  function imageRect() {
    const video = stage.querySelector("video");
    if (!(video instanceof HTMLVideoElement) || !video.videoWidth || !video.videoHeight) {
      return { x: 0, y: 0, width, height };
    }

    const stageBounds = stage.getBoundingClientRect();
    const videoBounds = video.getBoundingClientRect();
    const style = getComputedStyle(video);
    const borderLeft = Number.parseFloat(style.borderLeftWidth) || 0;
    const borderTop = Number.parseFloat(style.borderTopWidth) || 0;
    const borderRight = Number.parseFloat(style.borderRightWidth) || 0;
    const borderBottom = Number.parseFloat(style.borderBottomWidth) || 0;
    const boxWidth = Math.max(0, videoBounds.width - borderLeft - borderRight);
    const boxHeight = Math.max(0, videoBounds.height - borderTop - borderBottom);
    const containScale = Math.min(boxWidth / video.videoWidth, boxHeight / video.videoHeight);
    const scale =
      style.objectFit === "cover"
        ? Math.max(boxWidth / video.videoWidth, boxHeight / video.videoHeight)
        : style.objectFit === "none"
          ? 1
          : style.objectFit === "scale-down"
            ? Math.min(1, containScale)
            : containScale;
    const imageWidth = video.videoWidth * scale;
    const imageHeight = video.videoHeight * scale;
    const [positionX = "50%", positionY = "50%"] = style.objectPosition.split(/\s+/);
    /** @param {string} value @param {number} freeSpace */
    const positionOffset = (value, freeSpace) => {
      if (value.endsWith("%")) return (Number.parseFloat(value) / 100) * freeSpace;
      if (value === "left" || value === "top") return 0;
      if (value === "right" || value === "bottom") return freeSpace;
      if (value === "center") return freeSpace / 2;
      return Number.parseFloat(value) || 0;
    };
    return {
      x:
        videoBounds.left -
        stageBounds.left +
        borderLeft +
        positionOffset(positionX, boxWidth - imageWidth),
      y:
        videoBounds.top -
        stageBounds.top +
        borderTop +
        positionOffset(positionY, boxHeight - imageHeight),
      width: imageWidth,
      height: imageHeight,
    };
  }

  /**
   * @param {FaceDebug} debug
   * @param {DebugFace | null} selected
   */
  function blocker(debug, selected) {
    if (debug.state === "starting") return "WAITING FOR CAMERA";
    if (!selected) {
      return debug.action === "no_camera_image"
        ? "CAMERA IMAGE UNAVAILABLE"
        : `NO FACE PASSED ${Math.round(debug.min_confidence * 100)}% DETECTOR`;
    }
    if (debug.x_error !== null && Math.abs(debug.x_error) > debug.x_tolerance) {
      return "OUTSIDE HORIZONTAL TOLERANCE";
    }
    if (debug.y_error !== null && Math.abs(debug.y_error) > debug.y_tolerance) {
      return "OUTSIDE VERTICAL TOLERANCE";
    }
    if (debug.lock_frames < debug.lock_needed) {
      return `STABILIZING ${debug.lock_frames}/${debug.lock_needed}`;
    }
    return "FACE LOCKED";
  }

  function renderCard() {
    if (!latest) {
      card.hidden = true;
      return;
    }
    const selected =
      typeof latest.selected === "number" ? (latest.faces[latest.selected] ?? null) : null;
    card.hidden = false;
    cardTitle.textContent = blocker(latest, selected);
    cardTitle.classList.toggle("locked", cardTitle.textContent === "FACE LOCKED");
    const xError = latest.x_error === null ? "—" : latest.x_error.toFixed(3);
    const yError = latest.y_error === null ? "—" : latest.y_error.toFixed(3);
    cardErrors.textContent =
      `faces ${latest.faces.length} · x ${xError}/±${latest.x_tolerance.toFixed(2)} · ` +
      `y ${yError}/±${latest.y_tolerance.toFixed(2)}`;
    cardAction.textContent =
      `head ${latest.head_tilt}° · lock ${latest.lock_frames}/${latest.lock_needed} · ` +
      latest.action.replaceAll("_", " ");
  }

  function draw() {
    const dpr = window.devicePixelRatio || 1;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    const image = imageRect();
    /** @param {number} value */
    const x = (value) => image.x + value * image.width;
    /** @param {number} value */
    const y = (value) => image.y + value * image.height;
    const xTolerance = latest?.x_tolerance ?? DEFAULT_X_TOLERANCE;
    const yTolerance = latest?.y_tolerance ?? DEFAULT_Y_TOLERANCE;

    const toleranceX = x(0.5 - xTolerance);
    const toleranceY = y(0.5 - yTolerance);
    const toleranceWidth = xTolerance * 2 * image.width;
    const toleranceHeight = yTolerance * 2 * image.height;
    ctx.fillStyle = "rgb(64 255 166 / 10%)";
    ctx.fillRect(toleranceX, toleranceY, toleranceWidth, toleranceHeight);
    ctx.strokeStyle = "rgb(64 255 166 / 80%)";
    ctx.lineWidth = 1.5;
    ctx.setLineDash([7, 5]);
    ctx.strokeRect(toleranceX, toleranceY, toleranceWidth, toleranceHeight);
    ctx.setLineDash([]);

    ctx.strokeStyle = "rgb(255 255 255 / 45%)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x(0.5) - 10, y(0.5));
    ctx.lineTo(x(0.5) + 10, y(0.5));
    ctx.moveTo(x(0.5), y(0.5) - 10);
    ctx.lineTo(x(0.5), y(0.5) + 10);
    ctx.stroke();

    if (!latest) return;
    const selected =
      typeof latest.selected === "number" ? (latest.faces[latest.selected] ?? null) : null;

    for (const [index, face] of latest.faces.entries()) {
      const isSelected = index === latest.selected;
      const inside =
        Math.abs(face.center_x - 0.5) <= latest.x_tolerance &&
        Math.abs(face.center_y - 0.5) <= latest.y_tolerance;
      ctx.strokeStyle = isSelected ? (inside ? "#40ffa6" : "#ff5c7a") : "#ffd166";
      ctx.lineWidth = isSelected ? 3 : 1.5;
      ctx.strokeRect(
        x(face.center_x - face.width / 2),
        y(face.center_y - face.height / 2),
        face.width * image.width,
        face.height * image.height,
      );
      ctx.fillStyle = ctx.strokeStyle;
      ctx.beginPath();
      ctx.arc(x(face.center_x), y(face.center_y), isSelected ? 4 : 2.5, 0, Math.PI * 2);
      ctx.fill();
    }

    if (selected) {
      ctx.strokeStyle = "rgb(255 255 255 / 65%)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x(0.5), y(0.5));
      ctx.lineTo(x(selected.center_x), y(selected.center_y));
      ctx.stroke();
    }

  }

  /** @param {any} msg */
  function onDebug(msg) {
    if (typeof msg?.data !== "string") return;
    try {
      const parsed = JSON.parse(msg.data);
      if (!parsed || !Array.isArray(parsed.faces)) return;
      latest = /** @type {FaceDebug} */ (parsed);
    } catch {
      return;
    }
    draw();
    renderCard();
    if (staleTimer !== null) clearTimeout(staleTimer);
    let visibleMs = 800;
    if (latest.state === "starting") visibleMs = 6_000;
    else if (latest.state === "failed") visibleMs = 5_000;
    else if (latest.state === "locked") visibleMs = 2_000;
    staleTimer = window.setTimeout(() => {
      latest = null;
      draw();
      renderCard();
      staleTimer = null;
    }, visibleMs);
  }

  const observer = new ResizeObserver(fit);
  observer.observe(stage);
  const video = stage.querySelector("video");
  if (video instanceof HTMLVideoElement) {
    video.addEventListener("loadedmetadata", draw);
    video.addEventListener("resize", draw);
  }
  fit();
  const unsubscribe = ros.subscribe(FACE_DEBUG_TOPIC, onDebug, 0, "std_msgs/msg/String");

  return {
    destroy() {
      unsubscribe();
      observer.disconnect();
      if (video instanceof HTMLVideoElement) {
        video.removeEventListener("loadedmetadata", draw);
        video.removeEventListener("resize", draw);
      }
      if (staleTimer !== null) clearTimeout(staleTimer);
      card.remove();
      canvas.remove();
    },
  };
}
