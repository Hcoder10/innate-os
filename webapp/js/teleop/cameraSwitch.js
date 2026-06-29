import { WEBRTC_ACTIVE_STREAMS_TOPIC } from "../constants.js";

// Multi-camera PiP strip — a Zoom-style switcher pinned top-right. The big stage shows the PRIMARY camera;
// the strip shows every OTHER camera as a tile that climbs a three-rung ladder:
//
//   off (dim pill)  --click-->  live thumbnail (PiP)  --click-->  primary (fills the stage; leaves the strip)
//
// Promoting a thumbnail demotes the current big back into the strip (the swap). Hovering a live thumbnail
// reveals a × that drops it back to off. "Size is the state": the big feed is self-evidently primary, so
// tiles carry no extra active highlight.
//
// The roster (2-4 cameras, not hardcoded) comes from /webrtc/active_streams. The enabled set + primary
// persist in localStorage; by default only the first camera is on. All enabled feeds stream live — the
// session asks the robot to encode just that set, reneg-free.

const STORE_KEY = "innate.cameras";

/**
 * @param {HTMLElement} parent cockpit root — owns the strip.
 * @param {import("../webrtcSession.js").WebRtcSession} session
 * @param {import("../rosClient.js").RosClient} ros
 * @returns {{ destroy: () => void }}
 */
export function createCameraSwitch(parent, session, ros) {
  const strip = document.createElement("div");
  strip.className = "cam-strip";
  strip.hidden = true; // shown once we learn the camera roster
  parent.append(strip);

  /** @type {string[]} */ let roster = []; // camera names in m-line order
  /** @type {Set<string>} */ let enabled = new Set();
  /** @type {string | null} */ let primary = null;
  /** @type {Map<string, { tile: HTMLElement, video: HTMLVideoElement, index: number }>} */
  let tiles = new Map();

  loadPrefs();

  function loadPrefs() {
    try {
      const p = JSON.parse(localStorage.getItem(STORE_KEY) || "{}");
      enabled = new Set(Array.isArray(p.enabled) ? p.enabled : []);
      primary = typeof p.primary === "string" ? p.primary : null;
    } catch {
      /* defaults: nothing enabled, reconcile picks the first camera */
    }
  }

  function savePrefs() {
    localStorage.setItem(STORE_KEY, JSON.stringify({ enabled: [...enabled], primary }));
  }

  // Drop names the roster no longer has, then guarantee exactly one valid enabled primary (the big stage
  // always needs a feed, and the link always needs ≥1 live camera). Default: the first camera.
  function reconcile() {
    enabled = new Set([...enabled].filter((n) => roster.includes(n)));
    if (primary && !roster.includes(primary)) primary = null;
    if (!primary || !enabled.has(primary)) primary = [...enabled][0] ?? roster[0] ?? null;
    if (primary) enabled.add(primary);
  }

  // Tell the session which cameras to stream (the live set) and which one fills the big stage.
  function pushSession() {
    session.setActiveCameras(roster.filter((n) => enabled.has(n)));
    if (primary) session.setPrimaryCamera(roster.indexOf(primary), primary);
  }

  function commit() {
    savePrefs();
    pushSession();
    renderStructure();
  }

  /** Bring a camera one rung up the ladder: off → live secondary (does NOT steal the big stage). @param {string} name */
  function enable(name) {
    enabled.add(name);
    commit();
  }

  /** Promote a camera to the big stage; the current big drops back into the strip. @param {string} name */
  function promote(name) {
    if (name === primary) return;
    primary = name;
    enabled.add(name);
    commit();
  }

  /** Drop a live camera back to off. The primary isn't a strip tile, so it can't be disabled here. @param {string} name */
  function disable(name) {
    if (name === primary) return;
    if (enabled.size <= 1) return; // keep ≥1 live camera for the link
    enabled.delete(name);
    commit();
  }

  // Rebuild the strip's tiles — every camera EXCEPT the primary (which is the big stage).
  function renderStructure() {
    strip.hidden = roster.length === 0;
    tiles = new Map();
    if (strip.hidden) {
      strip.replaceChildren();
      return;
    }
    strip.replaceChildren(...roster.filter((name) => name !== primary).map(buildTile));
    syncStreams(session.state);
  }

  /** @param {string} name */
  function buildTile(name) {
    const index = roster.indexOf(name);
    if (!enabled.has(name)) {
      const tile = document.createElement("div");
      tile.className = "cam-tile off";
      tile.textContent = name;
      tile.title = `Turn on ${name} camera`;
      tile.addEventListener("click", () => enable(name));
      return tile;
    }

    const tile = document.createElement("div");
    tile.className = "cam-tile live";
    tile.title = `Make ${name} the main view`;
    tile.addEventListener("click", () => promote(name));
    const video = document.createElement("video");
    video.autoplay = true;
    video.muted = true;
    video.playsInline = true;
    const tag = document.createElement("span");
    tag.className = "cam-tile-label";
    tag.textContent = name;
    const close = document.createElement("button");
    close.type = "button";
    close.className = "cam-tile-close";
    close.title = `Turn off ${name}`;
    close.textContent = "×";
    close.addEventListener("click", (e) => {
      e.stopPropagation();
      disable(name);
    });
    tile.append(video, tag, close);
    tiles.set(name, { tile, video, index });
    return tile;
  }

  // Keep each live thumbnail bound to its camera's stream and reflect liveness (a not-yet-flowing feed
  // shows the "connecting" shimmer). Cheap and idempotent — runs on every session change.
  /** @param {WebRtcState} state */
  function syncStreams(state) {
    for (const { tile, video, index } of tiles.values()) {
      const stream = state.videoStreams[index] ?? null;
      if (video.srcObject !== stream) {
        video.srcObject = stream;
        video.play().catch(() => {});
      }
      tile.classList.toggle("connecting", !state.videoLive[index]);
    }
  }

  const unsubSession = session.onChange(syncStreams);

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
    // Status republishes on a 2s timer; only react when the roster actually changes.
    if (next.length === roster.length && next.every((c, i) => c === roster[i])) return;
    roster = next;
    reconcile();
    commit();
  });

  return {
    destroy() {
      unsub?.();
      unsubSession();
      strip.remove();
    },
  };
}
