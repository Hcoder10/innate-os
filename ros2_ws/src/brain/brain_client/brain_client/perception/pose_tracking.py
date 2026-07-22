# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Robot pose source: TF (map->base_link) with odometry fallback in mapfree mode.

Owns the on-demand ``/odom`` and nav-mode subscriptions and the 30 Hz transform
timer (created via :meth:`start` when the brain activates, torn down via
:meth:`stop`). The pure ``(x, y, theta)`` math lives in :mod:`perception.pose`;
this module is the ROS-facing source of poses.
"""

from __future__ import annotations

import traceback

import rclpy
from nav_msgs.msg import Odometry
from rclpy.duration import Duration
from std_msgs.msg import String
from tf2_ros import TransformException
from tf2_ros.buffer import Buffer
from tf2_ros.transform_listener import TransformListener

from brain_client.common.geometry import quaternion_to_yaw

Pose = tuple[float, float, float]


class PoseTracker:
    def __init__(self, node, *, odom_topic: str, nav_mode_topic: str):
        self._node = node
        self._logger = node.get_logger()
        self._odom_topic = odom_topic
        self._nav_mode_topic = nav_mode_topic

        self.last_odom: Odometry | None = None
        self.cur_nav_mode: str | None = None

        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, node)

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
        # Destroying here is safe only because brain_client_node is spun
        # single-threaded and stop() runs on that spin thread (between
        # callbacks). On a multi-threaded executor this would race the wait
        # set (InvalidHandle) — flag-gate the callbacks instead, like
        # skills/robot_state.py.
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
    def _on_odom(self, msg: Odometry) -> None:
        self.last_odom = msg

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
