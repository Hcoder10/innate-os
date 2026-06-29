import { WEBRTC_ACTIVE_STREAMS_TOPIC } from "../constants.js";
import { createMap } from "../map/mapWidget.js";

// Multi-view PiP strip — a Zoom-style switcher pinned bottom-right. The big stage shows the PRIMARY view;
// the strip shows every OTHER view as a tile that climbs a three-rung ladder:
//
//   off (dim pill)  --click-->  live thumbnail (PiP)  --click-->  primary (fills the stage; leaves the strip)
//
// Promoting a thumbnail demotes whatever was big back into the strip (the swap). Hovering a live thumbnail
// reveals a × that drops it back to off. "Size is the state": the big view is self-evidently primary, so
// tiles carry no extra active highlight.
//
// Views are the robot's cameras (from /webrtc/active_streams, 2-4, not hardcoded) PLUS the nav map, which
// behaves identically — off / live thumbnail / big. When the map is primary it covers the stage and the
// video stage hides; a camera stays the session's primary underneath so the WebRTC link keeps a live feed.
// The enabled set + which view is primary persist in localStorage; by default only the first camera is on.

const STORE_KEY = "innate.cameras";
const MAP_ZOOM_KEY = "innate.map.zoom"; // { small, big } metres-across, persisted per map size
const MAP_ID = "__map__"; // sentinel "view" id for the nav map (never a real camera name)
const MAP_ZOOM_DEFAULT = { small: 6, big: 16 }; // tighter as a thumbnail, wider on the full stage

/**
 * @param {HTMLElement} parent cockpit root — owns the strip and (when big) the map layer.
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
  /** @type {Set<string>} */ let enabledCams = new Set();
  let mapOn = false;
  /** @type {string} */ let primary = MAP_ID; // a camera name or MAP_ID; reconcile fixes the real default
  /** @type {Map<string, { tile: HTMLElement, video: HTMLVideoElement, index: number }>} */
  let tiles = new Map();

  // The map widget is heavy (subscriptions + canvas), so it exists only while the map is on. Its host div
  // is persistent and reparents between a strip tile (small) and the stage (big) — never rebuilt.
  /** @type {HTMLElement | null} */ let mapHost = null;
  /** @type {{ destroy: () => void, setZoom: (m: number) => void } | null} */ let mapWidget = null;
  /** @type {"small" | "big"} which saved zoom is live: thumbnail vs full stage */ let mapMode = "small";
  let mapZoom = { ...MAP_ZOOM_DEFAULT };

  loadPrefs();

  function loadPrefs() {
    try {
      const p = JSON.parse(localStorage.getItem(STORE_KEY) || "{}");
      enabledCams = new Set(Array.isArray(p.enabled) ? p.enabled : []);
      mapOn = p.mapOn === true;
      primary = typeof p.primary === "string" ? p.primary : MAP_ID;
    } catch {
      /* defaults: nothing enabled, reconcile picks the first camera */
    }
    try {
      const z = JSON.parse(localStorage.getItem(MAP_ZOOM_KEY) || "{}");
      if (z.small > 0) mapZoom.small = z.small;
      if (z.big > 0) mapZoom.big = z.big;
    } catch {
      /* defaults above */
    }
  }

  function saveMapZoom() {
    localStorage.setItem(MAP_ZOOM_KEY, JSON.stringify(mapZoom));
  }

  function savePrefs() {
    localStorage.setItem(STORE_KEY, JSON.stringify({ enabled: [...enabledCams], mapOn, primary }));
  }

  // Drop names the roster no longer has, then guarantee a valid enabled primary (the stage always needs a
  // view). Zero cameras is fine when the map is primary (map-only releases WebRTC). Default when nothing
  // valid persists: the first camera.
  function reconcile() {
    enabledCams = new Set([...enabledCams].filter((n) => roster.includes(n)));
    const validPrimary =
      primary === MAP_ID ? mapOn : roster.includes(primary) && enabledCams.has(primary);
    if (!validPrimary) primary = [...enabledCams][0] ?? (mapOn ? MAP_ID : roster[0]) ?? MAP_ID;
    if (primary === MAP_ID) mapOn = true;
    else enabledCams.add(primary);
  }

  // Tell the session which cameras to stream and which one backs the big stage. When the map is primary
  // there's no big camera, but a camera still streams as the session primary (its liveness gates the link).
  function pushSession() {
    session.setActiveCameras(roster.filter((n) => enabledCams.has(n)));
    const primaryCam =
      primary !== MAP_ID && roster.includes(primary) ? primary : roster.find((n) => enabledCams.has(n));
    if (primaryCam) session.setPrimaryCamera(roster.indexOf(primaryCam), primaryCam);
  }

  function commit() {
    savePrefs();
    pushSession();
    renderStructure();
  }

  /** Bring a view one rung up the ladder: off → live secondary (does NOT steal the big stage). @param {string} id */
  function enable(id) {
    if (id === MAP_ID) mapOn = true;
    else enabledCams.add(id);
    commit();
  }

  /** Promote a view to the big stage; whatever was big drops back into the strip. @param {string} id */
  function promote(id) {
    if (id === primary) return;
    primary = id;
    if (id === MAP_ID) mapOn = true;
    else enabledCams.add(id);
    commit();
  }

  // Drop a live view back to off. The primary isn't a strip tile, so it can't be disabled here — which
  // means a camera primary always keeps ≥1 camera on; zero cameras is only reachable with the map primary.
  /** @param {string} id */
  function disable(id) {
    if (id === primary) return;
    if (id === MAP_ID) mapOn = false;
    else enabledCams.delete(id);
    commit();
  }

  // Match the live map widget to mapOn (create when turned on, tear down when off).
  function ensureMap() {
    if (mapOn && !mapWidget) {
      mapHost = document.createElement("div");
      mapHost.className = "cam-map-host";
      mapWidget = createMap(mapHost, {
        zoom: mapZoom[mapMode],
        // Scroll-zoom persists against whichever size is showing now.
        onZoomChange: (m) => {
          mapZoom[mapMode] = m;
          saveMapZoom();
        },
      });
    } else if (!mapOn && mapWidget) {
      mapWidget.destroy();
      mapHost?.remove();
      mapWidget = null;
      mapHost = null;
    }
  }

  // Park the map host where the current primary dictates: full stage (big) or inside its strip tile (small),
  // and swap in that size's saved zoom. (As a thumbnail, a plain click still bubbles to the tile's promote
  // handler — goal-mode is off and its control hidden — so the map stays clickable while also wheel-zoomable.)
  function placeMap() {
    const big = primary === MAP_ID;
    mapMode = big ? "big" : "small";
    parent.classList.toggle("cam-map-primary", big);
    if (mapHost) {
      mapHost.classList.toggle("big", big);
      if (big && parent.firstChild !== mapHost) parent.insertBefore(mapHost, parent.firstChild);
    }
    mapWidget?.setZoom(mapZoom[mapMode]);
  }

  // Rebuild the strip's tiles — every view EXCEPT the primary (which is the big stage).
  function renderStructure() {
    strip.hidden = roster.length === 0;
    tiles = new Map();
    ensureMap();
    if (strip.hidden) {
      strip.replaceChildren();
      placeMap();
      return;
    }
    const children = roster.filter((name) => name !== primary).map(buildCameraTile);
    if (primary !== MAP_ID) children.push(buildMapTile());
    strip.replaceChildren(...children);
    placeMap();
    syncStreams(session.state);
  }

  /** @param {string} name */
  function buildCameraTile(name) {
    const index = roster.indexOf(name);
    if (!enabledCams.has(name)) return offTile(name, name, `Turn on ${name} camera`);
    const tile = liveTile(name, name, `Make ${name} the main view`);
    const video = document.createElement("video");
    video.autoplay = true;
    video.muted = true;
    video.playsInline = true;
    tile.prepend(video);
    tiles.set(name, { tile, video, index });
    return tile;
  }

  function buildMapTile() {
    if (!mapOn) return offTile(MAP_ID, "map", "Show the navigation map");
    const tile = liveTile(MAP_ID, "map", "Make the map the main view");
    tile.classList.add("cam-tile-map");
    if (mapHost) tile.prepend(mapHost); // reparent the persistent host into the thumbnail
    return tile;
  }

  /** Dim pill for an off view. @param {string} id @param {string} label @param {string} title */
  function offTile(id, label, title) {
    const tile = document.createElement("div");
    tile.className = "cam-tile off";
    tile.textContent = label;
    tile.title = title;
    tile.addEventListener("click", () => enable(id));
    return tile;
  }

  /** Live thumbnail shell (caller prepends the video/map). @param {string} id @param {string} label @param {string} title */
  function liveTile(id, label, title) {
    const tile = document.createElement("div");
    tile.className = "cam-tile live";
    tile.title = title;
    tile.addEventListener("click", () => promote(id));
    const tag = document.createElement("span");
    tag.className = "cam-tile-label";
    tag.textContent = label;
    const close = document.createElement("button");
    close.type = "button";
    close.className = "cam-tile-close";
    close.title = `Turn off ${label}`;
    close.textContent = "×";
    close.addEventListener("click", (e) => {
      e.stopPropagation();
      disable(id);
    });
    tile.append(tag, close);
    return tile;
  }

  // Keep each camera thumbnail bound to its stream and reflect liveness (a not-yet-flowing feed shows the
  // "connecting" shimmer). Cheap and idempotent — runs on every session change.
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
      mapWidget?.destroy();
      mapHost?.remove();
      parent.classList.remove("cam-map-primary");
      strip.remove();
    },
  };
}
