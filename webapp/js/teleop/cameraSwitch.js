import { WEBRTC_ACTIVE_STREAMS_TOPIC } from "../constants.js";

/**
 * Dynamic per-camera view switcher — a small glass button per camera, sitting just left of the profiling
 * toggle. The camera list is NOT hardcoded: it comes from the robot's /webrtc/active_streams (published
 * every 2s), so it adapts to however many cameras the node is configured with. Clicking a button asks the
 * session to switch which camera is displayed — a no-reneg START, so it's instant (no reconnect).
 *
 * @param {HTMLElement} parent cockpit root — owns the button bar.
 * @param {import("../webrtcSession.js").WebRtcSession} session
 * @param {import("../rosClient.js").RosClient} ros
 * @returns {{ destroy: () => void }}
 */
export function createCameraSwitch(parent, session, ros) {
  const bar = document.createElement("div");
  bar.className = "cam-switch";
  bar.hidden = true; // shown once we learn there's more than one camera to switch between
  parent.append(bar);

  /** @type {string[]} */
  let cameras = [];

  function render() {
    bar.hidden = cameras.length < 2; // nothing to switch between with 0-1 cameras
    if (bar.hidden) {
      bar.replaceChildren();
      return;
    }
    const selected = session.selectedCamera;
    bar.replaceChildren(
      ...cameras.map((name, index) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "cam-switch-btn" + (index === selected.index ? " active" : "");
        btn.textContent = name;
        btn.title = `View ${name} camera`;
        btn.addEventListener("click", () => {
          session.selectCamera(index, name);
          render(); // reflect the new selection immediately, don't wait for the next status
        });
        return btn;
      }),
    );
  }

  const unsub = ros.subscribe(WEBRTC_ACTIVE_STREAMS_TOPIC, (m) => {
    const raw = m?.data ?? m?.msg?.data;
    if (typeof raw !== "string") return;
    let next;
    try {
      next = JSON.parse(raw).cameras;
    } catch {
      return;
    }
    if (!Array.isArray(next)) return;
    // Status republishes on a 2s timer; only re-render when the list actually changes.
    if (next.length === cameras.length && next.every((c, i) => c === cameras[i])) return;
    cameras = next;
    render();
  });

  render();

  return {
    destroy() {
      unsub?.();
      bar.remove();
    },
  };
}
