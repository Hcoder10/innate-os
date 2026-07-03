// Physics worker -- runs MuJoCo WASM off the main thread.
//
// "Simple torque" drive experiment: the robot base has no wheel joints
// modeled yet (see AGENTS.md / mars.urdf), so instead of real wheel
// friction/actuation this represents the base as a free-floating box and
// pushes it with an applied world-frame force/torque, PD-servoed toward the
// commanded (linear, angular) velocity -- a stand-in for "the wheels are
// driving it forward" that still gets real gravity/contact against the
// apartment's collision mesh. Swap for modeled wheels later without
// touching the message protocol below.
//
// MESSAGE PROTOCOL
// main -> worker: {type:'init', spawn:{x,y,yaw}, worldUrl?}
//                 {type:'reset', spawn:{x,y,yaw}}
//                 {type:'step', dt, vx, wz}
//                 {type:'apply_force', anchorX, anchorY, anchorZ, fx, fy, fz}
//                 {type:'clear_force'}
// worker -> main: {type:'ready'} | {type:'error', message}
//                 {type:'pose', x, y, z, yaw}

import loadMujoco from "@mujoco/mujoco";

const DEFAULT_WORLD_XML_URL = "/physics/world.xml";

// Free joint qpos = [x, y, z, qw, qx, qy, qz]; qvel = [vx, vy, vz, wx, wy, wz]
// (every world this worker loads has exactly one joint in the whole model,
// so these are always the first 7 / 6 entries -- no name lookup needed).
const QPOS_FREE = 0;
const QVEL_FREE = 0;
const SPAWN_DROP_HEIGHT = 0.4; // meters -- see setSpawn

// PD velocity-servo gains, applied as a world-frame force/torque at the
// base's center of mass (mujoco's xfrc_applied).
//
// KP_FORWARD was 25 (max force at MAX_LINEAR=0.4 m/s: 10 N), which is well
// under the ~26.5 N of static friction the 3 kg/mu=0.9 box needs to
// overcome before it slides at all -- confirmed headlessly (see
// sim-web collision-test debugging): the box just sat there. 200 (max
// force 80 N) reaches a steady ~0.3 m/s without the instability seen at
// higher gains (300 made the box hop off the ground).
//
// KP_YAW was 1.2, similarly too weak: a flat box resting on 4 corners needs
// real torque to overcome the sliding friction at each corner as it spins
// (not just "torsional" friction, which only applies at condim>=4) -- ~3.5
// N*m worth for this box, so 1.2 N*m*(rad/s) never got anywhere (confirmed
// headlessly: wz stalled below 0.05 rad/s from a 1.0 target). But cranking
// it past ~10 hits a hard numerical-instability cliff: at KP_YAW=12 the
// step below diverges outright (z shoots into the thousands within
// seconds) -- this is very likely what "spins wildly then respawns" in the
// apartment actually was. 6 sits safely in the stable window (headlessly
// verified with simultaneous forward+turn commands).
const KP_FORWARD = 200; // N per (m/s) forward-velocity error
const KP_LATERAL = 40; // N per (m/s) lateral-velocity error (damping only -- suppresses sideways sliding, approximating a non-holonomic base)
const KP_YAW = 6; // N*m per (rad/s) yaw-rate error

let mujoco: Awaited<ReturnType<typeof loadMujoco>>;
let model: InstanceType<typeof mujoco.MjModel>;
let data: InstanceType<typeof mujoco.MjData>;
let baseBodyId = -1;

let targetVx = 0;
let targetWz = 0;

// External drag force (see ./testWorker.ts for the same pattern): persists
// at a fixed anchor/force until cleared, added on top of the drive PD
// force each sub-step rather than replacing it.
let dragActive = false;
let dragAnchor = { x: 0, y: 0, z: 0 };
let dragForce = { x: 0, y: 0, z: 0 };

// Catch anything that escapes the try/catch below (e.g. a WASM trap) so a
// worker crash is never silent.
self.onerror = (event) => {
  self.postMessage({ type: "error", message: String(event) });
};

self.onmessage = async (e: MessageEvent) => {
  const msg = e.data;
  try {
    switch (msg.type) {
      case "init":
        await handleInit(msg.spawn, msg.worldUrl);
        break;
      case "reset":
        handleReset(msg.spawn);
        break;
      case "step":
        handleStep(msg.dt, msg.vx, msg.wz);
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

async function handleInit(
  spawn: { x: number; y: number; yaw: number },
  worldUrl: string = DEFAULT_WORLD_XML_URL,
): Promise<void> {
  mujoco = await loadMujoco();

  const xmlText = await (await fetch(worldUrl)).text();
  const meshFiles = [...xmlText.matchAll(/<mesh file="([^"]+)"/g)].map((m) => m[1]);
  const meshBaseUrl = worldUrl.slice(0, worldUrl.lastIndexOf("/") + 1);

  const vfs = new mujoco.MjVFS();
  for (const file of meshFiles) {
    const buf = await (await fetch(`${meshBaseUrl}${file}`)).arrayBuffer();
    vfs.addBuffer(file, new Uint8Array(buf));
  }

  model = mujoco.MjModel.from_xml_string(xmlText, vfs);
  vfs.delete();
  data = new mujoco.MjData(model);
  baseBodyId = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY.value, "robot_base");
  console.log(
    `[physics] loaded ${worldUrl}: nbody=${model.nbody} nmesh=${model.nmesh} baseBodyId=${baseBodyId}`,
  );
  if (baseBodyId < 0) {
    self.postMessage({ type: "error", message: "robot_base body not found in compiled model" });
    return;
  }

  setSpawn(spawn);
  mujoco.mj_forward(model, data);
  console.log(
    `[physics] after settle: qpos=${Array.from({ length: 7 }, (_, i) => data.qpos[i].toFixed(3))} ncon=${data.ncon}`,
  );
  self.postMessage({ type: "ready" });
}

function handleReset(spawn: { x: number; y: number; yaw: number }): void {
  if (!model) return;
  mujoco.mj_resetData(model, data);
  setSpawn(spawn);
  mujoco.mj_forward(model, data);
  postPose();
}

function setSpawn(spawn: { x: number; y: number; yaw: number }): void {
  const qpos = data.qpos;
  const half = spawn.yaw / 2;
  qpos[QPOS_FREE + 0] = spawn.x;
  qpos[QPOS_FREE + 1] = spawn.y;
  // Dropped from height rather than spawned right at the floor: if the
  // apartment collision mesh's floor isn't exactly where we expect (e.g. a
  // frame-conversion mismatch), a clean fall makes that obvious (z keeps
  // dropping, never settles) instead of an instant, violent unstick from an
  // already-interpenetrating spawn.
  qpos[QPOS_FREE + 2] = SPAWN_DROP_HEIGHT;
  qpos[QPOS_FREE + 3] = Math.cos(half); // qw
  qpos[QPOS_FREE + 4] = 0; // qx
  qpos[QPOS_FREE + 5] = 0; // qy
  qpos[QPOS_FREE + 6] = Math.sin(half); // qz
  const qvel = data.qvel;
  for (let i = 0; i < 6; i++) qvel[QVEL_FREE + i] = 0;
}

let stepCount = 0;
let diverged = false;

function handleStep(dt: number, vx: number, wz: number): void {
  if (!model || diverged) return;
  targetVx = vx;
  targetWz = wz;

  const subStep = model.opt.timestep;
  const steps = Math.max(1, Math.round(dt / subStep));
  for (let i = 0; i < steps; i++) {
    applyDriveForce();
    mujoco.mj_step(model, data);
  }
  stepCount++;

  const qpos = data.qpos;
  if (!Number.isFinite(qpos[QPOS_FREE + 0]) || !Number.isFinite(qpos[QPOS_FREE + 2])) {
    diverged = true;
    self.postMessage({
      type: "error",
      message: `simulation diverged at step ${stepCount} (ncon=${data.ncon}) -- likely bad initial contact or missing floor collision`,
    });
    return;
  }

  // Throttled trace so we can see z settle (or not) and contact count in the
  // browser console without flooding it every frame.
  if (stepCount % 60 === 0) {
    console.log(
      `[physics] step=${stepCount} z=${qpos[QPOS_FREE + 2].toFixed(3)} ncon=${data.ncon}`,
    );
  }

  postPose();
}

function applyDriveForce(): void {
  const qpos = data.qpos;
  const qvel = data.qvel;

  const qw = qpos[QPOS_FREE + 3];
  const qz = qpos[QPOS_FREE + 6];
  const yaw = 2 * Math.atan2(qz, qw); // upright-only quat -> yaw

  const cos = Math.cos(yaw);
  const sin = Math.sin(yaw);
  const vxWorld = qvel[QVEL_FREE + 0];
  const vyWorld = qvel[QVEL_FREE + 1];

  // World-frame velocity resolved into the base's forward/lateral axes.
  const vForward = vxWorld * cos + vyWorld * sin;
  const vLateral = -vxWorld * sin + vyWorld * cos;
  const wzActual = qvel[QVEL_FREE + 5];

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
    // Torque computed about the body's center of mass (xipos), matching
    // ufb-studio's drag-force implementation (see also testWorker.ts).
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

function postPose(): void {
  const qpos = data.qpos;
  const qw = qpos[QPOS_FREE + 3];
  const qz = qpos[QPOS_FREE + 6];
  self.postMessage({
    type: "pose",
    x: qpos[QPOS_FREE + 0],
    y: qpos[QPOS_FREE + 1],
    z: qpos[QPOS_FREE + 2],
    yaw: 2 * Math.atan2(qz, qw),
  });
}
