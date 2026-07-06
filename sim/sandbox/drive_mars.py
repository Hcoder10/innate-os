"""Drive the real Mars URDF robot (base + arm + head, real collision
geometry) around the apartment collision mesh in MuJoCo's native interactive
viewer, controlled live from a tiny browser control panel (WASD keys or an
on-screen joystick) over a local WebSocket.

This is a native-MuJoCo sandbox for testing drive feel against the fixed
apartment collision mesh (see sim/tools + README.md) before porting anything
back to sim/viewer's WASM worker. All model building (room meshes, world
XML, URDF attach with a planar base, geom styling, drive gains) comes from
mars_sim_driver.world, shared with the driver node.

No live keyboard capture in the MuJoCo window itself -- mjpython on macOS
runs the GUI on the main thread and exposes no key callbacks. Driving input
instead comes over the network from control_panel.html, which sidesteps that
limitation entirely since it's not going through mujoco.viewer's own input
handling.

Usage:
    uv run sandbox/drive_mars.py [sim/assets/apartment_split_v2]
Then open http://localhost:8766/control_panel.html in a browser -- it
connects to ws://localhost:8765 automatically.
"""

import contextlib
import http.server
import json
import sys
import threading
from collections.abc import Callable
from functools import partial
from pathlib import Path

import _driver_pkg  # noqa: F401
import mujoco
import mujoco.viewer
import numpy as np
import websockets
import websockets.sync.server
from common import SPAWN_X, SPAWN_Y, SPAWN_YAW_DEG, launch_with_camera
from mars_sim_driver import world
from mars_sim_driver.world import (
    ARM_HOME,
    DRIVEN_JOINTS,
    KP_FORWARD,
    KP_LATERAL,
    KP_YAW,
    MAX_BASE_ANGULAR_SPEED,
    MAX_BASE_LINEAR_SPEED,
    MAX_LINEAR,
    MAX_YAW,
    MIMIC_JOINT,
)

WS_PORT = 8765
HTTP_PORT = 8766

# Arm/head PD position-servo gains -- this sandbox's own placeholder servo
# feel (the driver node uses stiffer gains, see mars_sim_driver.core).
KP_JOINT = 8.0
KD_JOINT = 0.6


def build_model(rooms: dict[str, list[Path]], visual_rooms: dict[str, Path] | None = None) -> mujoco.MjModel:
    world_xml = world.build_world_xml(rooms, include_placeholder_robot=False, visual_rooms=visual_rooms)
    world_spec = mujoco.MjSpec.from_string(world_xml)
    robot_spec = world.load_robot_spec(world.default_urdf_path())
    world.add_planar_base(robot_spec)

    frame = world_spec.worldbody.add_frame()
    world_spec.attach(robot_spec, frame=frame, prefix="robot_")
    model = world_spec.compile()
    world.style_robot_geoms(model)
    # With z pinned by the planar base, the ground's contact margin reads as
    # permanent penetration whose friction glues the base -- zero it.
    model.geom("ground").margin = 0.0
    # Brighten toward sim/viewer's look (ambient/hemisphere fill there); the
    # interactive viewer can't tone-map, so lighting is the only lever.
    model.vis.headlight.ambient[:] = 0.5
    model.vis.headlight.diffuse[:] = 0.5
    return model


class DriveState:
    """vx/wz targets set by the WebSocket server thread, read every physics
    step by the control callback on the viewer's own thread. Plain
    float-pair assignment is safe enough under the GIL without a lock.

    handle and on_respawn are wired up from main() once the viewer has
    loaded (see launch_with_camera's on_ready) so the WebSocket thread can
    act on "respawn"/"toggle_collision" commands directly, guarded by the
    handle's own lock rather than routing through the physics step."""

    def __init__(self) -> None:
        self.vx = 0.0
        self.wz = 0.0
        self.handle: mujoco.viewer.Handle | None = None
        self.on_respawn: Callable[[], None] | None = None

    def set(self, vx: float, wz: float) -> None:
        self.vx = max(-1.0, min(1.0, vx)) * MAX_LINEAR
        self.wz = max(-1.0, min(1.0, wz)) * MAX_YAW

    def respawn(self) -> None:
        if self.on_respawn is not None:
            self.on_respawn()

    def toggle_collision(self) -> None:
        if self.handle is None:
            return
        opt = self.handle.opt
        opt.geomgroup[world.VISUAL_GROUP], opt.geomgroup[world.COLLISION_GROUP] = (
            opt.geomgroup[world.COLLISION_GROUP],
            opt.geomgroup[world.VISUAL_GROUP],
        )


def run_ws_server(state: DriveState) -> None:
    def handler(ws: websockets.sync.server.ServerConnection) -> None:
        print("control panel connected")
        for message in ws:
            try:
                cmd = json.loads(message)
            except (ValueError, TypeError, json.JSONDecodeError):
                continue
            if cmd.get("cmd") == "respawn":
                state.respawn()
            elif cmd.get("cmd") == "toggle_collision":
                state.toggle_collision()
            else:
                try:
                    state.set(float(cmd.get("vx", 0.0)), float(cmd.get("wz", 0.0)))
                except (ValueError, TypeError):
                    pass

    with websockets.sync.server.serve(handler, "localhost", WS_PORT) as server:
        print(f"WebSocket control server listening on ws://localhost:{WS_PORT}")
        server.serve_forever()


def run_http_server() -> None:
    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(Path(__file__).resolve().parent))
    with http.server.ThreadingHTTPServer(("localhost", HTTP_PORT), handler) as server:
        print(f"Control panel served at http://localhost:{HTTP_PORT}/control_panel.html")
        server.serve_forever()


def main() -> None:
    split_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else world.default_assets_dir() / "apartment_split_v2"
    rooms = world.find_decomposed_rooms(split_dir)
    if not rooms:
        raise SystemExit(f"no <room>/<room>_collision_*.obj found under {split_dir}")

    visual_dir = world.default_assets_dir() / "apartment_visual"
    visual_rooms = world.find_visual_rooms(visual_dir) if visual_dir.is_dir() else {}
    if visual_rooms:
        print(f"Loaded {len(visual_rooms)} textured room(s) -- press '3' in the viewer to toggle collision hulls.")
    else:
        print(
            f"No textured rooms found under {visual_dir} -- run export_visual_rooms.py first for the textured render."
        )

    model = build_model(rooms, visual_rooms)
    data = mujoco.MjData(model)
    print(f"nbody={model.nbody} njnt={model.njnt} ngeom={model.ngeom} nmesh={model.nmesh}")

    base_id = model.body("robot_base_link").id
    base_adr = {}  # "x"/"y"/"yaw" -> (qpos_adr, dof_adr)
    for short in ("x", "y", "yaw"):
        jid = model.joint(f"robot_base_{short}").id
        base_adr[short] = (model.jnt_qposadr[jid], model.jnt_dofadr[jid])

    def set_spawn_pose() -> None:
        data.qpos[base_adr["x"][0]] = SPAWN_X
        data.qpos[base_adr["y"][0]] = SPAWN_Y
        data.qpos[base_adr["yaw"][0]] = np.radians(SPAWN_YAW_DEG)
        for name, (qadr, _dadr) in joint_dofs.items():
            data.qpos[qadr] = ARM_HOME[name]
        data.qpos[mimic_qpos_adr] = MIMIC_JOINT[2] * ARM_HOME[MIMIC_JOINT[1]]

    joint_dofs: dict[str, tuple[int, int]] = {}
    for name in DRIVEN_JOINTS:
        jid = model.joint(f"robot_{name}").id
        joint_dofs[name] = (model.jnt_qposadr[jid], model.jnt_dofadr[jid])
    mimic_name, mimic_source, mimic_mult = MIMIC_JOINT
    mimic_jid = model.joint(f"robot_{mimic_name}").id
    mimic_qpos_adr, mimic_dof_adr = model.jnt_qposadr[mimic_jid], model.jnt_dofadr[mimic_jid]

    set_spawn_pose()
    mujoco.mj_forward(model, data)

    state = DriveState()
    threading.Thread(target=run_ws_server, args=(state,), daemon=True).start()
    threading.Thread(target=run_http_server, daemon=True).start()

    def control_callback(m: mujoco.MjModel, d: mujoco.MjData) -> None:
        dof_x, dof_y, dof_yaw = (base_adr[k][1] for k in ("x", "y", "yaw"))

        lin_speed = np.hypot(d.qvel[dof_x], d.qvel[dof_y])
        if lin_speed > MAX_BASE_LINEAR_SPEED:
            d.qvel[dof_x] *= MAX_BASE_LINEAR_SPEED / lin_speed
            d.qvel[dof_y] *= MAX_BASE_LINEAR_SPEED / lin_speed
        if abs(d.qvel[dof_yaw]) > MAX_BASE_ANGULAR_SPEED:
            d.qvel[dof_yaw] = np.copysign(MAX_BASE_ANGULAR_SPEED, d.qvel[dof_yaw])

        yaw = d.qpos[base_adr["yaw"][0]]
        cos, sin = np.cos(yaw), np.sin(yaw)

        v_forward = d.qvel[dof_x] * cos + d.qvel[dof_y] * sin
        v_lateral = -d.qvel[dof_x] * sin + d.qvel[dof_y] * cos

        force_forward = KP_FORWARD * (state.vx - v_forward)
        force_lateral = -KP_LATERAL * v_lateral
        torque_yaw = KP_YAW * (state.wz - d.qvel[dof_yaw])

        d.xfrc_applied[base_id, 0] = force_forward * cos - force_lateral * sin
        d.xfrc_applied[base_id, 1] = force_forward * sin + force_lateral * cos
        d.xfrc_applied[base_id, 5] = torque_yaw

        for name, (qadr, dadr) in joint_dofs.items():
            d.qfrc_applied[dadr] = KP_JOINT * (ARM_HOME[name] - d.qpos[qadr]) - KD_JOINT * d.qvel[dadr]
        mimic_target = mimic_mult * ARM_HOME[mimic_source]
        d.qfrc_applied[mimic_dof_adr] = (
            KP_JOINT * (mimic_target - d.qpos[mimic_qpos_adr]) - KD_JOINT * d.qvel[mimic_dof_adr]
        )

    mujoco.set_mjcb_control(control_callback)

    def do_respawn() -> None:
        """Resets the base pose/velocity and all arm/head joints back to
        spawn, guarded by the viewer's own lock (same one the physics loop
        in launch_with_camera holds while stepping) since this runs on the
        WebSocket thread, not the physics thread."""
        with state.handle.lock() if state.handle is not None else contextlib.nullcontext():
            set_spawn_pose()
            for _qadr, dadr in base_adr.values():
                data.qvel[dadr] = 0.0
            data.xfrc_applied[base_id] = 0.0
            for _qadr, dadr in joint_dofs.values():
                data.qvel[dadr] = 0.0
            data.qvel[mimic_dof_adr] = 0.0
            data.qvel[mimic_dof_adr] = 0.0

    state.on_respawn = do_respawn

    print(
        f"Open http://localhost:{HTTP_PORT}/control_panel.html in a browser to drive (WASD or the on-screen joystick)."
    )
    print("Mouse: double-click a body then Ctrl+right-drag to push it. Space to pause.")
    lx, ly, lz, azimuth, elevation, extent = world.spawn_camera_view(SPAWN_X, SPAWN_Y, SPAWN_YAW_DEG)
    launch_with_camera(
        model,
        data,
        (lx, ly, lz),
        azimuth,
        elevation,
        extent * 1.5,
        on_ready=lambda h: setattr(state, "handle", h),
    )


if __name__ == "__main__":
    main()
