// @ts-check
// Session factory: the camera panel's data source. Real robots stream WebRTC
// video (WebRtcSession); the simulator has no video pipeline -- SimSession
// (built from sim/viewer, lazy-loaded) renders the live sim with Three.js and
// exposes canvas captureStreams behind the exact same interface, so
// videoStage/cameraSwitch/profiling consume either without knowing.
//
// Usage (module top level -- the import must resolve before buildCockpit):
//   const { createSession, createStage } = await robotSessionFactory();
//   ...inside buildCockpit:
//     const session = createSession();
//     const stage = createStage ? createStage(root, session) : createVideoStage(root, session);

import { WebRtcSession } from "./webrtcSession.js";
import { ros } from "./rosClient.js";
import { getConfig } from "./config.js";

/**
 * @typedef {{
 *   audioEl: HTMLAudioElement | null,
 *   destroy: () => void,
 *   setOnboardingStep?: (step: "await_hello" | "welcome" | "await_go"
 *     | "tour_cameras" | "tour_telemetry" | "tour_chat" | "complete") => void
 * }} RobotStage
 */

/**
 * @returns {Promise<{ createSession: () => WebRtcSession, createStage: ((root: HTMLElement, session: WebRtcSession) => RobotStage) | null }>}
 * createStage is null for real robots (pages use createVideoStage); in sim it
 * mounts the live Three.js canvas (full resolution, drag-to-orbit).
 */
export async function robotSessionFactory() {
  /** @type {any} */
  const config = await getConfig();
  if (config.simControls) {
    try {
      // Served by proxy/https_server.py from sim/viewer's build -- a runtime
      // URL, not a module path tsc can resolve.
      // @ts-ignore
      const mod = await import("/sim-viewer/sim-session.js");
      return {
        createSession: () => /** @type {any} */ (mod.createSimSession()),
        createStage: (root, session) => mod.createSimStage(root, /** @type {any} */ (session)),
      };
    } catch (err) {
      console.error("[robotSession] sim viewer bundle unavailable, falling back to WebRTC:", err);
    }
  }
  return { createSession: () => new WebRtcSession(ros), createStage: null };
}
