// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Telemetry strip — robot name, battery, link state. Battery comes from
// sensor_msgs/BatteryState at 0.2 Hz; name/version ride /robot/info's
// JSON-in-String payload.

import { BATTERY_STATE_TOPIC, ROBOT_INFO_TOPIC, WEBSOCKET_STATUS_TOPIC } from "../constants.js";

/**
 * @param {HTMLElement} parent
 * @param {import("../rosClient.js").RosClient} rosClient
 * @param {{ showBattery?: boolean }} [opts] The sim has no battery, so it opts out.
 * @returns {{ destroy: () => void, preview: (snapshot: TelemetrySnapshot | null) => void }}
 */
export function createTelemetry(parent, rosClient, opts = {}) {
  const showBattery = opts.showBattery !== false;

  const wrap = document.createElement("div");
  wrap.className = "telemetry";

  const name = item("robot", "robot", "—", ROBOT_INFO_TOPIC);
  const battery = item("battery", "batt", "—", BATTERY_STATE_TOPIC);
  const link = item("link", "link", "—", "rosbridge websocket to the robot");
  // Cloud/local agent backend connection (the brain's websocket to its agent
  // backend) — distinct from the rosbridge LINK above.
  const agent = item("agent", "agent", "—", `the brain's connection to its agent backend — ${WEBSOCKET_STATUS_TOPIC}`);
  wrap.append(name.el, battery.el, link.el, agent.el);
  parent.appendChild(wrap);
  const items = [name, battery, link, agent];
  /** @type {Map<ReturnType<typeof item>, Required<TelemetryValue>>} */
  const liveValues = new Map(items.map((target) => [target, { text: "—", tone: "", meta: "" }]));
  let previewActive = false;
  /** @type {number | null} */
  let measureFrame = null;
  setBatteryVisible(showBattery);

  /**
   * @param {TelemetryKey} key
   * @param {string} labelText
   * @param {string} initial
   * @param {string} [title]
   */
  function item(key, labelText, initial, title = "") {
    const el = document.createElement("div");
    el.className = `telemetry-item telemetry-item-${key}`;
    if (title) el.title = title;
    const status = document.createElement("span");
    status.className = "telemetry-status-dot";
    status.setAttribute("aria-hidden", "true");
    const label = document.createElement("span");
    label.className = "microlabel";
    label.textContent = labelText;
    const value = document.createElement("span");
    value.className = "telemetry-value mono";
    value.title = initial;
    const meta = document.createElement("span");
    meta.className = "telemetry-meta mono";
    const track = document.createElement("span");
    track.className = "telemetry-value-track";
    const primary = document.createElement("span");
    primary.className = "telemetry-value-copy";
    primary.textContent = initial;
    const duplicate = document.createElement("span");
    duplicate.className = "telemetry-value-copy telemetry-value-duplicate";
    duplicate.textContent = initial;
    duplicate.setAttribute("aria-hidden", "true");
    track.append(primary, duplicate);
    value.append(track);
    el.append(status, label, value, meta);
    return { el, value, primary, duplicate, meta };
  }

  /**
   * @param {ReturnType<typeof item>} target
   * @param {string} text
   * @param {TelemetryTone} [tone]
   * @param {string} [meta]
   */
  function update(target, text, tone = "", meta = "") {
    liveValues.set(target, { text, tone, meta });
    if (previewActive) return;
    renderValue(target, text, tone, meta);
  }

  /**
   * @param {ReturnType<typeof item>} target
   * @param {string} text
   * @param {TelemetryTone} tone
   * @param {string} meta
   */
  function renderValue(target, text, tone, meta) {
    target.primary.textContent = text;
    target.duplicate.textContent = text;
    target.meta.textContent = meta;
    target.value.title = meta ? `${text} · ${meta}` : text;
    target.el.classList.toggle("live", tone === "live");
    target.el.classList.toggle("warn", tone === "warn");
    scheduleOverflowMeasure();
  }

  /** @param {boolean} visible */
  function setBatteryVisible(visible) {
    battery.el.hidden = !visible;
    wrap.classList.toggle("with-battery", visible);
  }

  /** @param {TelemetrySnapshot | null} snapshot */
  function preview(snapshot) {
    previewActive = snapshot !== null;
    if (!snapshot) {
      setBatteryVisible(showBattery);
      for (const [target, value] of liveValues) {
        renderValue(target, value.text, value.tone, value.meta);
      }
      return;
    }

    setBatteryVisible(snapshot.battery !== null);
    renderValue(name, snapshot.robot.text, snapshot.robot.tone ?? "", snapshot.robot.meta ?? "");
    renderValue(link, snapshot.link.text, snapshot.link.tone ?? "", snapshot.link.meta ?? "");
    renderValue(agent, snapshot.agent.text, snapshot.agent.tone ?? "", snapshot.agent.meta ?? "");
    if (snapshot.battery) {
      renderValue(battery, snapshot.battery.text, snapshot.battery.tone ?? "", snapshot.battery.meta ?? "");
    }
  }

  function scheduleOverflowMeasure() {
    if (measureFrame !== null) cancelAnimationFrame(measureFrame);
    measureFrame = requestAnimationFrame(() => {
      measureFrame = null;
      for (const target of items) {
        const overflow = Math.ceil(target.primary.scrollWidth - target.value.clientWidth);
        target.value.classList.toggle("overflowing", overflow > 1);
        target.value.style.setProperty("--telemetry-duration", `${Math.max(5, (target.primary.scrollWidth + 24) / 28)}s`);
      }
    });
  }

  const resizeObserver = new ResizeObserver(scheduleOverflowMeasure);
  resizeObserver.observe(wrap);
  scheduleOverflowMeasure();

  const unsubs = [
    rosClient.subscribe(ROBOT_INFO_TOPIC, (payload) => {
      if (typeof payload?.data !== "string") return;
      /** @type {RobotInfo} */
      let info;
      try {
        info = JSON.parse(payload.data);
      } catch {
        return;
      }
      const label = info.robot_name || info.hostname;
      if (label) {
        update(name, label, "", info.version ? `v${info.version}` : "");
      }
    }, undefined, "std_msgs/msg/String"),
    rosClient.onStateChange((state) => {
      update(link, state === "connected" ? "live" : state, state === "connected" ? "live" : state === "disconnected" ? "warn" : "");
    }),
    rosClient.subscribe(
      WEBSOCKET_STATUS_TOPIC,
      (payload) => {
        if (typeof payload?.data !== "string") return;
        let s;
        try {
          s = JSON.parse(payload.data);
        } catch {
          return;
        }
        const state = String(s?.state ?? "");
        let text = state || "—";
        let ok = false;
        let warn = false;
        if (s?.connected === true) {
          text = s?.hosted === false ? "local" : "cloud";
          ok = true;
        } else if (["connecting", "authenticating", "starting", "configured"].includes(state)) {
          text = "connecting";
          warn = true;
        } else if (state === "invalid_config") {
          text = "no key";
          warn = true;
        } else if (["connection_error", "backend_error", "disconnected", "error", "stopped"].includes(state)) {
          text = "offline";
          warn = true;
        }
        update(agent, text, ok ? "live" : warn ? "warn" : "");
      },
      500,
      "std_msgs/msg/String",
    ),
  ];

  if (showBattery) {
    unsubs.push(
      rosClient.subscribe(
        BATTERY_STATE_TOPIC,
        (/** @type {BatteryStateMsg} */ msg) => {
          const p = msg?.percentage;
          if (typeof p !== "number" || Number.isNaN(p)) return;
          // The robot publishes 0–100; tolerate a spec-compliant 0–1 source.
          const pct = p <= 1 ? p * 100 : p;
          update(battery, `${Math.round(pct)}%`, pct <= 15 ? "warn" : "");
        },
        1000,
        "sensor_msgs/msg/BatteryState",
      ),
    );
  }

  return {
    preview,
    destroy() {
      if (measureFrame !== null) cancelAnimationFrame(measureFrame);
      resizeObserver.disconnect();
      for (const unsub of unsubs) unsub();
      wrap.remove();
    },
  };
}

/** @typedef {"robot" | "battery" | "link" | "agent"} TelemetryKey */
/** @typedef {"" | "live" | "warn"} TelemetryTone */
/** @typedef {{ text: string, tone?: TelemetryTone, meta?: string }} TelemetryValue */
/**
 * @typedef {{
 *   robot: TelemetryValue,
 *   battery: TelemetryValue | null,
 *   link: TelemetryValue,
 *   agent: TelemetryValue
 * }} TelemetrySnapshot
 */
