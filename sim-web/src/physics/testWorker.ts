// Collision test sandbox worker -- runs test_world.xml (two plain boxes +
// a ground plane, see that file). Deliberately has none of the apartment
// worker's driving/PD-servo logic (see worker.ts): gravity does all the
// work, and the only external input is an optional drag force so you can
// click-and-drag a cube to push it (same technique as ufb-studio's
// mujoco-worker.js: xfrc_applied at an anchor point, torque computed about
// the body's center of mass via xipos).
//
// MESSAGE PROTOCOL
// main -> worker: {type:'init'}
//                 {type:'reset'}
//                 {type:'step', dt}
//                 {type:'apply_force', bodyId, anchorX, anchorY, anchorZ, fx, fy, fz}
//                 {type:'clear_force'}
// worker -> main: {type:'ready', bodies: [{name, bodyId}]} | {type:'error', message}
//                 {type:'pose', bodies: [{name,x,y,z,qw,qx,qy,qz}]}

import loadMujoco from "@mujoco/mujoco";

const WORLD_XML_URL = "/physics/test_world.xml";

interface FreeBody {
  name: string;
  bodyId: number;
  qposAdr: number;
}

let mujoco: Awaited<ReturnType<typeof loadMujoco>>;
let model: InstanceType<typeof mujoco.MjModel>;
let data: InstanceType<typeof mujoco.MjData>;
let freeBodies: FreeBody[] = [];
let forceBodyId = -1; // body with an active drag force; -1 = none

self.onerror = (event) => {
  self.postMessage({ type: "error", message: String(event) });
};

self.onmessage = async (e: MessageEvent) => {
  const msg = e.data;
  try {
    switch (msg.type) {
      case "init":
        await handleInit();
        break;
      case "reset":
        handleReset();
        break;
      case "step":
        handleStep(msg.dt);
        break;
      case "apply_force":
        handleApplyForce(msg);
        break;
      case "clear_force":
        handleClearForce();
        break;
    }
  } catch (err) {
    self.postMessage({ type: "error", message: String((err as Error)?.message ?? err) });
  }
};

async function handleInit(): Promise<void> {
  mujoco = await loadMujoco();

  const xmlText = await (await fetch(WORLD_XML_URL)).text();
  const vfs = new mujoco.MjVFS();
  model = mujoco.MjModel.from_xml_string(xmlText, vfs);
  vfs.delete();
  data = new mujoco.MjData(model);

  freeBodies = [];
  for (let j = 0; j < model.njnt; j++) {
    if (model.jnt_type[j] !== mujoco.mjtJoint.mjJNT_FREE.value) continue;
    const bodyId = model.jnt_bodyid[j];
    const name = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY.value, bodyId) || `body${bodyId}`;
    freeBodies.push({ name, bodyId, qposAdr: model.jnt_qposadr[j] });
  }

  mujoco.mj_forward(model, data);
  self.postMessage({
    type: "ready",
    bodies: freeBodies.map((b) => ({ name: b.name, bodyId: b.bodyId })),
  });
}

function handleReset(): void {
  if (!model) return;
  handleClearForce();
  mujoco.mj_resetData(model, data); // restores qpos0, i.e. test_world.xml's initial pose
  mujoco.mj_forward(model, data);
  postPose();
}

let stepCount = 0;
let diverged = false;

function handleStep(dt: number): void {
  if (!model || diverged) return;

  const subStep = model.opt.timestep;
  const steps = Math.max(1, Math.round(dt / subStep));
  for (let i = 0; i < steps; i++) mujoco.mj_step(model, data);
  stepCount++;

  const finite = freeBodies.every((b) => Number.isFinite(data.qpos[b.qposAdr]));
  if (!finite) {
    diverged = true;
    self.postMessage({
      type: "error",
      message: `simulation diverged at step ${stepCount} (ncon=${data.ncon})`,
    });
    return;
  }

  postPose();
}

function handleApplyForce(msg: {
  bodyId: number;
  anchorX: number;
  anchorY: number;
  anchorZ: number;
  fx: number;
  fy: number;
  fz: number;
}): void {
  if (!model || msg.bodyId < 0 || msg.bodyId >= model.nbody) return;

  // Torque computed about the body's center of mass (xipos), matching
  // ufb-studio's drag-force implementation.
  const xipos = data.xipos;
  const cx = xipos[msg.bodyId * 3 + 0];
  const cy = xipos[msg.bodyId * 3 + 1];
  const cz = xipos[msg.bodyId * 3 + 2];
  const rx = msg.anchorX - cx;
  const ry = msg.anchorY - cy;
  const rz = msg.anchorZ - cz;

  const xfrc = data.xfrc_applied;
  const base = msg.bodyId * 6;
  xfrc[base + 0] = msg.fx;
  xfrc[base + 1] = msg.fy;
  xfrc[base + 2] = msg.fz;
  xfrc[base + 3] = ry * msg.fz - rz * msg.fy;
  xfrc[base + 4] = rz * msg.fx - rx * msg.fz;
  xfrc[base + 5] = rx * msg.fy - ry * msg.fx;
  forceBodyId = msg.bodyId;
}

function handleClearForce(): void {
  if (!model || forceBodyId < 0) return;
  const base = forceBodyId * 6;
  for (let i = 0; i < 6; i++) data.xfrc_applied[base + i] = 0;
  forceBodyId = -1;
}

function postPose(): void {
  const qpos = data.qpos;
  const bodies = freeBodies.map((b) => ({
    name: b.name,
    x: qpos[b.qposAdr + 0],
    y: qpos[b.qposAdr + 1],
    z: qpos[b.qposAdr + 2],
    qw: qpos[b.qposAdr + 3],
    qx: qpos[b.qposAdr + 4],
    qy: qpos[b.qposAdr + 5],
    qz: qpos[b.qposAdr + 6],
  }));
  self.postMessage({ type: "pose", bodies });
}
