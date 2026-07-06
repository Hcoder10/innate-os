"""ROS 2 node impersonating the MARS hardware drivers with virtual_mars_core.
Topic/service contracts mirror the real stack (mars_bringup, rplidar launch,
mars_arm, mars_cam's stereo depth estimator) so brain_client, Nav2 and AMCL
run unchanged -- see README "Virtual MARS driver".

  pub /odom                                       nav_msgs/Odometry @30Hz (pose only, like bringup.py)
  pub TF odom->base_link                          @30Hz
  pub /scan                                       sensor_msgs/LaserScan @6Hz, frame base_laser
  pub /mars/main_camera/left/image_raw/compressed sensor_msgs/CompressedImage @7.5Hz (lazy)
  pub /mars/arm/image_raw/compressed              sensor_msgs/CompressedImage @5Hz (lazy)
  pub /mars/main_camera/depth/image_rect_raw      sensor_msgs/Image 16SC1 mm @8Hz (lazy)
  pub /mars/main_camera/points                    sensor_msgs/PointCloud2 xyz @8Hz (lazy)
  pub /mars/main_camera/left/camera_info          sensor_msgs/CameraInfo @8Hz
  pub /mars/arm/state                             sensor_msgs/JointState (joint1..6, rad) @20Hz
  pub /joint_states                               sensor_msgs/JointState (+joint_head) @30Hz
  pub /mars/head/current_position                 std_msgs/String (JSON, degrees) @10Hz
  sub /cmd_vel                                    geometry_msgs/Twist
  sub /mars/arm/commands                          std_msgs/Float64MultiArray (6x rad, best-effort)
  sub /mars/head/set_position                     std_msgs/Int32 (degrees)
  srv /mars/arm/goto_js, /goto_js_v2, /goto_js_trajectory (mars_msgs, if built)
  srv /mars/arm/torque_on|torque_off|reboot       std_srvs/Trigger (no-ops)
  srv /mars/head/set_ai_position                  std_srvs/Trigger (-20 deg)

The URDF's static frames (base_footprint, base_laser, camera_optical_frame,
arm links) come from robot_state_publisher fed by /joint_states -- launch it
alongside this node with the same mars.urdf. AMCL owns map->odom.

Needs a ROS 2 environment (rclpy comes from the distro, not pip):
  source /opt/ros/<distro>/setup.bash   # plus ros2_ws for mars_msgs services
  python3 virtual_mars_node.py
The mujoco/trimesh/pillow deps must be importable from that python.
"""

import contextlib
import json
import math
import threading
import time

import numpy as np
import rclpy
from geometry_msgs.msg import TransformStamped, Twist
from nav_msgs.msg import Odometry
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, qos_profile_sensor_data
from sensor_msgs.msg import CameraInfo, CompressedImage, Image, JointState, LaserScan, PointCloud2, PointField
from std_msgs.msg import Empty, Float64MultiArray, Int32, String
from std_srvs.srv import Trigger
from tf2_ros import TransformBroadcaster

from .core import CAMERA_FOVY, CAMERA_HEIGHT, CAMERA_WIDTH, VirtualMars, encode_jpeg

try:
    from mars_msgs.srv import GotoJS, GotoJSTrajectory
except ImportError:  # ros2_ws not sourced/built -- topic interface still works
    GotoJS = GotoJSTrajectory = None

MAIN_CAMERA_FPS = 7.5  # main_camera_driver: 15fps capture, JPEG every 2nd frame
WRIST_CAMERA_FPS = 5.0  # arm_camera_driver: 30fps capture, JPEG every 6th frame
DEPTH_FPS = 8.0  # stereo_depth_estimator max_fps
ODOM_HZ = 30.0  # bringup.py odom_frequency
SCAN_HZ = 6.0  # lidar.launch.py throttle
JOINT_STATE_HZ = 30.0
ARM_STATE_HZ = 20.0
HEAD_POSITION_HZ = 10.0
PHYSICS_CHUNK_S = 0.01

LIDAR_N_RAYS = 360
LIDAR_RANGE_MIN = 0.15  # rplidar A-series
LIDAR_RANGE_MAX = 12.0
HEAD_MIN_DEG, HEAD_MAX_DEG = -25.0, 25.0  # arm_config.yaml joint_7 limits
POINTS_SUBSAMPLE = 4  # 640x480 -> 160x120 cloud; costmap voxel-filters anyway
# The real stereo pipeline clamps depth to [0.25, 2.0]m (depth_clamp filter in
# stereo_depth_estimator.yaml) -- without the near clamp the sim camera sees
# the robot's own arm, which STVL then marks as an obstacle at the footprint.
DEPTH_MIN_M = 0.25
DEPTH_MAX_M = 2.0

ARM_JOINTS = ["joint1", "joint2", "joint3", "joint4", "joint5", "joint6"]

# Pinhole intrinsics implied by the render (square pixels, principal point
# centered): fy from the vertical FOV, fx = fy.
FOCAL = CAMERA_HEIGHT / (2 * math.tan(math.radians(CAMERA_FOVY) / 2))
CX, CY = CAMERA_WIDTH / 2, CAMERA_HEIGHT / 2
# Subsampled pixel grid for the pointcloud, precomputed once (published at 8 Hz).
POINTS_VS, POINTS_US = np.mgrid[0:CAMERA_HEIGHT:POINTS_SUBSAMPLE, 0:CAMERA_WIDTH:POINTS_SUBSAMPLE].astype(np.float32)


class VirtualMarsNode(Node):
    def __init__(self) -> None:
        super().__init__("virtual_mars")
        self.sim = VirtualMars()
        self._lock = threading.Lock()
        # Active joint-space trajectory: list of (from, to, duration) dicts
        # consumed by the physics loop; _traj_done fires when it drains.
        self._segments: list[tuple[dict, dict, float]] = []
        self._segment_started: float | None = None
        self._traj_done = threading.Event()
        self._traj_done.set()
        # Streaming commands / reset preempt a pending goto_js*; the waiting
        # service must then report failure, not success (real arm semantics).
        self._traj_preempted = False

        self._tf = TransformBroadcaster(self)
        self._odom_pub = self.create_publisher(Odometry, "/odom", 10)
        self._scan_pub = self.create_publisher(LaserScan, "/scan", qos_profile_sensor_data)
        self._main_pub = self.create_publisher(
            CompressedImage, "/mars/main_camera/left/image_raw/compressed", qos_profile_sensor_data
        )
        self._wrist_pub = self.create_publisher(
            CompressedImage, "/mars/arm/image_raw/compressed", qos_profile_sensor_data
        )
        # Raw variants: what webrtc_streamer (webapp video) encodes.
        self._main_raw_pub = self.create_publisher(Image, "/mars/main_camera/left/image_raw", qos_profile_sensor_data)
        self._wrist_raw_pub = self.create_publisher(Image, "/mars/arm/image_raw", qos_profile_sensor_data)
        self._depth_pub = self.create_publisher(
            Image, "/mars/main_camera/depth/image_rect_raw", qos_profile_sensor_data
        )
        self._points_pub = self.create_publisher(PointCloud2, "/mars/main_camera/points", qos_profile_sensor_data)
        self._caminfo_pub = self.create_publisher(CameraInfo, "/mars/main_camera/left/camera_info", 10)
        self._arm_state_pub = self.create_publisher(JointState, "/mars/arm/state", 10)
        self._joint_states_pub = self.create_publisher(JointState, "/joint_states", 10)
        self._head_pub = self.create_publisher(String, "/mars/head/current_position", 10)

        # Latched robot identity: clients (webapp) pick their camera view
        # implementation from this -- rendered view for sim, WebRTC for real.
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self._info_pub = self.create_publisher(String, "/robot_info", latched)
        info = String()
        info.data = json.dumps({"simulated": True, "driver": "virtual_mars", "physics": "mujoco"})
        self._info_pub.publish(info)

        # Camera roster, mirroring webrtc_streamer's /webrtc/active_streams
        # payload (2s cadence there): the webapp builds its camera switcher
        # from `cameras`. "orbit" is sim-only -- a free chase view the
        # SimSession renders; a real robot has no such camera.
        self._streams_pub = self.create_publisher(String, "/webrtc/active_streams", 10)
        self.create_timer(2.0, self._publish_active_streams)

        self.create_subscription(Twist, "/cmd_vel", self._on_cmd_vel, 10)
        self.create_subscription(
            Float64MultiArray, "/mars/arm/commands", self._on_arm_commands, qos_profile_sensor_data
        )
        self.create_subscription(Int32, "/mars/head/set_position", self._on_head_position, 10)
        # Sim-only convenience (no real-robot equivalent): respawn at the
        # spawn pose, used by sim/viewer's Reset button in connected mode.
        self.create_subscription(Empty, "/virtual_mars/reset", self._on_reset, 10)

        services = ReentrantCallbackGroup()  # goto services block; don't starve timers
        if GotoJS is not None:
            self.create_service(GotoJS, "/mars/arm/goto_js", self._on_goto_js, callback_group=services)
            self.create_service(GotoJS, "/mars/arm/goto_js_v2", self._on_goto_js, callback_group=services)
            self.create_service(
                GotoJSTrajectory, "/mars/arm/goto_js_trajectory", self._on_goto_js_trajectory, callback_group=services
            )
        else:
            self.get_logger().warning("mars_msgs not importable -- /mars/arm/goto_js* services disabled")
        for name in ("torque_on", "torque_off", "reboot", "fix_error"):
            self.create_service(Trigger, f"/mars/arm/{name}", self._on_trigger_noop, callback_group=services)
        self.create_service(Trigger, "/mars/head/set_ai_position", self._on_head_ai_position, callback_group=services)

        self.create_timer(1.0 / ODOM_HZ, self._publish_odom)
        self.create_timer(1.0 / SCAN_HZ, self._publish_scan)
        self.create_timer(1.0 / DEPTH_FPS, self._publish_camera_info)
        self.create_timer(1.0 / JOINT_STATE_HZ, self._publish_joint_states)
        self.create_timer(1.0 / ARM_STATE_HZ, self._publish_arm_state)
        self.create_timer(1.0 / HEAD_POSITION_HZ, self._publish_head)

        threading.Thread(target=self._physics_loop, daemon=True).start()
        # Rendering runs on its own thread, NOT executor timers: a software-GL
        # render takes ~100ms+, and doing it under the physics lock (or on the
        # mutually-exclusive callback group) starves physics and /cmd_vel.
        # The lock is only held for the update_scene snapshot.
        threading.Thread(target=self._render_loop, daemon=True).start()
        self.get_logger().info("virtual MARS up: odom + scan + cameras + depth/points + arm/head")

    # --- inputs ---

    def _on_cmd_vel(self, msg: Twist) -> None:
        with self._lock:
            self.sim.set_cmd_vel(msg.linear.x, msg.angular.z)

    def _on_arm_commands(self, msg: Float64MultiArray) -> None:
        values = list(msg.data)[: len(ARM_JOINTS)]
        with self._lock:
            if self._segments:
                self._traj_preempted = True  # streaming preempts trajectories
            self._segments.clear()
            self._traj_done.set()
            for name, value in zip(ARM_JOINTS, values, strict=False):
                self.sim.set_joint_target(name, float(value))

    def _on_reset(self, _msg: Empty) -> None:
        with self._lock:
            if self._segments:
                self._traj_preempted = True
            self._segments.clear()
            self._traj_done.set()
            self.sim.reset()

    def _on_head_position(self, msg: Int32) -> None:
        deg = max(HEAD_MIN_DEG, min(HEAD_MAX_DEG, float(msg.data)))
        with self._lock:
            self.sim.set_joint_target("joint_head", math.radians(deg))

    def _enqueue_segments(self, waypoints: list[dict], durations: list[float]) -> bool:
        with self._lock:
            current = self.sim.joint_targets()
            start = {k: current[k] for k in ARM_JOINTS}
            for target, duration in zip(waypoints, durations, strict=True):
                self._segments.append((start, target, max(duration, 0.05)))
                start = target
            self._segment_started = None
            self._traj_preempted = False
            self._traj_done.clear()
        total = sum(durations)
        return self._traj_done.wait(timeout=total + 5.0) and not self._traj_preempted

    def _on_goto_js(self, request, response):
        values = list(request.data.data)
        if len(values) < len(ARM_JOINTS):
            self.get_logger().warning(f"goto_js: got {len(values)} joint values, need {len(ARM_JOINTS)}; rejecting")
            response.success = False
            return response
        target = dict(zip(ARM_JOINTS, values, strict=False))
        response.success = self._enqueue_segments([target], [float(request.time)])
        return response

    def _on_goto_js_trajectory(self, request, response):
        n = int(request.num_joints)
        flat = list(request.waypoints.data)
        if n < len(ARM_JOINTS) or not flat or len(flat) % n != 0:
            self.get_logger().warning(
                f"goto_js_trajectory: invalid shape (num_joints={n}, {len(flat)} values); rejecting"
            )
            response.success = False
            return response
        waypoints = [dict(zip(ARM_JOINTS, flat[i : i + n], strict=False)) for i in range(0, len(flat), n)]
        durations = [float(d) for d in request.segment_durations]
        if len(durations) == len(waypoints) - 1:  # first segment: from current pose
            durations = [1.0, *durations]
        if len(durations) != len(waypoints):
            self.get_logger().warning(
                f"goto_js_trajectory: {len(waypoints)} waypoints but {len(durations)} durations; rejecting"
            )
            response.success = False
            return response
        response.success = self._enqueue_segments(waypoints, durations)
        return response

    def _on_trigger_noop(self, _request, response):
        response.success = True
        return response

    def _on_head_ai_position(self, _request, response):
        with self._lock:
            self.sim.set_joint_target("joint_head", math.radians(-20.0))
        response.success = True
        return response

    # --- physics ---

    def _physics_loop(self) -> None:
        start_wall = time.perf_counter()
        start_sim = self.sim.data.time
        while rclpy.ok():
            target = start_sim + (time.perf_counter() - start_wall)
            try:
                with self._lock:
                    self._advance_trajectory()
                    behind = target - self.sim.data.time
                    if behind > 0:
                        self.sim.step(min(behind, 0.1))  # cap catch-up after stalls
            except Exception as exc:  # noqa: BLE001 -- this thread must never die
                self.get_logger().error(f"physics step failed ({exc!r}); dropping active trajectory")
                with self._lock:
                    self._segments.clear()
                    self._traj_preempted = True
                    self._traj_done.set()
            time.sleep(PHYSICS_CHUNK_S)

    def _advance_trajectory(self) -> None:
        """Linear joint-space interpolation of the queued goto segments
        (called with the lock held, sim time as the clock)."""
        if not self._segments:
            return
        now = self.sim.data.time
        if self._segment_started is None:
            self._segment_started = now
        start, end, duration = self._segments[0]
        alpha = min(1.0, (now - self._segment_started) / duration)
        for name in ARM_JOINTS:
            self.sim.set_joint_target(name, start[name] + alpha * (end[name] - start[name]))
        if alpha >= 1.0:
            self._segments.pop(0)
            self._segment_started = None
            if not self._segments:
                self._traj_done.set()

    # --- outputs ---

    def _stamp(self):
        return self.get_clock().now().to_msg()

    def _publish_odom(self) -> None:
        with self._lock:
            x, y, yaw = self.sim.pose()
        stamp = self._stamp()

        # Like bringup.py: pose only, zero twist/covariance.
        odom = Odometry()
        odom.header.stamp = stamp
        odom.header.frame_id = "odom"
        odom.child_frame_id = "base_link"
        odom.pose.pose.position.x = x
        odom.pose.pose.position.y = y
        odom.pose.pose.orientation.z = math.sin(yaw / 2)
        odom.pose.pose.orientation.w = math.cos(yaw / 2)
        self._odom_pub.publish(odom)

        tf = TransformStamped()
        tf.header.stamp = stamp
        tf.header.frame_id = "odom"
        tf.child_frame_id = "base_link"
        tf.transform.translation.x = x
        tf.transform.translation.y = y
        tf.transform.rotation.z = math.sin(yaw / 2)
        tf.transform.rotation.w = math.cos(yaw / 2)
        self._tf.sendTransform(tf)

    def _publish_scan(self) -> None:
        with self._lock:
            ranges = self.sim.lidar_scan(LIDAR_N_RAYS, LIDAR_RANGE_MAX)
        msg = LaserScan()
        msg.header.stamp = self._stamp()
        msg.header.frame_id = "base_laser"
        msg.angle_min = -math.pi
        msg.angle_max = math.pi - 2 * math.pi / LIDAR_N_RAYS
        msg.angle_increment = 2 * math.pi / LIDAR_N_RAYS
        msg.scan_time = 1.0 / SCAN_HZ
        msg.time_increment = 0.0
        msg.range_min = LIDAR_RANGE_MIN
        msg.range_max = LIDAR_RANGE_MAX
        # core scans CCW from the robot's +x; LaserScan here starts at -pi.
        msg.ranges = np.roll(ranges, -LIDAR_N_RAYS // 2).astype(np.float32).tolist()
        self._scan_pub.publish(msg)

    def _render_loop(self) -> None:
        """Paced rendering off the executor: physics lock held only for the
        update_scene snapshot, the slow GL render runs unlocked."""
        last = {"main": 0.0, "wrist": 0.0, "depth": 0.0}
        period = {"main": 1.0 / MAIN_CAMERA_FPS, "wrist": 1.0 / WRIST_CAMERA_FPS, "depth": 1.0 / DEPTH_FPS}
        while rclpy.ok():
            now = time.monotonic()
            rendered = False
            for camera, pub, raw_pub in (
                ("main", self._main_pub, self._main_raw_pub),
                ("wrist", self._wrist_pub, self._wrist_raw_pub),
            ):
                if now - last[camera] < period[camera]:
                    continue
                if pub.get_subscription_count() == 0 and raw_pub.get_subscription_count() == 0:
                    continue  # lazy, like the real drivers
                last[camera] = now
                rendered = True
                with self._lock:
                    self.sim.update_camera(camera)
                self._publish_camera_frames(pub, raw_pub, camera, self.sim.read_rgb())
            if now - last["depth"] >= period["depth"] and (
                self._depth_pub.get_subscription_count() > 0 or self._points_pub.get_subscription_count() > 0
            ):
                last["depth"] = now
                rendered = True
                with self._lock:
                    self.sim.update_depth("main")
                self._publish_depth_and_points(self.sim.read_depth())
            if not rendered:
                time.sleep(0.02)

    def _publish_camera_frames(self, pub, raw_pub, camera: str, rgb) -> None:
        stamp = self._stamp()
        frame_id = "camera_optical_frame" if camera == "main" else "arm_camera_link"

        if pub.get_subscription_count() > 0:
            msg = CompressedImage()
            msg.header.stamp = stamp
            msg.header.frame_id = frame_id
            msg.format = "jpeg"
            msg.data = encode_jpeg(rgb)
            pub.publish(msg)

        if raw_pub.get_subscription_count() > 0:
            msg = Image()
            msg.header.stamp = stamp
            msg.header.frame_id = frame_id
            msg.height, msg.width = rgb.shape[:2]
            msg.encoding = "bgr8"  # ROS camera-driver convention
            msg.is_bigendian = False
            msg.step = msg.width * 3
            msg.data = rgb[:, :, ::-1].tobytes()
            raw_pub.publish(msg)

    def _publish_depth_and_points(self, depth) -> None:
        want_depth = self._depth_pub.get_subscription_count() > 0
        want_points = self._points_pub.get_subscription_count() > 0
        stamp = self._stamp()
        invalid = (depth < DEPTH_MIN_M) | (depth > DEPTH_MAX_M)

        if want_depth:
            # 16SC1 millimeters, invalid = 0 -- publishing.cpp's convention.
            mm = np.where(invalid, 0, depth * 1000.0)
            mm = np.clip(mm, 0, np.iinfo(np.int16).max).astype(np.int16)
            msg = Image()
            msg.header.stamp = stamp
            msg.header.frame_id = "camera_optical_frame"
            msg.height, msg.width = mm.shape
            msg.encoding = "16SC1"
            msg.is_bigendian = False
            msg.step = msg.width * 2
            msg.data = mm.tobytes()
            self._depth_pub.publish(msg)

        if want_points:
            s = POINTS_SUBSAMPLE
            z = np.where(invalid, np.nan, depth)[::s, ::s].astype(np.float32)
            x = (POINTS_US - CX) / FOCAL * z
            y = (POINTS_VS - CY) / FOCAL * z
            cloud = np.stack([x, y, z], axis=-1)

            msg = PointCloud2()
            msg.header.stamp = stamp
            msg.header.frame_id = "camera_optical_frame"
            msg.height, msg.width = cloud.shape[:2]
            msg.fields = [
                PointField(name=n, offset=4 * i, datatype=PointField.FLOAT32, count=1) for i, n in enumerate("xyz")
            ]
            msg.is_bigendian = False
            msg.point_step = 12
            msg.row_step = 12 * msg.width
            msg.is_dense = False
            msg.data = cloud.tobytes()
            self._points_pub.publish(msg)

    def _publish_camera_info(self) -> None:
        msg = CameraInfo()
        msg.header.stamp = self._stamp()
        msg.header.frame_id = "camera_optical_frame"
        msg.width, msg.height = CAMERA_WIDTH, CAMERA_HEIGHT
        msg.distortion_model = "plumb_bob"
        msg.d = [0.0] * 5
        msg.k = [FOCAL, 0.0, CX, 0.0, FOCAL, CY, 0.0, 0.0, 1.0]
        msg.r = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
        msg.p = [FOCAL, 0.0, CX, 0.0, 0.0, FOCAL, CY, 0.0, 0.0, 0.0, 1.0, 0.0]
        self._caminfo_pub.publish(msg)

    def _publish_joint_states(self) -> None:
        with self._lock:
            positions = self.sim.joint_positions()
        msg = JointState()
        msg.header.stamp = self._stamp()
        msg.name = [*ARM_JOINTS, "joint_head"]  # same 7 as the real arm node
        msg.position = [positions[n] for n in msg.name]
        self._joint_states_pub.publish(msg)

    def _publish_arm_state(self) -> None:
        with self._lock:
            positions = self.sim.joint_positions()
        msg = JointState()
        msg.header.stamp = self._stamp()
        msg.name = list(ARM_JOINTS)
        msg.position = [positions[n] for n in ARM_JOINTS]
        self._arm_state_pub.publish(msg)

    def _publish_active_streams(self) -> None:
        msg = String()
        msg.data = json.dumps({"cameras": ["main", "arm", "orbit"], "count": 0, "clients": [], "simulated": True})
        self._streams_pub.publish(msg)

    def _publish_head(self) -> None:
        with self._lock:
            pitch = self.sim.head_pitch_deg()
        msg = String()
        msg.data = json.dumps(
            {
                "current_position": pitch,
                "min_angle": HEAD_MIN_DEG,
                "max_angle": HEAD_MAX_DEG,
                "default_angle": 0.0,
            }
        )
        self._head_pub.publish(msg)


def main() -> None:
    rclpy.init()
    node = VirtualMarsNode()
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(node)
    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        with contextlib.suppress(Exception):  # SIGINT may have shut rcl down already
            rclpy.shutdown()


if __name__ == "__main__":
    main()
