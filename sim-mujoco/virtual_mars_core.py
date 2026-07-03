"""Transport-agnostic core of the virtual MARS driver: headless MuJoCo
physics of the apartment + real mars.urdf, with the two robot cameras
rendered offscreen. virtual_mars_node.py wraps this in a ROS 2 node that
impersonates the mars_cam/base drivers; this module has no ROS dependency so
it can be developed and tested anywhere (see test_virtual_mars.py).

Reuses drive_mars.py's model building (URDF attach with visual meshes,
free-jointed base, drive/joint servos ported from the browser worker).
"""

import io
import math
import os
from pathlib import Path

import drive_apartment as da
import drive_mars as dm
import mujoco
import numpy as np
from common import SPAWN_X, SPAWN_Y, SPAWN_YAW_DEG
from PIL import Image

ARM_HOME = dm.ARM_HOME

# Stop the base if the last Twist is stale, like a real base watchdog.
CMD_VEL_TIMEOUT_S = 0.5

# name -> (body carrying the camera, URDF-frame forward/up of the lens).
# MuJoCo cameras look down their local -Z with +Y up; _camera_quat converts.
CAMERAS = {
    "main": ("robot_camera_optical_frame", (0, 0, 1), (0, -1, 0)),
    "wrist": ("robot_arm_camera_link", (1, 0, 0), (0, 0, 1)),
}
CAMERA_WIDTH, CAMERA_HEIGHT = 640, 480
CAMERA_FOVY = 80  # genesis' robot_camera_vfov
JPEG_QUALITY = 80  # matches main_camera_driver.cpp
# Post-render ACES tone map approximating sim-web's Three.js output
# (ACESFilmicToneMapping); exposure calibrated visually against it.
TONEMAP_EXPOSURE = 2.5

# Arm/head PD servo -- apartmentWorker.ts's tuned defaults (reactive feel),
# torque-clamped; the URDF's per-joint damping=5 caps speed at LIMIT/5 rad/s.
KP_JOINT = 50.0
KD_JOINT = 1.0
EFFORT_LIMIT = 50.0  # N*m


def encode_jpeg(rgb: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, "JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


def _tonemap(rgb: np.ndarray) -> np.ndarray:
    linear = (rgb.astype(np.float32) / 255.0) ** 2.2 * TONEMAP_EXPOSURE
    mapped = np.clip(linear * (2.51 * linear + 0.03) / (linear * (2.43 * linear + 0.59) + 0.14), 0, 1)
    return (mapped ** (1 / 2.2) * 255).astype(np.uint8)


def _camera_quat(forward, up) -> np.ndarray:
    z = -np.array(forward, dtype=float)
    y = np.array(up, dtype=float)
    x = np.cross(y, z)
    quat = np.zeros(4)
    mujoco.mju_mat2Quat(quat, np.column_stack([x, y, z]).flatten())
    return quat


# Apartment meshes (decomposed hulls + textured rooms). Defaults to the local
# work/ outputs; the container/launcher points this at the synced asset image.
ASSETS_DIR = Path(os.environ.get("VIRTUAL_MARS_ASSETS", Path(__file__).resolve().parent / "work"))


class VirtualMars:
    def __init__(self, split_dir: Path | None = None):
        rooms = da.find_decomposed_rooms(split_dir or ASSETS_DIR / "apartment_split_v2")
        if not rooms:
            raise RuntimeError(
                f"no decomposed rooms under {split_dir or ASSETS_DIR} -- "
                "run decompose_rooms.py or set VIRTUAL_MARS_ASSETS"
            )
        visual_dir = ASSETS_DIR / "apartment_visual"
        visual_rooms = da.find_visual_rooms(visual_dir) if visual_dir.is_dir() else {}

        world_spec = mujoco.MjSpec.from_string(
            da.build_world_xml(rooms, include_placeholder_robot=False, visual_rooms=visual_rooms)
        )
        # Lidar rays hit only the textured visual meshes (true surfaces, like
        # a real lidar) when available -- without them, fall back to all
        # groups (the collision hulls, ~1cm inflated).
        self._lidar_groups = None
        if visual_rooms:
            self._lidar_groups = np.zeros(6, dtype=np.uint8)
            self._lidar_groups[da.VISUAL_GROUP] = 1
        robot_spec = dm.load_robot_spec(dm.URDF_PATH)
        dm.add_planar_base(robot_spec)
        world_spec.attach(robot_spec, frame=world_spec.worldbody.add_frame(), prefix="robot_")

        for cam_name, (body_name, forward, up) in CAMERAS.items():
            cam = world_spec.body(body_name).add_camera()
            cam.name = cam_name
            cam.fovy = CAMERA_FOVY
            cam.quat = _camera_quat(forward, up)

        self.model = world_spec.compile()
        dm.style_robot_geoms(self.model)
        # The planar base pins z at the plane, so the ground's 7mm contact
        # margin reads as permanent penetration -- huge normal force whose
        # friction cone glues the base. The worker's ground has no margin.
        self.model.geom("ground").margin = 0.0
        # znear is a fraction of stat.extent; pin it to genesis' 0.03m so the
        # cameras clip their own housing shells instead of rendering them.
        self.model.vis.map.znear = 0.03 / self.model.stat.extent
        # The textured rooms carry baked lighting; brighten toward the
        # Three.js render (ambient 1.2 + ACES exposure 1.5 there).
        self.model.vis.headlight.ambient[:] = 0.5
        self.model.vis.headlight.diffuse[:] = 0.5
        self.data = mujoco.MjData(self.model)

        self._base_id = self.model.body("robot_base_link").id
        self._base = {}  # "x"/"y"/"yaw" -> (qpos_adr, dof_adr)
        for short in ("x", "y", "yaw"):
            jid = self.model.joint(f"robot_base_{short}").id
            self._base[short] = (self.model.jnt_qposadr[jid], self.model.jnt_dofadr[jid])

        self._joints = {}  # name -> (qpos_adr, dof_adr, home)
        for name, home in ARM_HOME.items():
            jid = self.model.joint(f"robot_{name}").id
            self._joints[name] = (self.model.jnt_qposadr[jid], self.model.jnt_dofadr[jid], home)
        mimic_name, mimic_source, mimic_mult = dm.MIMIC_JOINT
        jid = self.model.joint(f"robot_{mimic_name}").id
        self._mimic = (self.model.jnt_qposadr[jid], self.model.jnt_dofadr[jid], mimic_source, mimic_mult)

        self._renderer: mujoco.Renderer | None = None
        self._depth_renderer: mujoco.Renderer | None = None
        self._cmd_vx = 0.0
        self._cmd_wz = 0.0
        self._cmd_sim_time = -math.inf
        self.reset()

    def reset(self) -> None:
        mujoco.mj_resetData(self.model, self.data)
        self.data.qpos[self._base["x"][0]] = SPAWN_X
        self.data.qpos[self._base["y"][0]] = SPAWN_Y
        self.data.qpos[self._base["yaw"][0]] = math.radians(SPAWN_YAW_DEG)
        for qadr, _dadr, home in self._joints.values():
            self.data.qpos[qadr] = home
        mq, _md, source, mult = self._mimic
        self.data.qpos[mq] = mult * ARM_HOME[source]
        self._cmd_vx = self._cmd_wz = 0.0
        self._cmd_sim_time = -math.inf
        mujoco.mj_forward(self.model, self.data)

    def set_cmd_vel(self, vx: float, wz: float) -> None:
        self._cmd_vx = max(-dm.MAX_LINEAR, min(dm.MAX_LINEAR, vx))
        self._cmd_wz = max(-dm.MAX_YAW, min(dm.MAX_YAW, wz))
        self._cmd_sim_time = self.data.time

    def set_joint_target(self, name: str, value: float) -> None:
        if name in self._joints:
            qadr, dadr, _home = self._joints[name]
            self._joints[name] = (qadr, dadr, value)

    def joint_targets(self) -> dict[str, float]:
        return {name: target for name, (_q, _d, target) in self._joints.items()}

    def step(self, duration: float) -> None:
        """Advance the sim by `duration` seconds, applying servos each step."""
        end = self.data.time + duration
        while self.data.time < end:
            self._apply_control()
            mujoco.mj_step(self.model, self.data)
            if not np.all(np.isfinite(self.data.qpos)):
                self.reset()
                return

    def _apply_control(self) -> None:
        d = self.data
        dof_x, dof_y, dof_yaw = (self._base[k][1] for k in ("x", "y", "yaw"))

        lin = math.hypot(d.qvel[dof_x], d.qvel[dof_y])
        if lin > dm.MAX_BASE_LINEAR_SPEED:
            d.qvel[dof_x] *= dm.MAX_BASE_LINEAR_SPEED / lin
            d.qvel[dof_y] *= dm.MAX_BASE_LINEAR_SPEED / lin
        if abs(d.qvel[dof_yaw]) > dm.MAX_BASE_ANGULAR_SPEED:
            d.qvel[dof_yaw] = math.copysign(dm.MAX_BASE_ANGULAR_SPEED, d.qvel[dof_yaw])

        expired = d.time - self._cmd_sim_time > CMD_VEL_TIMEOUT_S
        vx = 0.0 if expired else self._cmd_vx
        wz = 0.0 if expired else self._cmd_wz

        yaw = d.qpos[self._base["yaw"][0]]
        cos, sin = math.cos(yaw), math.sin(yaw)
        v_forward = d.qvel[dof_x] * cos + d.qvel[dof_y] * sin
        v_lateral = -d.qvel[dof_x] * sin + d.qvel[dof_y] * cos

        force_forward = dm.KP_FORWARD * (vx - v_forward)
        force_lateral = -dm.KP_LATERAL * v_lateral
        torque_yaw = dm.KP_YAW * (wz - d.qvel[dof_yaw])
        d.xfrc_applied[self._base_id, 0] = force_forward * cos - force_lateral * sin
        d.xfrc_applied[self._base_id, 1] = force_forward * sin + force_lateral * cos
        d.xfrc_applied[self._base_id, 5] = torque_yaw

        for qadr, dadr, target in self._joints.values():
            torque = KP_JOINT * (target - d.qpos[qadr]) - KD_JOINT * d.qvel[dadr]
            d.qfrc_applied[dadr] = max(-EFFORT_LIMIT, min(EFFORT_LIMIT, torque))
        mq, md, source, mult = self._mimic
        target = mult * self._joints[source][2]
        torque = KP_JOINT * (target - d.qpos[mq]) - KD_JOINT * d.qvel[md]
        d.qfrc_applied[md] = max(-EFFORT_LIMIT, min(EFFORT_LIMIT, torque))

    def update_camera(self, camera: str) -> None:
        """Snapshot sim state into the renderer's scene (fast; call under the
        physics lock). The GL render itself (read_rgb) can then run outside."""
        if self._renderer is None:
            self._renderer = mujoco.Renderer(self.model, height=CAMERA_HEIGHT, width=CAMERA_WIDTH)
        self._renderer.update_scene(self.data, camera=camera)

    def read_rgb(self) -> np.ndarray:
        """Tone-mapped uint8 RGB of the last update_camera snapshot. Slow
        (software GL) -- do not hold the physics lock across this."""
        return _tonemap(self._renderer.render())

    def render_rgb(self, camera: str) -> np.ndarray:
        self.update_camera(camera)
        return self.read_rgb()

    def render_jpeg(self, camera: str) -> bytes:
        return encode_jpeg(self.render_rgb(camera))

    def update_depth(self, camera: str) -> None:
        """Depth counterpart of update_camera."""
        if self._depth_renderer is None:
            self._depth_renderer = mujoco.Renderer(self.model, height=CAMERA_HEIGHT, width=CAMERA_WIDTH)
            self._depth_renderer.enable_depth_rendering()
        self._depth_renderer.update_scene(self.data, camera=camera)

    def read_depth(self) -> np.ndarray:
        return self._depth_renderer.render()

    def render_depth(self, camera: str) -> np.ndarray:
        """Depth image in meters (float32, CAMERA_HEIGHT x CAMERA_WIDTH)."""
        self.update_depth(camera)
        return self.read_depth()

    def lidar_scan(self, n_rays: int, max_range: float) -> np.ndarray:
        """360-degree planar scan from the base_laser mount, CCW from the
        robot's +x. Distances in meters; max_range where nothing was hit."""
        body_id = self.model.body("robot_base_laser").id
        origin = self.data.xpos[body_id].copy()
        _x, _y, yaw = self.pose()
        angles = yaw + np.arange(n_rays) * (2 * math.pi / n_rays)
        vecs = np.zeros((n_rays, 3))
        vecs[:, 0] = np.cos(angles)
        vecs[:, 1] = np.sin(angles)

        geomids = np.full(n_rays, -1, dtype=np.int32)
        dists = np.full(n_rays, -1.0)
        mujoco.mj_multiRay(
            self.model,
            self.data,
            origin,
            vecs.flatten(),
            self._lidar_groups,
            1,  # include static geoms
            self._base_id,  # exclude the robot's own base body
            geomids,
            dists,
            None,  # surface normals not needed
            n_rays,
            max_range,
        )
        dists[geomids == -1] = max_range
        return np.minimum(dists, max_range)

    def pose(self) -> tuple[float, float, float]:
        return (
            float(self.data.qpos[self._base["x"][0]]),
            float(self.data.qpos[self._base["y"][0]]),
            float(self.data.qpos[self._base["yaw"][0]]),
        )

    def velocity(self) -> tuple[float, float, float]:
        """(v_forward, v_lateral, wz) in the base frame, for odometry."""
        dof_x, dof_y, dof_yaw = (self._base[k][1] for k in ("x", "y", "yaw"))
        yaw = self.data.qpos[self._base["yaw"][0]]
        cos, sin = math.cos(yaw), math.sin(yaw)
        vx, vy = self.data.qvel[dof_x], self.data.qvel[dof_y]
        return (vx * cos + vy * sin, -vx * sin + vy * cos, float(self.data.qvel[dof_yaw]))

    def head_pitch_deg(self) -> float:
        qadr, _dadr, _t = self._joints["joint_head"]
        return math.degrees(float(self.data.qpos[qadr]))

    def joint_positions(self) -> dict[str, float]:
        return {name: float(self.data.qpos[qadr]) for name, (qadr, _d, _t) in self._joints.items()}
