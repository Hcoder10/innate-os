"""Drive the real Mars URDF robot (base + arm + head, real collision
geometry) around the apartment collision mesh in MuJoCo's native interactive
viewer, controlled live from a tiny browser control panel (WASD keys or an
on-screen joystick) over a local WebSocket.

This is a native-MuJoCo sandbox for testing drive feel against the fixed
apartment collision mesh (see decompose_rooms.py / README.md) before porting
anything back to sim-web's WASM worker. It reuses:
  - drive_apartment.py's room-mesh loading + world XML builder (environment
    only, via include_placeholder_robot=False)
  - urdfWorker.ts's base-drive PD gains and arm/head joint PD-servo gains,
    ported to Python (same tuned values, same math)
  - mjSpec's attach() to merge the URDF robot (with a planar x/y/yaw base
    added to base_link, same as apartmentWorker.ts and genesis) into the
    apartment world spec

No live keyboard capture in the MuJoCo window itself -- same mjpython/macOS
limitation as drive_apartment.py (see its docstring). Driving input instead
comes over the network from control_panel.html, which sidesteps that
limitation entirely since it's not going through mujoco.viewer's own input
handling.

Usage:
    uv run drive_mars.py [work/apartment_split_v2]
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

import drive_apartment as da
import mujoco
import mujoco.viewer
import numpy as np
import websockets
import websockets.sync.server
from common import SPAWN_X, SPAWN_Y, SPAWN_YAW_DEG, launch_with_camera

URDF_PATH = Path(__file__).resolve().parent.parent / "sim-web" / "public" / "robot" / "mars.urdf"
WS_PORT = 8765
HTTP_PORT = 8766

# Same visual convention as scene.ts's Three.js render (which loads this URDF
# independently via urdf-loader -- MuJoCo's own physics-side URDF import
# normally only keeps <collision> geometry, discarding <visual> meshes
# entirely; see load_robot_spec for why).
ORANGE_LINKS = {"link1", "link3", "link5"}
BRIGHT_ORANGE = (1.0, 0.5, 0.0, 1.0)
# Marker spheres for frames that carry no real body geometry (see scene.ts's
# HIDDEN_FRAME_LINKS) -- hide rather than render as visible spheres.
HIDDEN_MARKER_LINKS = {"ee_link", "head_camera_left", "head_camera_right"}

MAX_LINEAR = 0.4
MAX_YAW = 1.0

# Base drive gains -- ported from apartmentWorker.ts (KP_YAW=6 resonates with
# the arm servos on the same axis, see that file).
KP_FORWARD = 200.0
KP_LATERAL = 40.0
KP_YAW = 3.0

# Arm/head PD position-servo gains -- ported from urdfWorker.ts.
KP_JOINT = 8.0
KD_JOINT = 0.6

# Matches the webapp's ARM_HOME_POSITIONS / genesis' ROBOT_ARM_HOME_POSITIONS.
ARM_HOME = {
    "joint1": 1.445009902188274,
    "joint2": -1.3882526130365052,
    "joint3": 1.517106999218899,
    "joint4": 0.44638840927472156,
    "joint5": -0.08897088569736719,
    "joint6": 0.0015339807878856412,
    "joint_head": 0.0,
}

# Safety governor: the apartment's convex-decomposed collision mesh still has
# occasional imperfect hull edges (see README's "Collision mesh
# regeneration" -- 1283 hulls, not all perfectly flush with the floor), and a
# blind scripted tour driving straight through one for seconds on end will
# still find them and explode. A human on the joystick will react and steer
# away well before that -- but as a safety net, clamp the base's velocity
# every step so a bad single-step contact impulse turns into a recoverable
# thump instead of an unbounded explosion into NaN.
MAX_BASE_LINEAR_SPEED = 2.0
MAX_BASE_ANGULAR_SPEED = 6.0

DRIVEN_JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5", "joint6", "joint_head"]
MIMIC_JOINT = ("joint6M", "joint6", -1.0)  # (name, source, multiplier) -- see urdfWorker.ts comment


def load_robot_spec(urdf_path: Path) -> mujoco.MjSpec:
    """Loads mars.urdf with its <visual> STL meshes intact, not just the
    <collision> boxes MjSpec.from_file(urdf_path) gives you by default.

    Two things stand in the way of a plain from_file() load:
    1. MuJoCo's URDF importer discards <visual> geometry unless the embedded
       <mujoco><compiler discardvisual="false"/></mujoco> override is
       present (confirmed empirically -- from_file() alone compiles fine but
       drops straight to 9 box-only geoms, one per <collision>).
    2. Every <visual> mesh filename uses a ROS "package://mars_sim/..." URI;
       MuJoCo has no ROS package resolver (confirmed: even with
       discardvisual="false" it tries to open the literal string
       "package://mars_sim/meshes/link1.STL" as a path and fails), so the
       prefix is rewritten to this URDF's own meshes/ directory first.

    Once both are in place, MuJoCo's URDF importer conveniently sorts
    <visual> meshes into contype=0 geoms (group 1) and <collision> boxes
    into contype=1 geoms (group 0) automatically -- see style_robot_geoms
    for moving the latter into the apartment's hidden-by-default collision
    group instead."""
    robot_dir = str(urdf_path.parent.resolve()) + "/"
    urdf_text = urdf_path.read_text().replace("package://mars_sim/", robot_dir)
    urdf_text = urdf_text.replace("<robot name=\"mars_bot\">", '<robot name="mars_bot"><mujoco><compiler discardvisual="false"/></mujoco>')
    return mujoco.MjSpec.from_string(urdf_text)


def style_robot_geoms(model: mujoco.MjModel, prefix: str = "robot_") -> None:
    """Post-compile pass matching scene.ts's Three.js styling of this same
    URDF: paints the arm links orange, hides the frame-marker spheres, and
    -- since MuJoCo's URDF import leaves <collision> geoms visible in the
    default group 0 rather than a class of their own -- moves them into the
    apartment's hidden-by-default collision group so only the new <visual>
    meshes show up initially."""
    for i in range(model.ngeom):
        body_name = mujoco.mj_id2name(model, mujoco.mjtObj.mjOBJ_BODY, model.geom_bodyid[i]) or ""
        link_name = body_name.removeprefix(prefix)

        if model.geom_contype[i] == 1:  # a <collision> geom, not a <visual> one
            model.geom_group[i] = da.COLLISION_GROUP
        elif link_name in ORANGE_LINKS:
            model.geom_rgba[i] = BRIGHT_ORANGE
        elif link_name in HIDDEN_MARKER_LINKS:
            model.geom_rgba[i, 3] = 0.0
        elif model.geom_rgba[i, :3].mean() < 0.4:
            # scene.ts lifts matt_black (0.05) toward charcoal so the robot's
            # form reads; a bit higher here since MuJoCo has no tone mapping.
            model.geom_rgba[i, :3] = 0.16


def add_planar_base(robot_spec: mujoco.MjSpec) -> None:
    """Planar base (x, y, yaw), matching genesis and apartmentWorker.ts: a
    wheeled chassis can't pitch, and a free joint lets the arm's reaction
    torque tip the 0.89kg base over."""
    base_body = robot_spec.body("base_link")
    for name, jtype, axis in (
        ("base_x", mujoco.mjtJoint.mjJNT_SLIDE, (1, 0, 0)),
        ("base_y", mujoco.mjtJoint.mjJNT_SLIDE, (0, 1, 0)),
        ("base_yaw", mujoco.mjtJoint.mjJNT_HINGE, (0, 0, 1)),
    ):
        joint = base_body.add_joint()
        joint.name = name
        joint.type = jtype
        joint.axis = axis


def build_model(rooms: dict[str, list[Path]], visual_rooms: dict[str, Path] | None = None) -> mujoco.MjModel:
    world_xml = da.build_world_xml(rooms, include_placeholder_robot=False, visual_rooms=visual_rooms)
    world_spec = mujoco.MjSpec.from_string(world_xml)
    robot_spec = load_robot_spec(URDF_PATH)
    add_planar_base(robot_spec)

    frame = world_spec.worldbody.add_frame()
    world_spec.attach(robot_spec, frame=frame, prefix="robot_")
    model = world_spec.compile()
    style_robot_geoms(model)
    # With z pinned by the planar base, the ground's contact margin reads as
    # permanent penetration whose friction glues the base -- zero it.
    model.geom("ground").margin = 0.0
    # Brighten toward sim-web's look (ambient/hemisphere fill there); the
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
        opt.geomgroup[da.VISUAL_GROUP], opt.geomgroup[da.COLLISION_GROUP] = (
            opt.geomgroup[da.COLLISION_GROUP],
            opt.geomgroup[da.VISUAL_GROUP],
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
    split_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent / "work" / "apartment_split_v2"
    rooms = da.find_decomposed_rooms(split_dir)
    if not rooms:
        raise SystemExit(f"no <room>/<room>_collision_*.obj found under {split_dir}")

    visual_dir = Path(__file__).resolve().parent / "work" / "apartment_visual"
    visual_rooms = da.find_visual_rooms(visual_dir) if visual_dir.is_dir() else {}
    if visual_rooms:
        print(f"Loaded {len(visual_rooms)} textured room(s) -- press '3' in the viewer to toggle collision hulls.")
    else:
        print(f"No textured rooms found under {visual_dir} -- run export_visual_rooms.py first for the sim-web-style render.")

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
        d.qfrc_applied[mimic_dof_adr] = KP_JOINT * (mimic_target - d.qpos[mimic_qpos_adr]) - KD_JOINT * d.qvel[mimic_dof_adr]

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

    print(f"Open http://localhost:{HTTP_PORT}/control_panel.html in a browser to drive (WASD or the on-screen joystick).")
    print("Mouse: double-click a body then Ctrl+right-drag to push it. Space to pause.")
    lx, ly, lz, azimuth, elevation, extent = da.spawn_camera_view(SPAWN_X, SPAWN_Y, SPAWN_YAW_DEG)
    launch_with_camera(
        model, data, (lx, ly, lz), azimuth, elevation, extent * 1.5,
        on_ready=lambda h: setattr(state, "handle", h),
    )


if __name__ == "__main__":
    main()
