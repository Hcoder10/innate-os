// URDF physics sandbox entry point (urdf-test.html) -- the real MARS robot
// loaded directly into MuJoCo from its URDF (see urdfWorker.ts), so the
// arm/head links' own collision boxes are simulated too, not just the
// base. WASD/joystick drives the base; sliders (built from the joint
// list the worker reports on init, so they always match the URDF's real
// limits) drive the arm/head via a per-joint PD position servo. Obstacles
// of a few different sizes/shapes let you test the arm bumping into
// things. Click-and-drag the robot to shove it externally, same as
// drive-test-main.ts.

import "./style.css";
import * as THREE from "three";
import { SimScene } from "./scene";
import { RobotPose } from "./robotPose";
import { joystickToTwist } from "./drive/curve";
import { LocalDriveController } from "./drive/driveController";
import { createKeyboardDrive, createWasdChips } from "./drive/keyboardDrive";
import { createJoystick } from "./drive/joystick";
import { UrdfPhysicsController, type ObstacleInfo } from "./physics/urdfPhysicsController";

const SPAWN_X = 0;
const SPAWN_Y = 0;
const SPAWN_YAW = 0;
const DRAG_K = 50; // N/m -- spring stiffness, matches the other sandboxes' drag force

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
const slidersEl = document.getElementById("sliders") as HTMLElement;

const scene = new SimScene(canvas);
const pose = new RobotPose(SPAWN_X, SPAWN_Y, SPAWN_YAW);
const drive = new LocalDriveController();
const physics = new UrdfPhysicsController();

createWasdChips(wasdMount, createKeyboardDrive(drive));
createJoystick(joystickMount, drive);

// ── Joint sliders ────────────────────────────────────────────────────────
// jointTargets is read every physics step; slider input just mutates it.
const jointTargets: Record<string, number> = {};

function buildSliders(joints: { name: string; lower: number; upper: number }[]): void {
  for (const joint of joints) {
    jointTargets[joint.name] = 0;

    const row = document.createElement("div");
    row.className = "slider-row";

    const label = document.createElement("label");
    label.textContent = joint.name;
    label.htmlFor = `slider-${joint.name}`;

    const input = document.createElement("input");
    input.type = "range";
    input.id = `slider-${joint.name}`;
    input.min = String(joint.lower);
    input.max = String(joint.upper);
    input.step = "0.01";
    input.value = "0";

    const valueEl = document.createElement("span");
    valueEl.className = "slider-value";
    valueEl.textContent = "0.00";

    input.addEventListener("input", () => {
      const v = parseFloat(input.value);
      jointTargets[joint.name] = v;
      valueEl.textContent = v.toFixed(2);
    });

    row.append(label, input, valueEl);
    slidersEl.appendChild(row);
  }
}

// ── Static obstacles (rendered to match whatever the worker reports) ──────
function addObstacles(obstacles: ObstacleInfo[]): void {
  const material = new THREE.MeshStandardMaterial({ color: 0xff7319, metalness: 0.2, roughness: 0.6 });
  for (const obstacle of obstacles) {
    let geometry: THREE.BufferGeometry;
    if (obstacle.shape === "box") {
      const [hx, hy, hz] = obstacle.size;
      geometry = new THREE.BoxGeometry(hx * 2, hy * 2, hz * 2);
    } else if (obstacle.shape === "sphere") {
      geometry = new THREE.SphereGeometry(obstacle.size[0], 24, 16);
    } else {
      const [radius, halfHeight] = obstacle.size;
      geometry = new THREE.CylinderGeometry(radius, radius, halfHeight * 2, 24);
      geometry.rotateX(Math.PI / 2); // MuJoCo cylinders stand along Z, three's along Y
    }
    const mesh = new THREE.Mesh(geometry, material);
    mesh.position.set(...obstacle.pos);
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    scene.scene.add(mesh);
  }
}

// This sandbox has no apartment glb to act as the visual floor.
const ground = new THREE.Mesh(
  new THREE.PlaneGeometry(10, 10),
  new THREE.MeshStandardMaterial({ color: 0x2a2d33, roughness: 0.9 }),
);
ground.receiveShadow = true;
scene.scene.add(ground);

let snapCamera = true;
physics.onPose = ({ x, y, yaw, joints }) => {
  pose.x = x;
  pose.y = y;
  pose.yaw = yaw;
  if (snapCamera) {
    scene.spawnAt(x, y, yaw);
    snapCamera = false;
  } else {
    scene.setPose(x, y, yaw);
  }
  scene.setJointAngles(joints);
};

resetBtn.addEventListener("click", () => {
  pose.reset();
  snapCamera = true;
  for (const name of Object.keys(jointTargets)) jointTargets[name] = 0;
  for (const input of slidersEl.querySelectorAll<HTMLInputElement>("input[type=range]")) input.value = "0";
  for (const valueEl of slidersEl.querySelectorAll<HTMLElement>(".slider-value")) valueEl.textContent = "0.00";
  physics.reset({ x: pose.x, y: pose.y, yaw: pose.yaw });
});

// ── Click-and-drag force on the robot (same technique as drive-test-main.ts) ──

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
  const { joints, obstacles } = await physics.init({ x: pose.x, y: pose.y, yaw: pose.yaw });
  buildSliders(joints);
  addObstacles(obstacles);

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
    physics.step(dt, linear, angular, jointTargets);
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
  console.error("[sim-web] failed to load urdf test scene:", err);
  loadingMsgEl.textContent = `Failed to load: ${(err as Error).message ?? err}`;
});
