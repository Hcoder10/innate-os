"""Sim stand-in for mars_nav's grid_localizer (which is CUDA-only and can't
run in the container). Same contract: lifecycle node named
navigation_grid_localizer, latched /initialpose, a `localize` Trigger -- but
"localization" is just the driver's ground-truth odom pose (map == odom in
sim until AMCL refines it)."""

import math
import time

import rclpy
from geometry_msgs.msg import PoseWithCovarianceStamped
from nav_msgs.msg import Odometry
from rclpy.lifecycle import LifecycleNode, TransitionCallbackReturn
from rclpy.qos import DurabilityPolicy, QoSProfile
from std_srvs.srv import Trigger

POSITION_MATCH_TOLERANCE_M = 0.35
YAW_MATCH_TOLERANCE_RAD = math.radians(20.0)
ODOM_RESET_POSITION_JUMP_M = 0.5
ODOM_RESET_YAW_JUMP_RAD = math.radians(30.0)
ODOM_RESET_MAX_SAMPLE_GAP_S = 0.2
AMCL_DRIFT_POSITION_M = 1.0
AMCL_DRIFT_YAW_RAD = math.radians(35.0)
AMCL_DRIFT_MIN_SAMPLES = 4
AMCL_DRIFT_DWELL_S = 0.75
AMCL_MODERATE_DRIFT_POSITION_M = 0.5
AMCL_MODERATE_DRIFT_YAW_RAD = math.radians(25.0)
AMCL_MODERATE_DRIFT_MIN_SAMPLES = 12
AMCL_MODERATE_DRIFT_DWELL_S = 3.0
AMCL_HEALTHY_REARM_S = 5.0
AMCL_RESEED_COOLDOWN_S = 15.0


def _yaw(pose) -> float:
    orientation = pose.orientation
    return math.atan2(
        2.0 * (orientation.w * orientation.z + orientation.x * orientation.y),
        1.0 - 2.0 * (orientation.y**2 + orientation.z**2),
    )


def _stamp_ns(msg) -> int:
    return int(msg.header.stamp.sec) * 1_000_000_000 + int(msg.header.stamp.nanosec)


def _pose_matches(pose, expected: tuple[float, float, float]) -> bool:
    x, y, yaw = expected
    position = pose.position
    yaw_error = math.atan2(math.sin(_yaw(pose) - yaw), math.cos(_yaw(pose) - yaw))
    return (
        math.hypot(position.x - x, position.y - y) <= POSITION_MATCH_TOLERANCE_M
        and abs(yaw_error) <= YAW_MATCH_TOLERANCE_RAD
    )


def _pose_error(first, second) -> tuple[float, float]:
    position_error = math.hypot(first.position.x - second.position.x, first.position.y - second.position.y)
    yaw_error = math.atan2(math.sin(_yaw(first) - _yaw(second)), math.cos(_yaw(first) - _yaw(second)))
    return position_error, abs(yaw_error)


class GridLocalizerSim(LifecycleNode):
    def __init__(self):
        super().__init__("navigation_grid_localizer")
        self._last_odom = None
        self._pose_pub = None
        self._retry_timer = None
        self._pending_pose = None
        self._drift_bad_since = None
        self._drift_bad_samples = 0
        self._drift_armed = True
        self._drift_healthy_since = None
        self._last_auto_reseed_at = None
        self.create_subscription(Odometry, "/odom", self._on_odom, 10)
        # AMCL's /initialpose sub is VOLATILE, so latching can't cover the
        # activation race -- retry until AMCL answers with /amcl_pose.
        self.create_subscription(PoseWithCovarianceStamped, "/amcl_pose", self._on_amcl_pose, 10)
        self.create_service(Trigger, "localize", self._on_localize)

    def _on_odom(self, msg):
        previous = self._last_odom
        self._last_odom = msg
        # ROS stamps remain wall time across a MuJoCo reset. Ground-truth odom
        # itself teleports to the challenge spawn, which is the reliable reset
        # signal available to this ROS-side sim adapter.
        if previous is not None:
            sample_gap_s = (_stamp_ns(msg) - _stamp_ns(previous)) / 1_000_000_000
            if not 0.0 <= sample_gap_s <= ODOM_RESET_MAX_SAMPLE_GAP_S:
                return
            position_jump, yaw_jump = _pose_error(msg.pose.pose, previous.pose.pose)
            if position_jump < ODOM_RESET_POSITION_JUMP_M and yaw_jump < ODOM_RESET_YAW_JUMP_RAD:
                return
            self.get_logger().info(
                f"simulator pose reset detected ({position_jump:.2f}m, {math.degrees(yaw_jump):.0f}deg); reseeding AMCL"
            )
            self._clear_drift_observation()
            self._drift_armed = True
            self._last_auto_reseed_at = None
            self._begin_localization()

    def _on_amcl_pose(self, msg):
        pose = msg.pose.pose
        if self._pending_pose is not None:
            if not _pose_matches(pose, self._pending_pose):
                return
            self._pending_pose = None
            self._clear_drift_observation()
            if not self._drift_armed:
                self._drift_healthy_since = time.monotonic()
            if self._retry_timer is not None:
                self._retry_timer.cancel()
                self._retry_timer = None
                self.get_logger().info("AMCL localized; stopping /initialpose retries")
            return

        if self._last_odom is None:
            return
        now = time.monotonic()
        position_error, yaw_error = _pose_error(pose, self._last_odom.pose.pose)
        healthy = position_error <= POSITION_MATCH_TOLERANCE_M and yaw_error <= YAW_MATCH_TOLERANCE_RAD
        if healthy:
            self._clear_drift_observation()
            if self._drift_armed:
                return
            if self._drift_healthy_since is None:
                self._drift_healthy_since = now
            cooldown_over = (
                self._last_auto_reseed_at is None or now - self._last_auto_reseed_at >= AMCL_RESEED_COOLDOWN_S
            )
            if now - self._drift_healthy_since >= AMCL_HEALTHY_REARM_S and cooldown_over:
                self._drift_armed = True
                self._drift_healthy_since = None
                self.get_logger().info("AMCL drift guard rearmed after stable localization")
            return

        self._drift_healthy_since = None
        if not self._drift_armed:
            self._clear_drift_observation()
            return
        severe_drift = position_error >= AMCL_DRIFT_POSITION_M or yaw_error >= AMCL_DRIFT_YAW_RAD
        moderate_drift = position_error >= AMCL_MODERATE_DRIFT_POSITION_M or yaw_error >= AMCL_MODERATE_DRIFT_YAW_RAD
        if not moderate_drift:
            self._clear_drift_observation()
            return
        if self._drift_bad_since is None:
            self._drift_bad_since = now
        self._drift_bad_samples += 1
        min_samples = AMCL_DRIFT_MIN_SAMPLES if severe_drift else AMCL_MODERATE_DRIFT_MIN_SAMPLES
        dwell_s = AMCL_DRIFT_DWELL_S if severe_drift else AMCL_MODERATE_DRIFT_DWELL_S
        if self._drift_bad_samples < min_samples or now - self._drift_bad_since < dwell_s:
            return

        self.get_logger().warning(
            f"AMCL diverged from simulator ground truth ({position_error:.2f}m, "
            f"{math.degrees(yaw_error):.0f}deg); reseeding"
        )
        self._clear_drift_observation()
        self._drift_armed = False
        self._last_auto_reseed_at = now
        if not self._begin_localization():
            self._drift_armed = True
            self._last_auto_reseed_at = None

    def _clear_drift_observation(self):
        self._drift_bad_since = None
        self._drift_bad_samples = 0

    def on_configure(self, state) -> TransitionCallbackReturn:
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self._pose_pub = self.create_lifecycle_publisher(PoseWithCovarianceStamped, "/initialpose", latched)
        return TransitionCallbackReturn.SUCCESS

    def on_activate(self, state) -> TransitionCallbackReturn:
        self._clear_drift_observation()
        self._drift_armed = True
        self._drift_healthy_since = None
        self._last_auto_reseed_at = None
        self._begin_localization()
        return super().on_activate(state)

    def on_deactivate(self, state) -> TransitionCallbackReturn:
        self._pending_pose = None
        self._clear_drift_observation()
        self._drift_armed = True
        self._drift_healthy_since = None
        self._last_auto_reseed_at = None
        if self._retry_timer is not None:
            self._retry_timer.cancel()
            self._retry_timer = None
        return super().on_deactivate(state)

    def _on_localize(self, _request, response):
        response.success = self._begin_localization()
        response.message = "ground-truth pose published" if response.success else "no odom yet"
        return response

    def _begin_localization(self) -> bool:
        if not self._publish_pose():
            return False
        if self._retry_timer is None:
            self._retry_timer = self.create_timer(2.0, self._publish_pose)
        return True

    def _publish_pose(self) -> bool:
        if self._last_odom is None or self._pose_pub is None:
            return False
        msg = PoseWithCovarianceStamped()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = "map"
        msg.pose.pose = self._last_odom.pose.pose
        msg.pose.covariance[0] = msg.pose.covariance[7] = 0.01
        msg.pose.covariance[35] = 0.01
        pose = msg.pose.pose
        self._pending_pose = (pose.position.x, pose.position.y, _yaw(pose))
        self._pose_pub.publish(msg)
        self.get_logger().info("published ground-truth /initialpose")
        return True


def main():
    rclpy.init()
    node = GridLocalizerSim()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
