// Collision test sandbox entry point (test.html) -- two plain boxes +
// gravity, no robot, no driving. Click-and-drag a cube to push it (spring
// force via xfrc_applied, same technique as ufb-studio's mujoco-worker.js
// drag force). See README "Collision test sandbox".

import "./style.css";
import * as THREE from "three";
import { TestScene } from "./testScene";
import { TestPhysicsController } from "./physics/testPhysicsController";

const DRAG_K = 50; // N/m -- spring stiffness, matches ufb-studio's drag force

const canvas = document.getElementById("scene") as HTMLCanvasElement;
const loadingEl = document.getElementById("loading") as HTMLElement;
const loadingMsgEl = document.getElementById("loading-msg") as HTMLElement;
const hudFps = document.getElementById("hud-fps") as HTMLElement;
const resetBtn = document.getElementById("reset-btn") as HTMLButtonElement;

const scene = new TestScene(canvas);
const physics = new TestPhysicsController();

physics.onPoses = (bodies) => {
  for (const b of bodies) scene.setBodyPose(b.name, b.x, b.y, b.z, b.qw, b.qx, b.qy, b.qz);
};

resetBtn.addEventListener("click", () => physics.reset());

// ── Click-and-drag force ────────────────────────────────────────────────────
// The anchor tracks the grabbed cube's live position (offset by where it was
// clicked) each move, so the spring measures the mouse's offset from the
// grab point rather than the cube's total displacement -- otherwise a cube
// that drifts away from a stationary mouse would feel an ever-growing force.

const drag: {
  active: boolean;
  bodyId: number;
  bodyName: string;
  localOffset: THREE.Vector3;
  plane: THREE.Plane;
} = {
  active: false,
  bodyId: -1,
  bodyName: "",
  localOffset: new THREE.Vector3(),
  plane: new THREE.Plane(),
};

function ndcFromEvent(e: PointerEvent): { x: number; y: number } {
  const rect = canvas.getBoundingClientRect();
  return {
    x: ((e.clientX - rect.left) / rect.width) * 2 - 1,
    y: -((e.clientY - rect.top) / rect.height) * 2 + 1,
  };
}

function facingPlaneThrough(point: THREE.Vector3): THREE.Plane {
  const camDir = new THREE.Vector3();
  scene.camera.getWorldDirection(camDir);
  return new THREE.Plane().setFromNormalAndCoplanarPoint(camDir, point);
}

canvas.addEventListener("pointerdown", (e) => {
  if (e.button !== 0) return;
  const { x, y } = ndcFromEvent(e);
  const pick = scene.pickCube(x, y);
  if (!pick) return;

  drag.active = true;
  drag.bodyId = pick.bodyId;
  drag.bodyName = pick.name;
  const bodyPos = scene.getBodyPosition(pick.name) ?? pick.point;
  drag.localOffset.copy(pick.point).sub(bodyPos);
  drag.plane = facingPlaneThrough(pick.point);

  scene.controls.enabled = false;
  canvas.setPointerCapture(e.pointerId);
  scene.showDragVisual(pick.point, pick.point);
});

canvas.addEventListener("pointermove", (e) => {
  if (!drag.active) return;
  const bodyPos = scene.getBodyPosition(drag.bodyName);
  if (!bodyPos) return;
  const anchor = bodyPos.add(drag.localOffset);
  drag.plane = facingPlaneThrough(anchor);

  const { x, y } = ndcFromEvent(e);
  const target = scene.raycastPlane(x, y, drag.plane);
  if (!target) return;

  const force: [number, number, number] = [
    (target.x - anchor.x) * DRAG_K,
    (target.y - anchor.y) * DRAG_K,
    (target.z - anchor.z) * DRAG_K,
  ];
  physics.applyForce(drag.bodyId, [anchor.x, anchor.y, anchor.z], force);
  scene.showDragVisual(anchor, target);
});

function stopDrag(): void {
  if (!drag.active) return;
  drag.active = false;
  physics.clearForce();
  scene.controls.enabled = true;
  scene.hideDragVisual();
}

canvas.addEventListener("pointerup", stopDrag);
canvas.addEventListener("pointercancel", stopDrag);

// ── Boot + render loop ──────────────────────────────────────────────────────

async function boot(): Promise<void> {
  loadingMsgEl.textContent = "Starting physics…";
  const bodies = await physics.init();
  for (const b of bodies) scene.setBodyId(b.name, b.bodyId);

  loadingEl.classList.add("hidden");
  startLoop();
}

function startLoop(): void {
  let last = performance.now();
  let fpsAccum = 0;
  let fpsFrames = 0;
  let fpsLastUpdate = last;

  function frame(now: number): void {
    requestAnimationFrame(frame);
    const dt = Math.min((now - last) / 1000, 0.1); // clamp for tab-switch stalls
    last = now;

    physics.step(dt);
    scene.render();

    fpsAccum += dt;
    fpsFrames += 1;
    if (now - fpsLastUpdate > 500) {
      hudFps.textContent = String(Math.round(fpsFrames / fpsAccum));
      fpsAccum = 0;
      fpsFrames = 0;
      fpsLastUpdate = now;
    }
  }

  requestAnimationFrame(frame);
}

boot().catch((err) => {
  console.error("[sim-web] failed to load test scene:", err);
  loadingMsgEl.textContent = `Failed to load: ${(err as Error).message ?? err}`;
});
