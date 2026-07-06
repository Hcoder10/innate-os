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

const THUMB_FRAME_DIV = 3;

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
      `padding:4px 10px;border-radius:999px;border:1px solid rgba(255,255,255,.25);background:${OFF_BG};` +
      "color:rgba(255,255,255,.75);font:500 11px system-ui;cursor:pointer;backdrop-filter:blur(6px);";
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
  let lastTime = performance.now();
  let disposed = false;

  const loop = (now: number) => {
    raf = requestAnimationFrame(loop);
    const dt = Math.min((now - lastTime) / 1000, 0.1);
    lastTime = now;
    session.tick(scene, dt);

    // Thumbnails first (scissor corner renders, blitted out)...
    if (frame % THUMB_FRAME_DIV === 0) {
      for (const { index, name } of session.liveThumbnails()) {
        scene.setView(VIEW_FOR[name] ?? "orbit");
        scene.renderRegion(0, 0, THUMB_W, THUMB_H);
        session.blitThumbnail(index, canvas, THUMB_W, THUMB_H);
      }
    }
    // ...then the primary view full-frame on top.
    scene.setView(VIEW_FOR[session.primaryCamera] ?? "orbit");
    scene.render();
    frame++;
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
