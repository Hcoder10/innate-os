// @ts-check
// Telemetry strip — robot name, battery, link state. Battery comes from
// sensor_msgs/BatteryState at 0.2 Hz; name/version ride /robot/info's
// JSON-in-String payload.

import { BATTERY_STATE_TOPIC, ROBOT_INFO_TOPIC, WEBSOCKET_STATUS_TOPIC } from "../constants.js";

/**
 * @param {HTMLElement} parent
 * @param {import("../rosClient.js").RosClient} rosClient
 * @param {{ showBattery?: boolean }} [opts] The sim has no battery, so it opts out.
 * @returns {{ destroy: () => void }}
 */
export function createTelemetry(parent, rosClient, opts = {}) {
  const showBattery = opts.showBattery !== false;

  const wrap = document.createElement("div");
  wrap.className = "telemetry";

  const name = item("robot", "—");
  const battery = showBattery ? item("batt", "—") : null;
  const link = item("link", "—");
  // Cloud/local agent backend connection (the brain's websocket to its agent
  // backend) — distinct from the rosbridge LINK above.
  const agent = item("agent", "—");
  wrap.append(name.el, ...(battery ? [battery.el] : []), link.el, agent.el);
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
        agent.value.textContent = text;
        agent.el.classList.toggle("live", ok);
        agent.el.classList.toggle("warn", warn);
      },
      500,
    ),
  ];

  if (battery) {
    unsubs.push(
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
    );
  }

  return {
    destroy() {
      for (const unsub of unsubs) unsub();
      wrap.remove();
    },
  };
}
