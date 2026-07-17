// SimStage — the webapp's camera panel in simulation: a mounted Three.js
// canvas (drag-to-orbit), not a video element; keeps the .video-stage class
// so the webapp's CSS behaves identically. Owns all rendering: primary view
// full-res every frame, PiP thumbnails scissor-rendered from the same GL
// context and blitted out.

import * as THREE from "three";
import { SimScene, type CameraView } from "./scene";
import { LoadQueue } from "./loadQueue";
import { THUMB_H, THUMB_W, type SimSession } from "./simSession";

// One PiP tile refresh per N rendered frames, round-robin: ~30fps per tile
// at most half an extra scene render per frame.
const THUMB_FRAME_DIV = 2;

// 60fps render cap: uncapped 120Hz rAF doubles the page's GPU/CPU for no
// visible gain (~75Hz interpolated state) and the load jitters everything.
const MIN_FRAME_MS = 1000 / 62;

const VIEW_FOR: Record<string, CameraView> = { main: "main", arm: "arm", orbit: "orbit" };

// Droppable props offered by the placement picker; kind matches world.py
// OBJECT_KINDS and scene.ts OBJECTS.
const DROP_KINDS: { kind: string; label: string }[] = [
  { kind: "human", label: "🧍" },
  { kind: "soccer_ball", label: "⚽" },
  { kind: "labrador", label: "🐕" },
];

export function createSimStage(parent: HTMLElement, session: SimSession): { audioEl: null; destroy: () => void } {
  const wrap = document.createElement("div");
  wrap.className = "video-stage"; // reuse the webapp's stage styling/CSS ladder
  wrap.style.position = "relative";
  const canvas = document.createElement("canvas");
  canvas.style.width = "100%";
  canvas.style.height = "100%";
  canvas.style.display = "block";
  wrap.appendChild(canvas);
  parent.appendChild(wrap);

  // Sim-only debug stack (toggle chips + optional perf HUD), bottom-left just
  // above the webapp's WASD overlay -- the corners and top-center belong to
  // other overlays (arm panel, cam tiles, telemetry, agent status).
  const debugStack = document.createElement("div");
  debugStack.className = "sim-debug-stack"; // pages hide it where it collides
  debugStack.style.cssText =
    "position:absolute;left:14px;bottom:132px;display:flex;flex-direction:column;gap:8px;z-index:5;";
  wrap.appendChild(debugStack);
  const chips = document.createElement("div");
  chips.style.cssText = "display:flex;gap:6px;";
  const OFF_BG = "rgba(0,0,0,.45)";
  const ON_BG = "rgba(0,255,136,.22)";
  const addChip = (label: string, onToggle: (on: boolean) => void): ((on: boolean) => void) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = label;
    b.style.cssText =
      // No backdrop-filter: re-blurring over an animated canvas costs fps.
      `padding:4px 10px;border-radius:999px;border:1px solid rgba(255,255,255,.25);background:${OFF_BG};` +
      "color:rgba(255,255,255,.75);font:500 11px system-ui;cursor:pointer;";
    let on = false;
    const set = (next: boolean) => {
      on = next;
      b.style.background = on ? ON_BG : OFF_BG;
      b.style.color = on ? "#7dffc4" : "rgba(255,255,255,.75)";
    };
    b.onclick = () => {
      set(!on);
      onToggle(on);
    };
    chips.appendChild(b);
    return set;
  };
  addChip("lidar", (on) => session.setLidarVisible(on));
  addChip("collisions", (on) => session.setCollisionHullsVisible(on));
  // Drop-object picker: one chip per prop (world.py OBJECT_KINDS). Selecting a
  // kind arms placement for it; the others disarm (mutually exclusive). A
  // stepping stone toward per-challenge UI -- for now it just seeds the world.
  const dropLabel = document.createElement("span");
  dropLabel.textContent = "drop";
  dropLabel.style.cssText = "align-self:center;color:rgba(255,255,255,.5);font:500 11px system-ui;margin-left:4px;";
  chips.appendChild(dropLabel);
  const dropChipSetters = new Map<string, (on: boolean) => void>();
  for (const { kind, label } of DROP_KINDS) {
    dropChipSetters.set(
      kind,
      addChip(label, (on) => armDrop(on ? kind : null)),
    );
  }
  debugStack.appendChild(chips);

  // Loading indicator: a compact pill holding a progress bar, centered just
  // below the camera thumbnail strip (webapp's .cam-strip pins tiles at the top
  // edge, ~14px + 120px tall) so the two don't overlap. The canvas shows
  // immediately underneath (robot first, then rooms stream in), not a full
  // black block. Fades out when the download finishes. pointer-events:none so
  // it never shields the stage; the bar re-enables them for its hover.
  const loading = document.createElement("div");
  loading.style.cssText =
    "position:absolute;top:150px;left:50%;transform:translateX(-50%);z-index:6;pointer-events:none;" +
    "display:flex;flex-direction:column;align-items:center;gap:8px;padding:12px 18px;border-radius:12px;" +
    "background:rgba(0,0,0,.42);transition:opacity .5s ease;";
  const bar = document.createElement("div");
  bar.style.cssText =
    "width:min(280px,60%);height:6px;border-radius:999px;background:rgba(255,255,255,.12);" +
    "overflow:hidden;transition:background .2s;pointer-events:auto;";
  const barFill = document.createElement("div");
  barFill.style.cssText = "height:100%;width:0%;background:#7dffc4;border-radius:999px;transition:width .3s ease;";
  bar.appendChild(barFill);
  bar.onmouseenter = () => (bar.style.background = "rgba(255,255,255,.22)");
  bar.onmouseleave = () => (bar.style.background = "rgba(255,255,255,.12)");
  const loadingLabel = document.createElement("div");
  loadingLabel.style.cssText = "color:rgba(255,255,255,.6);font:500 13px system-ui;";
  const readout = document.createElement("div");
  readout.style.cssText = "color:rgba(255,255,255,.35);font:500 11px ui-monospace,monospace;";
  loading.append(bar, loadingLabel, readout);
  wrap.appendChild(loading);

  const setLoading = (text: string) => (loadingLabel.textContent = text);
  const mb = (bytes: number) => (bytes / 1e6).toFixed(1);
  const setProgress = (loaded: number, total: number) => {
    barFill.style.width = `${total > 0 ? Math.min(100, (loaded / total) * 100) : 0}%`;
    readout.textContent = `${mb(loaded)} / ${mb(total)} MB`;
  };
  const failLoading = (text: string) => {
    barFill.style.background = "#ff9f9f";
    loadingLabel.style.color = "#ff9f9f";
    loadingLabel.textContent = text;
  };
  const hideLoading = () => {
    loading.style.opacity = "0";
    loading.style.pointerEvents = "none"; // never shield the stage while fading
    loading.addEventListener("transitionend", () => loading.remove(), { once: true });
    // transitionend never fires under prefers-reduced-motion (the webapp
    // disables all transitions) or when the fade starts pre-paint -- without
    // this fallback the invisible overlay stayed and ate every click.
    setTimeout(() => loading.remove(), 700);
  };
  // The scrim fades when the download finishes (see the load sequence below);
  // here we only surface load failures.
  const unsubscribe = session.onChange((s) => {
    if (s.status === "error") {
      failLoading("simulation view failed to load — see the browser console");
      unsubscribe();
    }
  });

  // ?simperf: frame-time readout (median/p95 of the last second).
  let perfEl: HTMLElement | null = null;
  let frameTimes: number[] = [];
  let perfNextAt = 0;
  let longTaskMs = 0;
  let longTaskObserver: PerformanceObserver | null = null;
  const bare = new URLSearchParams(location.search).has("simbare");
  try {
    longTaskObserver = new PerformanceObserver((list) => {
      for (const e of list.getEntries()) longTaskMs += e.duration;
    });
    longTaskObserver.observe({ type: "longtask", buffered: false });
  } catch {
    /* longtask unsupported -- HUD just shows 0 */
  }
  if (new URLSearchParams(location.search).has("simperf")) {
    perfEl = document.createElement("div");
    perfEl.style.cssText =
      "align-self:flex-start;padding:3px 8px;border-radius:6px;" +
      "background:rgba(0,0,0,.6);color:#9f9;font:11px ui-monospace,monospace;pointer-events:none;";
    debugStack.prepend(perfEl);
  }

  const scene = new SimScene(canvas, { fixedSize: { width: parent.clientWidth || 1280, height: parent.clientHeight || 720 } });
  scene.followCamera = true;

  // Drop-object placement: while a kind is armed the orbit controls are paused
  // -- press marks the spot on the floor, drag points the yaw (arrow preview,
  // the human's head), release drops that body there and physics settles it.
  let armedKind: string | null = null;
  let dropStart: THREE.Vector3 | null = null;
  let dropArrow: THREE.ArrowHelper | null = null;
  const clearDropDrag = () => {
    dropStart = null;
    if (dropArrow) {
      scene.scene.remove(dropArrow);
      dropArrow.dispose();
      dropArrow = null;
    }
  };
  const armDrop = (kind: string | null) => {
    armedKind = kind;
    scene.placementMode = kind !== null;
    canvas.style.cursor = kind !== null ? "crosshair" : "";
    for (const [k, set] of dropChipSetters) set(k === kind); // reflect the armed kind, disarm the rest
    if (kind === null) clearDropDrag();
  };
  canvas.addEventListener("pointerdown", (e) => {
    if (armedKind === null || e.button !== 0) return;
    dropStart = scene.screenToFloor(e.clientX, e.clientY);
  });
  canvas.addEventListener("pointermove", (e) => {
    if (!dropStart) return;
    const cur = scene.screenToFloor(e.clientX, e.clientY);
    if (!cur) return;
    const drag = cur.sub(dropStart).setZ(0);
    if (drag.length() < 0.05) return;
    if (!dropArrow) {
      dropArrow = new THREE.ArrowHelper(drag.clone().normalize(), dropStart.clone().setZ(0.05), 1, 0xff8800, 0.25, 0.15);
      scene.scene.add(dropArrow);
    }
    dropArrow.setDirection(drag.clone().normalize());
    dropArrow.setLength(Math.max(drag.length(), 0.4), 0.25, 0.15);
  });
  canvas.addEventListener("pointerup", (e) => {
    if (armedKind === null || !dropStart) return;
    const end = scene.screenToFloor(e.clientX, e.clientY);
    const drag = end ? end.sub(dropStart).setZ(0) : new THREE.Vector3();
    // An identity drop faces +y (the human's head); the drag vector is that direction.
    const yaw = drag.length() > 0.2 ? Math.atan2(drag.y, drag.x) - Math.PI / 2 : 0;
    session.dropObject(armedKind, dropStart.x, dropStart.y, yaw);
    armDrop(null);
  });

  const resize = () => {
    const w = wrap.clientWidth;
    const h = wrap.clientHeight;
    if (!w || !h) return; // hidden (map primary): keep the last real size
    scene.setRenderSize(w, h, Math.min(devicePixelRatio, 2));
  };
  const observer = new ResizeObserver(resize);
  observer.observe(wrap);
  resize();

  let raf = 0;
  let frame = 0;
  let thumbCursor = 0;
  let lastTime = performance.now();
  let disposed = false;

  const loop = (now: number) => {
    raf = requestAnimationFrame(loop);
    if (now - lastTime < MIN_FRAME_MS - 1) return; // 120Hz display -> render every other vsync
    const dt = Math.min((now - lastTime) / 1000, 0.1);
    lastTime = now;
    session.tick(scene, dt);

    // Thumbnails first (scissor corner renders, blitted out), one tile per
    // slot -- see THUMB_FRAME_DIV.
    if (!bare && frame % THUMB_FRAME_DIV === 0) {
      const live = session.liveThumbnails();
      if (live.length) {
        const { index, name } = live[thumbCursor++ % live.length];
        scene.setView(VIEW_FOR[name] ?? "orbit");
        scene.renderRegion(0, 0, THUMB_W, THUMB_H);
        // renderRegion speaks logical px; the canvas backing store is
        // scaled by the pixel ratio -- blit the full physical region or
        // the tile shows a zoomed-in crop.
        const ratio = scene.renderer.getPixelRatio();
        session.blitThumbnail(index, canvas, THUMB_W * ratio, THUMB_H * ratio);
      }
    }
    // ...then the primary view full-frame on top.
    scene.setView(VIEW_FOR[session.primaryCamera] ?? "orbit");
    scene.render();
    frame++;

    if (perfEl) {
      frameTimes.push(performance.now() - now);
      if (now >= perfNextAt) {
        perfNextAt = now + 1000;
        const sorted = [...frameTimes].sort((x, y) => x - y);
        const med = sorted[sorted.length >> 1] ?? 0;
        const p95 = sorted[Math.floor(sorted.length * 0.95)] ?? 0;
        const lag = session.pipelineLag;
        const lagTxt = lag ? `  lag ${lag.curMs.toFixed(0)}ms (min ${lag.minMs.toFixed(0)})` : "";
        perfEl.textContent = `js ${med.toFixed(1)}/${p95.toFixed(1)}ms  lt ${longTaskMs.toFixed(0)}ms  ${frameTimes.length}fps${lagTxt}`;
        frameTimes = [];
        longTaskMs = 0;
      }
    }
  };

  // One shared bounded queue drives real byte progress for the whole load;
  // seed an estimate so the bar has a width before Content-Lengths arrive
  // (apartment ~35 MB + robot ~7 MB), refined as real sizes land.
  const queue = new LoadQueue(2, ({ loaded, total }) => setProgress(loaded, total));
  queue.setEstimatedTotal(42e6);
  (async () => {
    try {
      // Start rendering + accept poses right away: the worldstate socket is
      // already connecting (session.start), so the robot's placeholder box
      // snaps to its real spawn pose while the STLs stream, then the mesh
      // replaces it. Bail at each await if the stage was destroyed mid-load
      // (SPA remount) -- else we'd mutate a disposed scene.
      session.stageReady();
      raf = requestAnimationFrame(loop);
      // The apartment manifest first (a few KB, unqueued): it draws every
      // room's placeholder box and frames the camera on them, so the first
      // frames show the apartment's wireframe layout rather than an empty
      // void while the meshes are still being fetched.
      setLoading("loading layout...");
      const layout = await scene.loadApartmentLayout();
      if (disposed) return;
      scene.frameLayout(layout);
      setLoading("loading robot and apartment...");
      // Await once: the robot's STLs are now in the shared queue. Enqueue the
      // apartment rooms after, so they land behind the robot (deterministic
      // robot-first, no timing guess).
      const { done: robotDone } = await scene.loadRobot(queue);
      if (disposed) return;
      const apartment = scene.streamApartment(queue, layout);
      // Await twice: the rest of both loads.
      await Promise.all([robotDone, apartment]);
      if (disposed) return;
      hideLoading();
    } catch (err) {
      if (!disposed) session.stageError(err);
    }
  })();

  return {
    audioEl: null, // sim has no robot mic; the pages skip the mic toggle in sim mode
    destroy() {
      disposed = true;
      queue.cancel(); // stop pulling new downloads for a stage that's gone
      unsubscribe();
      cancelAnimationFrame(raf);
      observer.disconnect();
      longTaskObserver?.disconnect();
      scene.dispose();
      wrap.remove();
    },
  };
}
