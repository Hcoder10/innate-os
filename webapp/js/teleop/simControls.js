// @ts-check
// Sim-only debug controls — Reset Position + a live FPS/queue readout. Gated by
// config.json `simControls` (off by default): the real robot has no
// /sim/reset_position service and no /stack_metrics endpoint. The metrics are a
// best-effort cross-origin fetch of the sim API; if it fails they just hide.

import { RESET_POSITION_SERVICE } from "../constants.js";

/**
 * @param {HTMLElement} parent
 * @param {import("../rosClient.js").RosClient} rosClient
 * @param {string} simApiUrl Base URL of the sim FastAPI (for /stack_metrics).
 * @returns {{ destroy: () => void }}
 */
export function createSimControls(parent, rosClient, simApiUrl) {
  const wrap = document.createElement("div");
  wrap.className = "overlay overlay-sim";

  const label = document.createElement("p");
  label.className = "microlabel";
  label.textContent = "sim";

  const resetBtn = document.createElement("button");
  resetBtn.className = "sim-button";
  resetBtn.type = "button";
  resetBtn.textContent = "Reset Position";
  resetBtn.addEventListener("click", async () => {
    resetBtn.disabled = true;
    try {
      await rosClient.callService(RESET_POSITION_SERVICE, {});
    } catch {
      // ignore — sim only
    } finally {
      resetBtn.disabled = false;
    }
  });

  const metrics = document.createElement("p");
  metrics.className = "sim-metrics mono";
  metrics.hidden = true;

  wrap.append(label, resetBtn, metrics);
  parent.appendChild(wrap);

  async function poll() {
    try {
      const res = await fetch(`${simApiUrl.replace(/\/$/, "")}/stack_metrics`, { cache: "no-store" });
      if (!res.ok) throw new Error("bad status");
      const m = await res.json();
      const fpsVals = Object.values(m?.fps_by_camera || {}).filter((v) => typeof v === "number");
      const fps = fpsVals.length ? Math.round(Math.max(...fpsVals)) : 0;
      const queue = Object.values(m?.queue_sizes || {}).reduce((a, v) => a + (typeof v === "number" ? v : 0), 0);
      metrics.textContent = `${fps} fps · queue ${queue}`;
      metrics.hidden = false;
    } catch {
      metrics.hidden = true;
    }
  }

  const timer = setInterval(poll, 1000);
  void poll();

  return {
    destroy() {
      clearInterval(timer);
      wrap.remove();
    },
  };
}
