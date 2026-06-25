// @ts-check
// Telemetry strip — robot name, battery, link state. Battery comes from
// sensor_msgs/BatteryState at 0.2 Hz; name/version ride /robot/info's
// JSON-in-String payload.

import { BATTERY_STATE_TOPIC, ROBOT_INFO_TOPIC } from "../constants.js";

/**
 * @param {HTMLElement} parent
 * @param {import("../rosClient.js").RosClient} rosClient
 * @returns {{ destroy: () => void }}
 */
export function createTelemetry(parent, rosClient) {
  const wrap = document.createElement("div");
  wrap.className = "telemetry";

  const name = item("robot", "—");
  const battery = item("batt", "—");
  const link = item("link", "—");
  wrap.append(name.el, battery.el, link.el);
  parent.appendChild(wrap);

  /**
   * @param {string} labelText
   * @param {string} initial
   */
  function item(labelText, initial) {
    const el = document.createElement("div");
    el.className = "telemetry-item";
    const label = document.createElement("span");
    label.className = "microlabel";
    label.textContent = labelText;
    const value = document.createElement("span");
    value.className = "telemetry-value mono";
    value.textContent = initial;
    el.append(label, value);
    return { el, value };
  }

  const unsubs = [
    rosClient.subscribe(
      BATTERY_STATE_TOPIC,
      (/** @type {BatteryStateMsg} */ msg) => {
        const p = msg?.percentage;
        if (typeof p !== "number" || Number.isNaN(p)) return;
        // The robot publishes 0–100; tolerate a spec-compliant 0–1 source.
        const pct = p <= 1 ? p * 100 : p;
        battery.value.textContent = `${Math.round(pct)}%`;
        battery.el.classList.toggle("warn", pct <= 15);
      },
      1000,
    ),
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
        name.value.textContent = info.version ? `${label} · v${info.version}` : label;
      }
    }),
    rosClient.onStateChange((state) => {
      link.value.textContent = state === "connected" ? "live" : state;
      link.el.classList.toggle("live", state === "connected");
    }),
  ];

  return {
    destroy() {
      for (const unsub of unsubs) unsub();
      wrap.remove();
    },
  };
}
