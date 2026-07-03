// URDF physics sandbox worker -- loads mars.urdf directly into MuJoCo
// (via the mjSpec API: parseXMLString + mjs_addFreeJoint) instead of a
// hand-written box-only MJCF, so every arm/head link's real collision
// geometry (already authored in the URDF) actually participates in
// physics, not just the base. See public/robot/mars.urdf and
// AGENTS.md's URDF description for the link/joint layout.
//
// Base driving reuses worker.ts's PD velocity-servo (same tuned gains).
// Arm/head joints are driven by a separate PD *position* servo per joint,
// commanded by the browser's sliders.
//
// MESSAGE PROTOCOL
// main -> worker: {type:'init', spawn:{x,y,yaw}}
//                 {type:'reset', spawn:{x,y,yaw}}
//                 {type:'step', dt, vx, wz, joints:{[name]: targetRad}}
//                 {type:'apply_force', anchorX, anchorY, anchorZ, fx, fy, fz}
//                 {type:'clear_force'}
// worker -> main: {type:'ready', joints:[{name,lower,upper}], obstacles:[{name,shape,size,pos}]}
//               | {type:'error', message}
//                 {type:'pose', x, y, z, yaw, joints:{[name]: rad}}

import loadMujoco from "@mujoco/mujoco";

const URDF_URL = "/robot/mars.urdf";

// Driven joints, in the order sliders are shown. joint6M (the mirrored
// gripper finger) isn't in this list -- MuJoCo's URDF import doesn't turn
// its <mimic> tag into an equality constraint (confirmed headlessly: model
// compiles with 0 equality constraints), so it's driven programmatically
// as -1x the joint6 target instead (see applyJointServo).
const DRIVEN_JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5", "joint6", "joint_head"] as const;
const MIMIC_JOINT = { name: "joint6M", source: "joint6", multiplier: -1 } as const;
const ALL_ARM_JOINTS = [...DRIVEN_JOINTS, MIMIC_JOINT.name];

// Base drive gains -- see worker.ts for the headless-tuning story (same
// robot base box, same friction).
const KP_FORWARD = 200;
const KP_LATERAL = 40;
const KP_YAW = 6;

// Arm/head PD position-servo gains -- headlessly verified stable and
// reasonably tight-tracking against these tiny (12-150g), heavily-damped
// (URDF <dynamics damping="5">) links.
const KP_JOINT = 8;
const KD_JOINT = 0.6;

const SPAWN_DROP_HEIGHT = 0.4;

// A handful of static obstacles of different sizes/shapes so collisions
// against the real arm geometry (not just the base) can be exercised.
const OBSTACLES: Array<
  | { name: string; shape: "box"; size: [number, number, number]; pos: [number, number, number] }
  | { name: string; shape: "sphere"; size: [number]; pos: [number, number, number] }
  | { name: string; shape: "cylinder"; size: [number, number]; pos: [number, number, number] }
> = [
  { name: "box_small", shape: "box", size: [0.05, 0.05, 0.05], pos: [0.6, 0.4, 0.05] },
  { name: "box_med", shape: "box", size: [0.12, 0.12, 0.12], pos: [0.7, -0.35, 0.12] },
  { name: "box_big", shape: "box", size: [0.25, 0.25, 0.25], pos: [1.3, 0, 0.25] },
  { name: "sphere_med", shape: "sphere", size: [0.1], pos: [0.5, -0.7, 0.1] },
  { name: "cylinder_tall", shape: "cylinder", size: [0.08, 0.3], pos: [1.0, 0.6, 0.3] },
];

let mujoco: Awaited<ReturnType<typeof loadMujoco>>;
let model: InstanceType<typeof mujoco.MjModel>;
let data: InstanceType<typeof mujoco.MjData>;
let baseBodyId = -1;

let targetVx = 0;
let targetWz = 0;
let jointTargets: Record<string, number> = {};

let dragActive = false;
let dragAnchor = { x: 0, y: 0, z: 0 };
let dragForce = { x: 0, y: 0, z: 0 };

interface JointHandle {
  name: string;
  qposAdr: number;
  dofAdr: number;
}
let jointHandles: JointHandle[] = [];
let mimicHandle: JointHandle | null = null;

self.onerror = (event) => {
  self.postMessage({ type: "error", message: String(event) });
};

self.onmessage = async (e: MessageEvent) => {
  const msg = e.data;
  try {
    switch (msg.type) {
      case "init":
        await handleInit(msg.spawn);
        break;
      case "reset":
        handleReset(msg.spawn);
        break;
      case "step":
        handleStep(msg.dt, msg.vx, msg.wz, msg.joints);
        break;
      case "apply_force":
        dragActive = true;
        dragAnchor = { x: msg.anchorX, y: msg.anchorY, z: msg.anchorZ };
        dragForce = { x: msg.fx, y: msg.fy, z: msg.fz };
        break;
      case "clear_force":
        dragActive = false;
        break;
    }
  } catch (err) {
    self.postMessage({ type: "error", message: String((err as Error)?.message ?? err) });
  }
};

async function handleInit(spawn: { x: number; y: number; yaw: number }): Promise<void> {
  mujoco = await loadMujoco();

  const urdfText = await (await fetch(URDF_URL)).text();
  const spec = mujoco.parseXMLString(urdfText);

  const baseBody = mujoco.mjs_findBody(spec, "base_link");
  if (!baseBody) {
    self.postMessage({ type: "error", message: "base_link not found in URDF" });
    return;
  }
  mujoco.mjs_addFreeJoint(baseBody);

  const worldBody = mujoco.mjs_findBody(spec, "world")!;
  const def = mujoco.mjs_getSpecDefault(spec)!;

  const ground = mujoco.mjs_addGeom(worldBody, def)!;
  mujoco.mjs_setName(ground.element, "ground");
  ground.type = mujoco.mjtGeom.mjGEOM_PLANE;
  ground.size[0] = 5;
  ground.size[1] = 5;
  ground.size[2] = 0.1;
  ground.friction[0] = 0.9;
  ground.friction[1] = 0.01;
  ground.friction[2] = 0.001;
  ground.rgba[0] = 0.3;
  ground.rgba[1] = 0.3;
  ground.rgba[2] = 0.32;
  ground.rgba[3] = 1;

  for (const obstacle of OBSTACLES) {
    const geom = mujoco.mjs_addGeom(worldBody, def)!;
    mujoco.mjs_setName(geom.element, obstacle.name);
    geom.pos[0] = obstacle.pos[0];
    geom.pos[1] = obstacle.pos[1];
    geom.pos[2] = obstacle.pos[2];
    geom.friction[0] = 0.9;
    geom.friction[1] = 0.01;
    geom.friction[2] = 0.001;
    geom.rgba[0] = 1;
    geom.rgba[1] = 0.45;
    geom.rgba[2] = 0.1;
    geom.rgba[3] = 1;
    if (obstacle.shape === "box") {
      geom.type = mujoco.mjtGeom.mjGEOM_BOX;
      geom.size[0] = obstacle.size[0];
      geom.size[1] = obstacle.size[1];
      geom.size[2] = obstacle.size[2];
    } else if (obstacle.shape === "sphere") {
      geom.type = mujoco.mjtGeom.mjGEOM_SPHERE;
      geom.size[0] = obstacle.size[0];
    } else {
      geom.type = mujoco.mjtGeom.mjGEOM_CYLINDER;
      geom.size[0] = obstacle.size[0];
      geom.size[1] = obstacle.size[1];
    }
  }

  model = mujoco.mj_compile(spec);
  data = new mujoco.MjData(model);
  baseBodyId = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY.value, "base_link");
  console.log(
    `[physics] loaded URDF: nbody=${model.nbody} njnt=${model.njnt} nq=${model.nq} ngeom=${model.ngeom} baseBodyId=${baseBodyId}`,
  );
  if (baseBodyId < 0) {
    self.postMessage({ type: "error", message: "base_link body not found in compiled model" });
    return;
  }

  const jointInfo: Array<{ name: string; lower: number; upper: number }> = [];
  jointHandles = [];
  for (const name of ALL_ARM_JOINTS) {
    const jid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT.value, name);
    if (jid < 0) continue;
    const handle: JointHandle = { name, qposAdr: model.jnt_qposadr[jid], dofAdr: model.jnt_dofadr[jid] };
    if (name === MIMIC_JOINT.name) {
      mimicHandle = handle;
    } else {
      jointHandles.push(handle);
      jointInfo.push({ name, lower: model.jnt_range[jid * 2], upper: model.jnt_range[jid * 2 + 1] });
      jointTargets[name] = 0;
    }
  }

  setSpawn(spawn);
  mujoco.mj_forward(model, data);
  self.postMessage({ type: "ready", joints: jointInfo, obstacles: OBSTACLES });
}

function handleReset(spawn: { x: number; y: number; yaw: number }): void {
  if (!model) return;
  mujoco.mj_resetData(model, data);
  for (const name of Object.keys(jointTargets)) jointTargets[name] = 0;
  setSpawn(spawn);
  mujoco.mj_forward(model, data);
  postPose();
}

function setSpawn(spawn: { x: number; y: number; yaw: number }): void {
  const qpos = data.qpos;
  const half = spawn.yaw / 2;
  qpos[0] = spawn.x;
  qpos[1] = spawn.y;
  qpos[2] = SPAWN_DROP_HEIGHT;
  qpos[3] = Math.cos(half); // qw
  qpos[4] = 0; // qx
  qpos[5] = 0; // qy
  qpos[6] = Math.sin(half); // qz
  const qvel = data.qvel;
  for (let i = 0; i < model.nv; i++) qvel[i] = 0;
}

let stepCount = 0;
let diverged = false;

function handleStep(dt: number, vx: number, wz: number, joints: Record<string, number> = {}): void {
  if (!model || diverged) return;
  targetVx = vx;
  targetWz = wz;
  for (const [name, value] of Object.entries(joints)) {
    if (name in jointTargets) jointTargets[name] = value;
  }

  const subStep = model.opt.timestep;
  const steps = Math.max(1, Math.round(dt / subStep));
  for (let i = 0; i < steps; i++) {
    applyBaseDrive();
    applyJointServo();
    mujoco.mj_step(model, data);
  }
  stepCount++;

  const qpos = data.qpos;
  if (!Number.isFinite(qpos[0]) || !Number.isFinite(qpos[2])) {
    diverged = true;
    self.postMessage({
      type: "error",
      message: `simulation diverged at step ${stepCount} (ncon=${data.ncon})`,
    });
    return;
  }

  postPose();
}

function applyBaseDrive(): void {
  const qpos = data.qpos;
  const qvel = data.qvel;

  const qw = qpos[3];
  const qz = qpos[6];
  const yaw = 2 * Math.atan2(qz, qw);

  const cos = Math.cos(yaw);
  const sin = Math.sin(yaw);
  const vxWorld = qvel[0];
  const vyWorld = qvel[1];

  const vForward = vxWorld * cos + vyWorld * sin;
  const vLateral = -vxWorld * sin + vyWorld * cos;
  const wzActual = qvel[5];

  const forceForward = KP_FORWARD * (targetVx - vForward);
  const forceLateral = -KP_LATERAL * vLateral;
  const torqueYaw = KP_YAW * (targetWz - wzActual);

  const xfrc = data.xfrc_applied;
  const base = baseBodyId * 6;
  xfrc[base + 0] = forceForward * cos - forceLateral * sin;
  xfrc[base + 1] = forceForward * sin + forceLateral * cos;
  xfrc[base + 2] = 0;
  xfrc[base + 3] = 0;
  xfrc[base + 4] = 0;
  xfrc[base + 5] = torqueYaw;

  if (dragActive) {
    const xipos = data.xipos;
    const rx = dragAnchor.x - xipos[baseBodyId * 3 + 0];
    const ry = dragAnchor.y - xipos[baseBodyId * 3 + 1];
    const rz = dragAnchor.z - xipos[baseBodyId * 3 + 2];
    xfrc[base + 0] += dragForce.x;
    xfrc[base + 1] += dragForce.y;
    xfrc[base + 2] += dragForce.z;
    xfrc[base + 3] += ry * dragForce.z - rz * dragForce.y;
    xfrc[base + 4] += rz * dragForce.x - rx * dragForce.z;
    xfrc[base + 5] += rx * dragForce.y - ry * dragForce.x;
  }
}

function applyJointServo(): void {
  const qpos = data.qpos;
  const qvel = data.qvel;
  const qfrc = data.qfrc_applied;

  for (const joint of jointHandles) {
    const target = jointTargets[joint.name] ?? 0;
    const q = qpos[joint.qposAdr];
    const v = qvel[joint.dofAdr];
    qfrc[joint.dofAdr] = KP_JOINT * (target - q) - KD_JOINT * v;
  }

  if (mimicHandle) {
    const target = MIMIC_JOINT.multiplier * (jointTargets[MIMIC_JOINT.source] ?? 0);
    const q = qpos[mimicHandle.qposAdr];
    const v = qvel[mimicHandle.dofAdr];
    qfrc[mimicHandle.dofAdr] = KP_JOINT * (target - q) - KD_JOINT * v;
  }
}

function postPose(): void {
  const qpos = data.qpos;
  const qw = qpos[3];
  const qz = qpos[6];
  const joints: Record<string, number> = {};
  for (const joint of jointHandles) joints[joint.name] = qpos[joint.qposAdr];
  if (mimicHandle) joints[mimicHandle.name] = qpos[mimicHandle.qposAdr];

  self.postMessage({
    type: "pose",
    x: qpos[0],
    y: qpos[1],
    z: qpos[2],
    yaw: 2 * Math.atan2(qz, qw),
    joints,
  });
}
