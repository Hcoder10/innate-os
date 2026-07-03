"""Shared helpers for loading sim-web's physics worlds into native MuJoCo.

Native mujoco (unlike the @mujoco/mujoco WASM build sim-web uses in the
browser) loads XML + meshes straight from disk, so there's no VFS buffering
step -- just point at the file directly and MuJoCo resolves <mesh file=...>
relative to the XML's own directory.
"""

import queue
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import mujoco
import mujoco.viewer

SIM_WEB = Path(__file__).resolve().parent.parent / "sim-web"
PHYSICS_DIR = SIM_WEB / "public" / "physics"
WORLD_XML = PHYSICS_DIR / "world.xml"

# Matches src/main.ts's SPAWN_X / SPAWN_Y / SPAWN_YAW.
SPAWN_X = -4.34
SPAWN_Y = -0.17
SPAWN_YAW_DEG = -89.8

# Matches src/physics/worker.ts.
SPAWN_DROP_HEIGHT = 0.4
KP_FORWARD = 200.0
KP_LATERAL = 40.0
KP_YAW = 6.0


def load_world(xml_path: Path = WORLD_XML) -> mujoco.MjModel:
    return mujoco.MjModel.from_xml_path(str(xml_path))


def set_spawn(model: mujoco.MjModel, data: mujoco.MjData, x: float, y: float, yaw_deg: float) -> None:
    """Places the robot_base free joint at (x, y, drop-height, yaw) -- same
    convention as worker.ts's setSpawn (qpos[0:3] = xyz, qpos[3:7] = wxyz
    quat, dropped from height rather than spawned flush with the floor)."""
    import numpy as np

    half = np.radians(yaw_deg) / 2
    data.qpos[0] = x
    data.qpos[1] = y
    data.qpos[2] = SPAWN_DROP_HEIGHT
    data.qpos[3] = np.cos(half)
    data.qpos[4] = 0.0
    data.qpos[5] = 0.0
    data.qpos[6] = np.sin(half)
    data.qvel[:6] = 0.0


def apply_drive_force(model: mujoco.MjModel, data: mujoco.MjData, target_vx: float, target_wz: float) -> None:
    """Port of worker.ts's applyDriveForce: PD velocity-servo on the free
    joint, expressed as a world-frame force/torque via xfrc_applied."""
    import numpy as np

    base_id = model.body("robot_base").id
    qw, qz = data.qpos[3], data.qpos[6]
    yaw = 2 * np.arctan2(qz, qw)
    cos, sin = np.cos(yaw), np.sin(yaw)

    vx_world, vy_world = data.qvel[0], data.qvel[1]
    v_forward = vx_world * cos + vy_world * sin
    v_lateral = -vx_world * sin + vy_world * cos
    wz_actual = data.qvel[5]

    force_forward = KP_FORWARD * (target_vx - v_forward)
    force_lateral = -KP_LATERAL * v_lateral
    torque_yaw = KP_YAW * (target_wz - wz_actual)

    data.xfrc_applied[base_id, 0] = force_forward * cos - force_lateral * sin
    data.xfrc_applied[base_id, 1] = force_forward * sin + force_lateral * cos
    data.xfrc_applied[base_id, 5] = torque_yaw


def launch_with_camera(
    model: mujoco.MjModel,
    data: mujoco.MjData,
    lookat: tuple[float, float, float],
    azimuth: float,
    elevation: float,
    distance: float,
    on_ready: Optional[Callable[["mujoco.viewer.Handle"], None]] = None,
) -> None:
    """mujoco.viewer.launch(), but forces the free camera to a specific pose
    right after the model loads, instead of wherever the Simulate GUI's own
    on-load reset puts it -- empirically that doesn't end up framing the
    spawn point the way a model's <statistic>/<visual><global> hints do for
    mjv_defaultFreeCamera() directly (e.g. in our own offscreen renders).

    Getting a live handle to poke cam.* on requires launch()'s private
    handle_return queue (the same mechanism launch_passive() uses) -- but
    mujoco.viewer disallows combining that with run_physics_thread=True
    (plain launch()'s mode, where physics stepping is automatic), so with
    handle_return we have to step physics ourselves in the loop below,
    mirroring viewer.py's own inaccessible-from-here physics thread closely
    enough to keep real-time pacing and the Space-to-pause control working.

    on_ready(handle), if given, fires once the handle is available (before
    the physics loop starts) -- e.g. to stash it somewhere a WebSocket
    command handler on another thread can reach it (drive_mars.py uses this
    to flip handle.opt.geomgroup for a "toggle collision view" command)."""
    handle_return: "queue.Queue[mujoco.viewer.Handle]" = queue.Queue(1)

    def run_physics_and_set_camera() -> None:
        handle = handle_return.get()
        handle.cam.lookat = lookat
        handle.cam.distance = distance
        handle.cam.azimuth = azimuth
        handle.cam.elevation = elevation
        if on_ready is not None:
            on_ready(handle)

        start_wall = time.perf_counter()
        start_sim = data.time
        while handle.is_running():
            sim = handle._get_sim()
            if sim is not None and sim.run:
                target_sim_time = start_sim + (time.perf_counter() - start_wall)
                with handle.lock():
                    steps = 0
                    while data.time < target_sim_time and steps < 500:
                        mujoco.mj_step(model, data)
                        steps += 1
            else:
                start_wall = time.perf_counter()
                start_sim = data.time
            handle.sync()
            time.sleep(0.001)

    threading.Thread(target=run_physics_and_set_camera, daemon=True).start()
    mujoco.viewer._launch_internal(model, data, run_physics_thread=False, handle_return=handle_return)
