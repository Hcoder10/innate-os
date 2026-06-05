"""Robot pose source: TF (map->base_link), odometry, and AMCL covariance.

Owns the always-on ``/amcl_pose`` subscription plus the on-demand ``/odom`` and
nav-mode subscriptions and the 30 Hz transform timer (created via :meth:`start`
when the brain activates, torn down via :meth:`stop`). The pure ``(x, y, theta)``
math lives in :mod:`perception.pose`; this module is the ROS-facing source of poses.
"""

from __future__ import annotations

import traceback

import rclpy
from geometry_msgs.msg import PoseWithCovariance, PoseWithCovarianceStamped
from nav_msgs.msg import Odometry
from rclpy.duration import Duration
from rclpy.qos import QoSDurabilityPolicy, QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from std_msgs.msg import String
from tf2_ros import TransformException
from tf2_ros.buffer import Buffer
from tf2_ros.transform_listener import TransformListener

from brain_client.common.geometry import quaternion_to_yaw

Pose = tuple[float, float, float]


class PoseTracker:
    def __init__(
        self, node, *, amcl_pose_topic: str, odom_topic: str, nav_mode_topic: str, use_odom_as_amcl_pose: bool
    ):
        self._node = node
        self._logger = node.get_logger()
        self._odom_topic = odom_topic
        self._nav_mode_topic = nav_mode_topic
        self._use_odom_as_amcl_pose = use_odom_as_amcl_pose

        self.last_odom: Odometry | None = None
        self.last_amcl_pose: PoseWithCovarianceStamped | None = None
        self.cur_nav_mode: str | None = None

        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, node)

        # AMCL publishes TRANSIENT_LOCAL; match it to receive the latched pose.
        amcl_qos = QoSProfile(
            reliability=QoSReliabilityPolicy.RELIABLE,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=1,
            durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
        )
        self._amcl_sub = node.create_subscription(
            PoseWithCovarianceStamped, amcl_pose_topic, self._on_amcl_pose, amcl_qos
        )

        self._odom_sub = None
        self._nav_mode_sub = None
        self._transform_timer = None

    # --- on-demand lifecycle (brain active) ---
    def start(self) -> None:
        if self._odom_sub is not None:
            return
        self._odom_sub = self._node.create_subscription(Odometry, self._odom_topic, self._on_odom, 10)
        self._nav_mode_sub = self._node.create_subscription(String, self._nav_mode_topic, self._on_nav_mode, 10)
        self._transform_timer = self._node.create_timer(1.0 / 30.0, self._fetch_transform)

    def stop(self) -> None:
        for sub in (self._odom_sub, self._nav_mode_sub):
            if sub is not None:
                self._node.destroy_subscription(sub)
        self._odom_sub = None
        self._nav_mode_sub = None
        if self._transform_timer is not None:
            self._transform_timer.cancel()
            self._transform_timer = None

    @property
    def is_mapfree(self) -> bool:
        return self.cur_nav_mode == "mapfree"

    # --- callbacks ---
    def _on_amcl_pose(self, msg: PoseWithCovarianceStamped) -> None:
        self.last_amcl_pose = msg

    def _on_odom(self, msg: Odometry) -> None:
        self.last_odom = msg
        if self._use_odom_as_amcl_pose:
            self.last_amcl_pose = self.last_odom

    def _on_nav_mode(self, msg: String) -> None:
        self.cur_nav_mode = msg.data
        self._logger.debug(f"Current Navigation Mode is {self.cur_nav_mode}")

    def _fetch_transform(self) -> None:
        """30 Hz: refresh ``last_odom`` from the map->base_link TF (non-mapfree)."""
        try:
            if self.cur_nav_mode == "mapfree":
                return
            base, mapf, when = "base_link", "map", rclpy.time.Time()
            if self.tf_buffer.can_transform(mapf, base, when, timeout=Duration(seconds=0.1)):
                t = self.tf_buffer.lookup_transform(mapf, base, when, timeout=Duration(seconds=0.1))
                odom = Odometry()
                odom.header.stamp = self._node.get_clock().now().to_msg()
                odom.header.frame_id = mapf
                odom.child_frame_id = base
                odom.pose.pose.position.x = t.transform.translation.x
                odom.pose.pose.position.y = t.transform.translation.y
                odom.pose.pose.position.z = t.transform.translation.z
                odom.pose.pose.orientation = t.transform.rotation
                self.last_odom = odom
            else:
                self._logger.warn(f"Could not get transform '{base}'->'{mapf}'. Waiting...")
        except TransformException as ex:
            self._logger.error(f"TransformException '{base}'->'{mapf}': {ex}")
        except Exception as e:
            self._logger.error(f"Error in _fetch_transform: {e}, {traceback.format_exc()}")

    # --- pose queries ---
    def current_pose_xyt(self) -> Pose | None:
        """Current robot pose as (x, y, theta); None if unavailable."""
        try:
            if self.is_mapfree and self.last_odom is not None:
                pos = self.last_odom.pose.pose.position
                ori = self.last_odom.pose.pose.orientation
            else:
                transform = self.tf_buffer.lookup_transform(
                    target_frame="map",
                    source_frame="base_link",
                    time=rclpy.time.Time(),
                    timeout=rclpy.time.Duration(seconds=0.5),
                )
                pos = transform.transform.translation
                ori = transform.transform.rotation
            return (pos.x, pos.y, quaternion_to_yaw(ori))
        except Exception:
            return None

    def pose_source(self, log_prefix: str = "") -> tuple[bool, tuple | None]:
        """Determine the pose source for the navigation payload.

        Returns ``(use_mapfree, pose_source)`` where ``pose_source`` is
        ``("tf"|"odom", pose_msg)`` or None when no pose is available.
        """
        use_mapfree = self.is_mapfree

        if not use_mapfree:
            try:
                transform = self.tf_buffer.lookup_transform(
                    target_frame="map",
                    source_frame="base_link",
                    time=rclpy.time.Time(),
                    timeout=rclpy.time.Duration(seconds=0.5),
                )
                pose_with_cov = PoseWithCovariance()
                pose_with_cov.pose.position.x = transform.transform.translation.x
                pose_with_cov.pose.position.y = transform.transform.translation.y
                pose_with_cov.pose.position.z = transform.transform.translation.z
                pose_with_cov.pose.orientation = transform.transform.rotation
                if self.last_amcl_pose is not None:
                    pose_with_cov.covariance = self.last_amcl_pose.pose.covariance
                return use_mapfree, ("tf", pose_with_cov)
            except Exception as tf_err:
                self._logger.debug(f"{log_prefix}TF map->base_link not available: {tf_err}")
                return use_mapfree, None
        elif self.last_odom is not None:
            try:
                cov = getattr(self.last_odom.pose, "covariance", None)
                needs_set = cov is None
                if not needs_set and hasattr(cov, "__len__"):
                    needs_set = len(cov) < 36
                if needs_set:
                    self.last_odom.pose.covariance = [1e4] * 36
            except Exception:
                pass
            return use_mapfree, ("odom", self.last_odom.pose)
        return use_mapfree, None
