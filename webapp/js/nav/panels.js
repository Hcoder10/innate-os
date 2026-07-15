// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Nav telemetry sidebar — raw readouts of every navigation sensor, grouped
// into small panels: pose (AMCL map-frame + raw odom), velocity (commanded
// /cmd_vel vs measured /odom), lidar summary, nav/battery state, and a
// per-topic receive-rate table. Values fill in as messages arrive; a topic
// that never publishes just keeps its "—".

import { ros } from "../rosClient.js";
import {
  AMCL_POSE_TOPIC,
  BATTERY_STATE_TOPIC,
  CMD_VEL_TOPIC,
  MAP_TOPIC,
  NAV_CURRENT_MAP_TOPIC,
  NAV_CURRENT_MODE_TOPIC,
  ODOM_TOPIC,
  SCAN_TOPIC,
} from "../constants.js";

const DASH = "—";
const RATE_WINDOW_MS = 5000;

/** @param {number} rad */
function deg(rad) {
  return (rad * 180) / Math.PI;
}

/** @param {any} q quaternion → yaw radians, or null */
function yawOf(q) {
  if (!q) return null;
  return Math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z));
}

/** Message-arrival tracker for the rates table (Hz over a sliding window). */
function makeRate() {
  /** @type {number[]} */
  const stamps = [];
  return {
    tick() {
      stamps.push(performance.now());
    },
    hz() {
      const now = performance.now();
      while (stamps.length && now - stamps[0] > RATE_WINDOW_MS) stamps.shift();
      if (stamps.length < 2) return null;
      return stamps.length / (RATE_WINDOW_MS / 1000);
    },
  };
}

/**
 * @param {HTMLElement} root the sidebar container.
 * @returns {{ destroy: () => void }}
 */
export function createNavPanels(root) {
  /** @param {string} title @returns {{ row: (label: string) => HTMLElement }} */
  function panel(title) {
    const section = document.createElement("section");
    section.className = "nav-panel";
    const label = document.createElement("p");
    label.className = "microlabel";
    label.textContent = title;
    section.appendChild(label);
    root.appendChild(section);
    return {
      row(name) {
        const row = document.createElement("div");
        row.className = "nav-row";
        const key = document.createElement("span");
        key.className = "nav-row-label";
        key.textContent = name;
        const value = document.createElement("span");
        value.className = "nav-row-value mono";
        value.textContent = DASH;
        row.append(key, value);
        section.appendChild(row);
        return value;
      },
    };
  }

  const pose = panel("Pose");
  const mapXY = pose.row("map x · y");
  const mapYaw = pose.row("map heading");
  const odomXY = pose.row("odom x · y");
  const odomYaw = pose.row("odom heading");

  const vel = panel("Velocity");
  const velActual = vel.row("measured v · ω");
  const velCmd = vel.row("commanded v · ω");

  const lidar = panel("Lidar");
  const scanPoints = lidar.row("points");
  const scanNearest = lidar.row("nearest");

  const nav = panel("Nav state");
  const navMode = nav.row("mode");
  const navMap = nav.row("map");
  const battery = nav.row("battery");

  const rates = panel("Received rates");

  // ---- subscriptions -------------------------------------------------------
  /** @type {Array<() => void>} */
  const unsubs = [];
  /** @type {Array<{ label: string, value: HTMLElement, rate: ReturnType<typeof makeRate> }>} */
  const rateRows = [];

  /**
   * Subscribe + count arrivals for the rates table in one go.
   * @param {string} topic @param {(msg: any) => void} handler
   * @param {number} [throttle] @param {string} [type]
   */
  function watch(topic, handler, throttle, type) {
    const rate = makeRate();
    rateRows.push({ label: topic, value: rates.row(topic), rate });
    unsubs.push(
      ros.subscribe(
        topic,
        (msg) => {
          rate.tick();
          handler(msg);
        },
        throttle,
        type,
      ),
    );
  }

  watch(ODOM_TOPIC, (msg) => {
    const p = msg?.pose?.pose?.position;
    const yaw = yawOf(msg?.pose?.pose?.orientation);
    if (typeof p?.x === "number" && typeof p?.y === "number") odomXY.textContent = `${p.x.toFixed(2)}, ${p.y.toFixed(2)} m`;
    if (yaw !== null) odomYaw.textContent = `${deg(yaw).toFixed(0)}°`;
    const v = msg?.twist?.twist?.linear?.x;
    const w = msg?.twist?.twist?.angular?.z;
    if (typeof v === "number" && typeof w === "number") {
      velActual.textContent = `${v.toFixed(2)} m/s · ${deg(w).toFixed(0)}°/s`;
    }
  }, 100);

  watch(AMCL_POSE_TOPIC, (msg) => {
    const p = msg?.pose?.pose?.position;
    const yaw = yawOf(msg?.pose?.pose?.orientation);
    if (typeof p?.x === "number" && typeof p?.y === "number") mapXY.textContent = `${p.x.toFixed(2)}, ${p.y.toFixed(2)} m`;
    if (yaw !== null) mapYaw.textContent = `${deg(yaw).toFixed(0)}°`;
  }, 0, "geometry_msgs/msg/PoseWithCovarianceStamped");

  watch(CMD_VEL_TOPIC, (msg) => {
    const v = msg?.linear?.x;
    const w = msg?.angular?.z;
    if (typeof v === "number" && typeof w === "number") {
      velCmd.textContent = `${v.toFixed(2)} m/s · ${deg(w).toFixed(0)}°/s`;
    }
  }, 100, "geometry_msgs/msg/Twist");

  watch(SCAN_TOPIC, (msg) => {
    const ranges = msg?.ranges;
    if (!Array.isArray(ranges)) return;
    let count = 0;
    let best = Infinity;
    let bestI = -1;
    for (let i = 0; i < ranges.length; i++) {
      const r = ranges[i];
      if (!Number.isFinite(r) || r < msg.range_min || r > msg.range_max) continue;
      count++;
      if (r < best) {
        best = r;
        bestI = i;
      }
    }
    scanPoints.textContent = `${count} / ${ranges.length}`;
    scanNearest.textContent =
      bestI >= 0 ? `${best.toFixed(2)} m @ ${deg(msg.angle_min + bestI * msg.angle_increment).toFixed(0)}°` : DASH;
  }, 150, "sensor_msgs/msg/LaserScan");

  // /map barely changes — it's in the rates table for liveness, not content.
  watch(MAP_TOPIC, () => {}, 250);

  unsubs.push(
    ros.subscribe(NAV_CURRENT_MODE_TOPIC, (msg) => {
      if (typeof msg?.data === "string" && msg.data) navMode.textContent = msg.data;
    }, 0, "std_msgs/msg/String"),
    ros.subscribe(NAV_CURRENT_MAP_TOPIC, (msg) => {
      if (typeof msg?.data === "string" && msg.data) navMap.textContent = msg.data;
    }, 0, "std_msgs/msg/String"),
    ros.subscribe(BATTERY_STATE_TOPIC, (msg) => {
      const p = msg?.percentage;
      if (typeof p !== "number" || Number.isNaN(p)) return;
      // The robot publishes 0–100; tolerate a spec-compliant 0–1 source.
      const pct = p <= 1 ? p * 100 : p;
      const volts = typeof msg?.voltage === "number" && msg.voltage > 0 ? ` · ${msg.voltage.toFixed(1)} V` : "";
      battery.textContent = `${Math.round(pct)}%${volts}`;
    }, 1000),
  );

  const rateTimer = setInterval(() => {
    for (const { value, rate } of rateRows) {
      const hz = rate.hz();
      value.textContent = hz === null ? DASH : `${hz.toFixed(1)} Hz`;
    }
  }, 1000);

  return {
    destroy() {
      clearInterval(rateTimer);
      for (const unsub of unsubs) unsub();
      root.innerHTML = "";
    },
  };
}
