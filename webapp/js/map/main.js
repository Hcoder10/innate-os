// @ts-check
// Map page — a plain 2D <canvas> rendering the nav occupancy grid (/map) plus
// the robot's pose (/odom). Standalone for now; the widget can be embedded into
// teleop later. No three.js — the map is 2D, so a canvas + putImageData is all
// it needs.

import { ros } from "../rosClient.js";
import { initShell } from "../shell.js";
import { mountPage } from "../pageMount.js";
import { MAP_TOPIC, ODOM_TOPIC } from "../constants.js";

initShell("map", "../");

const stage = /** @type {HTMLElement} */ (document.getElementById("stage"));
mountPage(stage, "map-view", buildMap);

/**
 * @param {HTMLElement} root
 * @returns {{ destroy: () => void }}
 */
function buildMap(root) {
  const canvas = document.createElement("canvas");
  canvas.className = "map-canvas";
  root.appendChild(canvas);
  const ctx = canvas.getContext("2d");

  // Offscreen 1px-per-cell buffer; scaled to the canvas on draw (crisp + cheap).
  const off = document.createElement("canvas");
  const offCtx = off.getContext("2d");

  /** @type {{ width: number, height: number, resolution: number, originX: number, originY: number } | null} */
  let grid = null;
  /** @type {{ x: number, y: number, yaw: number } | null} */
  let pose = null;

  const dpr = () => window.devicePixelRatio || 1;

  function fit() {
    const r = root.getBoundingClientRect();
    const d = dpr();
    canvas.width = Math.max(1, Math.floor(r.width * d));
    canvas.height = Math.max(1, Math.floor(r.height * d));
    canvas.style.width = `${r.width}px`;
    canvas.style.height = `${r.height}px`;
    draw();
  }

  /** @param {any} msg nav_msgs/OccupancyGrid */
  function onMap(msg) {
    const info = msg?.info;
    const data = msg?.data;
    const width = info?.width | 0;
    const height = info?.height | 0;
    if (!info || !Array.isArray(data) || width <= 0 || height <= 0 || data.length < width * height) return;
    off.width = width;
    off.height = height;
    if (!offCtx) return;
    const img = offCtx.createImageData(width, height);
    for (let row = 0; row < height; row++) {
      const srcRow = height - 1 - row; // flip so canvas-top = highest world-y
      for (let col = 0; col < width; col++) {
        const v = data[srcRow * width + col];
        const di = (row * width + col) * 4;
        let shade;
        let a = 255;
        if (v < 0) {
          shade = 105; // unknown
          a = 200;
        } else {
          shade = 255 - Math.round((Math.max(0, Math.min(100, v)) / 100) * 255);
        }
        img.data[di] = shade;
        img.data[di + 1] = shade;
        img.data[di + 2] = shade;
        img.data[di + 3] = a;
      }
    }
    offCtx.putImageData(img, 0, 0);
    grid = {
      width,
      height,
      resolution: info.resolution || 0.05,
      originX: info.origin?.position?.x ?? 0,
      originY: info.origin?.position?.y ?? 0,
    };
    draw();
  }

  /** @param {any} msg nav_msgs/Odometry */
  function onOdom(msg) {
    const p = msg?.pose?.pose;
    const x = p?.position?.x;
    const y = p?.position?.y;
    const q = p?.orientation;
    if (typeof x !== "number" || typeof y !== "number" || !q) return;
    const yaw = Math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z));
    pose = { x, y, yaw };
    draw();
  }

  function draw() {
    if (!ctx) return;
    ctx.fillStyle = "#0a0a0c";
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    if (!grid) {
      ctx.fillStyle = "#8a8a93";
      ctx.font = `${14 * dpr()}px ui-monospace, monospace`;
      ctx.textAlign = "center";
      ctx.fillText("waiting for /map…", canvas.width / 2, canvas.height / 2);
      return;
    }

    const pad = 16 * dpr();
    const scale = Math.min((canvas.width - 2 * pad) / grid.width, (canvas.height - 2 * pad) / grid.height);
    const drawW = grid.width * scale;
    const drawH = grid.height * scale;
    const ox = (canvas.width - drawW) / 2;
    const oy = (canvas.height - drawH) / 2;
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(off, ox, oy, drawW, drawH);

    if (pose) {
      const col = (pose.x - grid.originX) / grid.resolution;
      const rowFromBottom = (pose.y - grid.originY) / grid.resolution;
      const px = ox + col * scale;
      const py = oy + (grid.height - rowFromBottom) * scale;
      const rad = Math.max(4, 6 * dpr());
      ctx.fillStyle = "#e8a33d";
      ctx.beginPath();
      ctx.arc(px, py, rad, 0, Math.PI * 2);
      ctx.fill();
      ctx.strokeStyle = "#e8a33d";
      ctx.lineWidth = 2 * dpr();
      ctx.beginPath();
      ctx.moveTo(px, py);
      ctx.lineTo(px + Math.cos(pose.yaw) * rad * 2.4, py - Math.sin(pose.yaw) * rad * 2.4);
      ctx.stroke();
    }
  }

  fit();
  const onResize = () => fit();
  window.addEventListener("resize", onResize);
  const unsubMap = ros.subscribe(MAP_TOPIC, onMap, 250);
  const unsubOdom = ros.subscribe(ODOM_TOPIC, onOdom, 100);

  return {
    destroy() {
      window.removeEventListener("resize", onResize);
      unsubMap();
      unsubOdom();
      root.innerHTML = "";
    },
  };
}
