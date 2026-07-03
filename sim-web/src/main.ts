// Mode A entry point: fully standalone — no ROS, no rosbridge. WASD/joystick
// drives the robot's kinematic pose directly in the browser.

import "./style.css";
import { SimScene, type CameraView } from "./scene";
import { RobotPose } from "./robotPose";
import { joystickToTwist } from "./drive/curve";
import { LocalDriveController } from "./drive/driveController";
import { createKeyboardDrive, createWasdChips } from "./drive/keyboardDrive";
import { createJoystick } from "./drive/joystick";
import { ApartmentPhysicsController } from "./physics/apartmentPhysicsController";
import { RosbridgePhysicsController } from "./physics/rosbridgeController";
import { DynamixelLeader, type LeaderArmState } from "./teleop/dynamixel";
import { leaderTicksToJointRadians, LEADER_JOINT_NAMES } from "./teleop/leaderArmMapping";
import { ARM_HOME_POSITIONS } from "./robot/armHome";

const SPAWN_X = -4.34;
const SPAWN_Y = -0.17;
const SPAWN_YAW = (-89.8 * Math.PI) / 180;

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
const collisionDebugToggle = document.getElementById("collision-debug-toggle") as HTMLInputElement;
const sdfCollisionToggle = document.getElementById("sdf-collision-toggle") as HTMLInputElement;
const viewSelect = document.getElementById("view-select") as HTMLSelectElement;
const jointSlidersEl = document.getElementById("joint-sliders") as HTMLElement;
const gainKpInput = document.getElementById("gain-kp") as HTMLInputElement;
const gainKdInput = document.getElementById("gain-kd") as HTMLInputElement;
const gainEffortInput = document.getElementById("gain-effort") as HTMLInputElement;

const scene = new SimScene(canvas);
const pose = new RobotPose(SPAWN_X, SPAWN_Y, SPAWN_YAW);
const drive = new LocalDriveController();

// Connected mode ("Mode B"): ?ros or ?ros=ws://host:9090 mirrors the real
// innate-os graph (virtual MARS driver) over rosbridge instead of stepping
// MuJoCo WASM locally. Default URL matches docker-compose's published port.
const rosParam = new URLSearchParams(location.search).get("ros");
// Default to the host serving this page, so LAN devices reach the same
// rosbridge the dev machine does (requires SIM_ROSBRIDGE_BIND=0.0.0.0).
const ROSBRIDGE_URL = rosParam === "" ? `ws://${location.hostname}:9090` : rosParam;

let physics: ApartmentPhysicsController | RosbridgePhysicsController = ROSBRIDGE_URL
  ? new RosbridgePhysicsController(ROSBRIDGE_URL)
  : new ApartmentPhysicsController();
if (ROSBRIDGE_URL) {
  const label = document.querySelector<HTMLElement>(".hud-title .microlabel");
  if (label) label.textContent = "connected";

  // Lidar overlay: /scan only exists in connected mode.
  const row = document.getElementById("lidar-toggle-row") as HTMLElement;
  const toggle = document.getElementById("lidar-toggle") as HTMLInputElement;
  row.hidden = false;
  (physics as RosbridgePhysicsController).onScan = (points) => scene.setLidarPoints(points);
  toggle.addEventListener("change", () => scene.setLidarVisible(toggle.checked));

  // Robot camera feed PiP: the actual MuJoCo-rendered frames the brain
  // receives, not the local Three.js view.
  const feedRow = document.getElementById("camera-feed-row") as HTMLElement;
  const feedToggle = document.getElementById("camera-feed-toggle") as HTMLInputElement;
  const feedSelect = document.getElementById("camera-feed-select") as HTMLSelectElement;
  const feedImg = document.getElementById("camera-feed") as HTMLImageElement;
  feedRow.hidden = false;
  const ros = physics as RosbridgePhysicsController;
  ros.onFrame = (dataUri) => (feedImg.src = dataUri);
  const updateFeed = () => {
    ros.setCameraFeed(feedToggle.checked ? feedSelect.value : null);
    feedImg.hidden = !feedToggle.checked;
  };
  feedToggle.addEventListener("change", updateFeed);
  feedSelect.addEventListener("change", updateFeed);
}

createWasdChips(wasdMount, createKeyboardDrive(drive));
createJoystick(joystickMount, drive);

// ── Joint sliders ────────────────────────────────────────────────────────
// The full mars.urdf is loaded into MuJoCo (see apartmentWorker.ts), so
// sliders drive real PD-servoed joint targets sent to the physics worker
// every step; the resulting simulated joint angles come back via
// physics.onPose and get applied to the URDF visual (see below).
const jointTargets: Record<string, number> = {};
const sliderElements: Record<string, { input: HTMLInputElement; numberInput: HTMLInputElement }> = {};

function buildSliders(joints: { name: string; lower: number; upper: number }[]): void {
  for (const joint of joints) {
    const home = ARM_HOME_POSITIONS[joint.name] ?? 0;
    jointTargets[joint.name] = home;

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
    input.value = String(home);

    const numberInput = document.createElement("input");
    numberInput.type = "number";
    numberInput.className = "slider-value";
    numberInput.min = String(joint.lower);
    numberInput.max = String(joint.upper);
    numberInput.step = "0.01";
    numberInput.value = home.toFixed(2);

    function setValue(v: number): void {
      const clamped = Math.max(joint.lower, Math.min(joint.upper, v));
      jointTargets[joint.name] = clamped;
      input.value = String(clamped);
      numberInput.value = clamped.toFixed(2);
    }

    input.addEventListener("input", () => setValue(parseFloat(input.value)));
    numberInput.addEventListener("change", () => {
      const v = parseFloat(numberInput.value);
      setValue(Number.isFinite(v) ? v : 0);
    });

    row.append(label, input, numberInput);
    jointSlidersEl.appendChild(row);
    sliderElements[joint.name] = { input, numberInput };
  }
}

// ── Arm/head PD gains -- live-tunable (see apartmentWorker.ts) ───────────
function sendGains(): void {
  physics.setGains(parseFloat(gainKpInput.value), parseFloat(gainKdInput.value), parseFloat(gainEffortInput.value));
}
gainKpInput.addEventListener("input", sendGains);
gainKdInput.addEventListener("input", sendGains);
gainEffortInput.addEventListener("input", sendGains);

// ── Leader-arm (USB) teleop ──────────────────────────────────────────────
// Ported from webapp/js/dynamixel.js + armPanel.js -- the same WebSerial
// leader arm the operator webapp reads to teleop the real robot. There's no
// rosbridge here, so engaging just writes straight into jointTargets
// (joints 1-6 -- the leader has no 7th DOF, so joint_head stays
// slider-controlled) instead of publishing to /leader_positions.
function setupLeaderArm(): void {
  const statusEl = document.getElementById("leader-status") as HTMLElement;
  const connectBtn = document.getElementById("leader-connect-btn") as HTMLButtonElement;
  const engageBtn = document.getElementById("leader-engage-btn") as HTMLButtonElement;

  if (!navigator.serial) {
    statusEl.textContent = "leader arm needs Chrome/Edge (WebSerial)";
    connectBtn.disabled = true;
    return;
  }
  const serial = navigator.serial;

  const leader = new DynamixelLeader();
  let engaged = false;

  function setJointSlidersEnabled(enabled: boolean): void {
    for (const name of LEADER_JOINT_NAMES) {
      const slider = sliderElements[name];
      if (!slider) continue;
      slider.input.disabled = !enabled;
      slider.numberInput.disabled = !enabled;
    }
  }

  function applyLeaderPositions(ticks: number[]): void {
    // Already unwrapped/clamped to the real joint limits inside leaderTicksToJointRadians.
    const radians = leaderTicksToJointRadians(ticks);
    for (const [name, value] of Object.entries(radians)) {
      const slider = sliderElements[name];
      if (!slider) continue;
      jointTargets[name] = value;
      slider.input.value = String(value);
      slider.numberInput.value = value.toFixed(2);
    }
  }

  function setEngaged(on: boolean): void {
    if (engaged === on) return;
    engaged = on;
    setJointSlidersEnabled(!on);
    render(leader.state);
  }

  function render(state: LeaderArmState): void {
    const reading = state.connected && state.positions !== null;
    statusEl.classList.toggle("warn", !!state.error);
    if (state.error) statusEl.textContent = state.error;
    else if (!state.connected) statusEl.textContent = "no leader arm";
    else statusEl.textContent = reading ? `${state.rate} Hz` : "listening…";

    connectBtn.hidden = state.connected;
    connectBtn.disabled = false;
    connectBtn.textContent = state.error ? "Reconnect leader arm" : "Connect leader arm";

    engageBtn.hidden = !reading;
    engageBtn.textContent = engaged ? "Live — click to stop" : "Engage follow";
    engageBtn.classList.toggle("active", engaged);
  }

  async function openPort(port: SerialPort): Promise<void> {
    connectBtn.disabled = true;
    connectBtn.textContent = "Opening…";
    try {
      await leader.open(port);
    } catch (err) {
      render({ ...leader.state, error: err instanceof Error ? err.message : "Couldn't open port" });
    }
  }

  connectBtn.addEventListener("click", async () => {
    try {
      // QinHeng CH9102 USB-serial bridge on the leader arm, same VID/PID
      // filter as webapp/js/teleop/armPanel.js.
      const port = await serial.requestPort({ filters: [{ usbVendorId: 0x1a86, usbProductId: 0x55d3 }] });
      await openPort(port);
    } catch {
      render(leader.state); // chooser dismissed -- not an error
    }
  });

  engageBtn.addEventListener("click", () => setEngaged(!engaged));

  leader.onChange((state) => {
    if (engaged && (!state.connected || state.error)) {
      setEngaged(false);
    } else if (engaged && state.positions) {
      applyLeaderPositions(state.positions);
    }
    render(state);
  });

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") setEngaged(false);
  });

  // Previously granted port (or one plugged in later) attaches by itself.
  serial.addEventListener("connect", (e) => {
    if (!leader.state.connected && e.target) void openPort(e.target as SerialPort);
  });
  void serial.getPorts().then((ports) => {
    if (ports[0] && !leader.state.connected) void openPort(ports[0]);
  });
}
setupLeaderArm();

// "Simple torque" experiment: the base has no wheels modeled yet, so physics
// pushes the full URDF's base_link toward the commanded velocity (see
// apartmentWorker.ts) rather than integrating pose directly. Every pose the
// physics worker reports gets forwarded to the scene here; snapCamera is
// true right after (re)spawning so the camera chase-snaps instead of
// smoothly following the teleport.
let snapCamera = true;

// Connected mode: /odom + /joint_states arrive at ~30 Hz, well below render
// rate -- applying them directly staircases the motion. Store them as a
// target and lerp toward it every frame instead.
const netTarget = { x: SPAWN_X, y: SPAWN_Y, yaw: SPAWN_YAW, joints: {} as Record<string, number>, live: false };
const shownJoints: Record<string, number> = {};

function attachPoseHandler(): void {
  physics.onPose = ({ x, y, yaw, joints }) => {
    if (ROSBRIDGE_URL) {
      Object.assign(netTarget, { x, y, yaw, live: true });
      Object.assign(netTarget.joints, joints);
      if (snapCamera) {
        pose.x = x;
        pose.y = y;
        pose.yaw = yaw;
        Object.assign(shownJoints, joints);
        scene.spawnAt(x, y, yaw);
        snapCamera = false;
      }
      return;
    }
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
}
attachPoseHandler();

/** Per-frame smoothing toward the latest network sample (connected mode). */
function applyNetPose(dt: number): void {
  if (!netTarget.live || snapCamera) return;
  const alpha = 1 - Math.exp(-dt * 15);
  pose.x += (netTarget.x - pose.x) * alpha;
  pose.y += (netTarget.y - pose.y) * alpha;
  const dyaw = Math.atan2(Math.sin(netTarget.yaw - pose.yaw), Math.cos(netTarget.yaw - pose.yaw));
  pose.yaw += dyaw * alpha;
  for (const [name, target] of Object.entries(netTarget.joints)) {
    shownJoints[name] = (shownJoints[name] ?? target) + (target - (shownJoints[name] ?? target)) * alpha;
  }
  scene.setPose(pose.x, pose.y, pose.yaw);
  scene.setJointAngles(shownJoints);
}

resetBtn.addEventListener("click", () => {
  pose.reset();
  snapCamera = true;
  physics.reset({ x: pose.x, y: pose.y, yaw: pose.yaw });

  for (const [name, slider] of Object.entries(sliderElements)) {
    const home = ARM_HOME_POSITIONS[name] ?? 0;
    jointTargets[name] = home;
    slider.input.value = String(home);
    slider.numberInput.value = home.toFixed(2);
  }
});

collisionDebugToggle.addEventListener("change", () => {
  scene.setCollisionDebugVisible(collisionDebugToggle.checked);
});

// Robot-mounted camera views (see scene.ts's ROBOT_CAMERA_MOUNTS -- same
// frames/FOV as the genesis sim's main + wrist cameras).
viewSelect.addEventListener("change", () => {
  scene.setView(viewSelect.value as CameraView);
});

// Switching collision mode needs a fresh model compile, so the physics
// worker is torn down and rebuilt at the robot's current pose (see
// apartmentWorker.ts's collision:"sdf" -- native octree SDFs of the
// watertight room shells instead of the CoACD convex hulls). Standalone
// only: in connected mode the physics lives in the driver, not here.
if (ROSBRIDGE_URL) sdfCollisionToggle.disabled = true;
sdfCollisionToggle.addEventListener("change", async () => {
  if (ROSBRIDGE_URL) return;
  const mode = sdfCollisionToggle.checked ? "sdf" : "hulls";
  sdfCollisionToggle.disabled = true;
  loadingEl.classList.remove("hidden");
  loadingMsgEl.textContent = `Rebuilding physics (${mode} collision)…`;
  try {
    physics.dispose();
    physics = new ApartmentPhysicsController();
    attachPoseHandler();
    snapCamera = true;
    await physics.init({ x: pose.x, y: pose.y, yaw: pose.yaw }, mode);
    sendGains();
    await scene.setCollisionDebugSource(mode);
  } finally {
    loadingEl.classList.add("hidden");
    sdfCollisionToggle.disabled = false;
  }
});

async function boot(): Promise<void> {
  loadingMsgEl.textContent = "Loading apartment scene…";
  await scene.loadApartment();

  loadingMsgEl.textContent = "Loading collision mesh…";
  await scene.loadCollisionDebug();
  scene.setCollisionDebugVisible(collisionDebugToggle.checked);

  loadingMsgEl.textContent = "Loading robot…";
  await scene.loadRobot();
  for (const option of [...viewSelect.options]) {
    if (!scene.availableViews.includes(option.value as CameraView)) option.remove();
  }

  loadingMsgEl.textContent = "Starting physics…";
  const { joints } = await physics.init({ x: pose.x, y: pose.y, yaw: pose.yaw });
  buildSliders(joints);
  sendGains();

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
    if (ROSBRIDGE_URL) applyNetPose(dt);
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
  console.error("[sim-web] failed to load scene:", err);
  loadingMsgEl.textContent = `Failed to load: ${(err as Error).message ?? err}`;
});
