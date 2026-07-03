// Drive test sandbox entry point (drive-test.html) -- the real MARS robot
// (URDF, same as the apartment scene) driven with WASD/joystick against
// drive_test_world.xml (flat ground plane + one fixed obstacle cube), no
// apartment mesh. See README "Drive test sandbox". Click-and-drag the robot
// to shove it externally (spring force via xfrc_applied, same technique as
// ufb-studio's drag interaction and the collision test sandbox's cubes).

import "./style.css";
import * as THREE from "three";
import { SimScene } from "./scene";
import { RobotPose } from "./robotPose";
import { joystickToTwist } from "./drive/curve";
import { LocalDriveController } from "./drive/driveController";
import { createKeyboardDrive, createWasdChips } from "./drive/keyboardDrive";
import { createJoystick } from "./drive/joystick";
import { PhysicsController } from "./physics/physicsController";

const DRIVE_TEST_WORLD_URL = "/physics/drive_test_world.xml";
const SPAWN_X = 0;
const SPAWN_Y = 0;
const SPAWN_YAW = 0;
const DRAG_K = 50; // N/m -- spring stiffness, matches the collision test sandbox's drag force

// Must match drive_test_world.xml's fixed "obstacle" geom.
const OBSTACLE_SIZE = 0.15 * 2;
const OBSTACLE_POS: [number, number, number] = [1.0, 0, 0.15];

const canvas = document.getElementById("scene") as HTMLCanvasElement;
const loadingEl = document.getElementById("loading") as HTMLElement;
const loadingMsgEl = document.getElementById("loading-msg") as HTMLElement;
const hudX = document.getElementById("hud-x") as HTMLElement;
const hudY = document.getElementById("hud-y") as HTMLElement;
const hudYaw = document.getElementById("hud-yaw") as HTMLElement;
const hudFps = document.getElementById("hud-fps") as HTMLElement;
const wasdMount = document.getElementById("wasd-mount") as HTMLElement;
const joystickMount = document.getElementById("joystick-mount") as HTMLElement;
const resetBtn = document.getElementById("reset-btn") as HTMLButtonElement;

const scene = new SimScene(canvas);
const pose = new RobotPose(SPAWN_X, SPAWN_Y, SPAWN_YAW);
const drive = new LocalDriveController();
const physics = new PhysicsController();

createWasdChips(wasdMount, createKeyboardDrive(drive));
createJoystick(joystickMount, drive);

// This sandbox has no apartment glb to act as the visual floor, so add a
// plain ground plane + the one fixed obstacle cube directly.
const ground = new THREE.Mesh(
  new THREE.PlaneGeometry(10, 10),
  new THREE.MeshStandardMaterial({ color: 0x2a2d33, roughness: 0.9 }),
);
ground.receiveShadow = true;
scene.scene.add(ground);

const obstacle = new THREE.Mesh(
  new THREE.BoxGeometry(OBSTACLE_SIZE, OBSTACLE_SIZE, OBSTACLE_SIZE),
  new THREE.MeshStandardMaterial({ color: 0xff7319, metalness: 0.2, roughness: 0.6 }),
);
obstacle.position.set(...OBSTACLE_POS);
obstacle.castShadow = true;
obstacle.receiveShadow = true;
scene.scene.add(obstacle);

let snapCamera = true;
physics.onPose = ({ x, y, yaw }) => {
  pose.x = x;
  pose.y = y;
  pose.yaw = yaw;
  if (snapCamera) {
    scene.spawnAt(x, y, yaw);
    snapCamera = false;
  } else {
    scene.setPose(x, y, yaw);
  }
};

resetBtn.addEventListener("click", () => {
  pose.reset();
  snapCamera = true;
  physics.reset({ x: pose.x, y: pose.y, yaw: pose.yaw });
});

// ── Click-and-drag force on the robot ───────────────────────────────────────
// Anchor tracks the robot's live position (offset by where it was clicked)
// each move, so the spring measures the mouse's offset from the grab point
// rather than the robot's total displacement -- see test-main.ts for the
// same technique on the collision test sandbox's cubes.

const drag: { active: boolean; localOffset: THREE.Vector3; plane: THREE.Plane } = {
  active: false,
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
  const point = scene.raycastRobot(x, y);
  if (!point) return;

  drag.active = true;
  drag.localOffset.copy(point).sub(scene.getRobotPosition());
  drag.plane = facingPlaneThrough(point);

  scene.controls.enabled = false;
  canvas.setPointerCapture(e.pointerId);
  scene.showDragVisual(point, point);
});

canvas.addEventListener("pointermove", (e) => {
  if (!drag.active) return;
  const anchor = scene.getRobotPosition().add(drag.localOffset);
  drag.plane = facingPlaneThrough(anchor);

  const { x, y } = ndcFromEvent(e);
  const target = scene.raycastPlane(x, y, drag.plane);
  if (!target) return;

  const force: [number, number, number] = [
    (target.x - anchor.x) * DRAG_K,
    (target.y - anchor.y) * DRAG_K,
    (target.z - anchor.z) * DRAG_K,
  ];
  physics.applyForce([anchor.x, anchor.y, anchor.z], force);
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
  loadingMsgEl.textContent = "Loading robot…";
  await scene.loadRobot();

  loadingMsgEl.textContent = "Starting physics…";
  await physics.init({ x: pose.x, y: pose.y, yaw: pose.yaw }, DRIVE_TEST_WORLD_URL);

  scene.spawnAt(pose.x, pose.y, pose.yaw);
  snapCamera = false;
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

    const { x, y } = drive.active;
    const { linear, angular } = joystickToTwist(x, y);
    physics.step(dt, linear, angular);
    scene.render();

    fpsAccum += dt;
    fpsFrames += 1;
    if (now - fpsLastUpdate > 500) {
      hudFps.textContent = String(Math.round(fpsFrames / fpsAccum));
      fpsAccum = 0;
      fpsFrames = 0;
      fpsLastUpdate = now;
      hudX.textContent = pose.x.toFixed(2);
      hudY.textContent = pose.y.toFixed(2);
      hudYaw.textContent = ((pose.yaw * 180) / Math.PI).toFixed(1);
    }
  }

  requestAnimationFrame(frame);
}

boot().catch((err) => {
  console.error("[sim-web] failed to load drive test scene:", err);
  loadingMsgEl.textContent = `Failed to load: ${(err as Error).message ?? err}`;
});
