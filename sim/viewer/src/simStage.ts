// SimStage — the webapp's primary "camera panel" in simulation: a real,
// mounted Three.js canvas (full container resolution, drag-to-orbit like a
// classic three.js app), NOT a video element. Replaces videoStage when the
// robot is simulated; the root keeps the .video-stage class so the webapp's
// layout and map-primary CSS behave identically.
//
// The stage owns ALL rendering for its SimSession: the primary view is drawn
// full-res every frame; live thumbnail views (cameraSwitch tiles) are
// scissor-rendered into a corner of the same canvas every few frames and
// blitted out to the session's captureStream canvases -- one GL context, no
// duplicated scene resources.

import { SimScene, type CameraView } from "./scene";
import { THUMB_H, THUMB_W, type SimSession } from "./simSession";

// One PiP tile refresh per N rendered frames, round-robin: each refresh is an
// extra scene render + a canvas-to-canvas composite (cheap now that the
// captureStream pipeline is gone -- THAT was what pinned the page to 15fps).
// N=2 with two live tiles gives each ~30fps while costing at most half an
// extra scene render per frame.
const THUMB_FRAME_DIV = 2;

// Render cap: on a 120Hz display an uncapped rAF loop doubles the GPU/CPU
// cost of the page for no visible gain (state arrives at ~75Hz and is
// interpolated), and on a loaded machine that pressure comes straight back
// as scheduling jitter everywhere -- including the world server's physics
// cadence. 60fps is indistinguishable here and halves the load.
const MIN_FRAME_MS = 1000 / 62;

const VIEW_FOR: Record<string, CameraView> = { main: "main", arm: "arm", orbit: "orbit" };

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

  // Sim-only debug overlays: small toggle chips over the canvas.
  const chips = document.createElement("div");
  chips.style.cssText = "position:absolute;left:10px;bottom:10px;display:flex;gap:6px;z-index:5;";
  const OFF_BG = "rgba(0,0,0,.45)";
  const ON_BG = "rgba(0,255,136,.22)";
  const addChip = (label: string, onToggle: (on: boolean) => void) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = label;
    b.style.cssText =
      // No backdrop-filter: blurring over an animated canvas forces a
      // re-blur pass every frame the canvas changes (fps drops while moving).
      `padding:4px 10px;border-radius:999px;border:1px solid rgba(255,255,255,.25);background:${OFF_BG};` +
      "color:rgba(255,255,255,.75);font:500 11px system-ui;cursor:pointer;";
    let on = false;
    b.onclick = () => {
      on = !on;
      b.style.background = on ? ON_BG : OFF_BG;
      b.style.color = on ? "#7dffc4" : "rgba(255,255,255,.75)";
      onToggle(on);
    };
    chips.appendChild(b);
  };
  addChip("lidar", (on) => session.setLidarVisible(on));
  addChip("collisions", (on) => session.setCollisionHullsVisible(on));
  wrap.appendChild(chips);

  // Tiny frame-time readout (?simperf in the URL): median/p95 of the last
  // second, so render regressions are measurable instead of guessed at.
  let perfEl: HTMLElement | null = null;
  let frameTimes: number[] = [];
  let perfNextAt = 0;
  let longTaskMs = 0;
  const bare = new URLSearchParams(location.search).has("simbare");
  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) longTaskMs += e.duration;
    }).observe({ type: "longtask", buffered: false });
  } catch {
    /* longtask unsupported -- HUD just shows 0 */
  }
  if (new URLSearchParams(location.search).has("simperf")) {
    perfEl = document.createElement("div");
    perfEl.style.cssText =
      "position:absolute;right:10px;bottom:10px;z-index:5;padding:3px 8px;border-radius:6px;" +
      "background:rgba(0,0,0,.6);color:#9f9;font:11px ui-monospace,monospace;pointer-events:none;";
    wrap.appendChild(perfEl);
  }

  const scene = new SimScene(canvas, { fixedSize: { width: parent.clientWidth || 1280, height: parent.clientHeight || 720 } });
  scene.followCamera = true;

  const resize = () => {
    const w = wrap.clientWidth || 1280;
    const h = wrap.clientHeight || 720;
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

  (async () => {
    try {
      await scene.loadApartment();
      await scene.loadRobot();
      session.stageReady();
      if (!disposed) raf = requestAnimationFrame(loop);
    } catch (err) {
      session.stageError(err);
    }
  })();

  return {
    audioEl: null, // sim has no robot mic; the pages skip the mic toggle in sim mode
    destroy() {
      disposed = true;
      cancelAnimationFrame(raf);
      observer.disconnect();
      wrap.remove();
    },
  };
}
