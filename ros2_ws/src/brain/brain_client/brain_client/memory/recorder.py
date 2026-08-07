# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Builds the spatial memory while the robot drives around a mapped area.

Always on, independent of brain activation: a webapp teleop tour must build
memories too, so the recorder owns its own lightweight subscriptions instead
of borrowing the brain's activation-gated sensors. One rule governs capture —
record only what a well-localized robot saw: AMCL's covariance must hold below
the webapp's "confident" thresholds for a few seconds straight, then the
admission policy (memory/selection.py) decides novelty, refresh, and eviction.

Also republishes the memory positions at 1 Hz on ``/brain/memory_positions``
(the topic the mobile app's map overlay watches) and nudges the search's
context cache to re-warm once recording quiets down.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from geometry_msgs.msg import PoseWithCovarianceStamped
from rclpy.qos import QoSDurabilityPolicy, QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from sensor_msgs.msg import CompressedImage
from std_msgs.msg import String

from brain_client.memory.selection import plan_admission

if TYPE_CHECKING:
    from collections.abc import Callable

    from rclpy.node import Node
    from rclpy.publisher import Publisher

    from brain_client.core.config import BrainConfig
    from brain_client.memory.store import MemoryStore
    from brain_client.perception.pose_tracking import PoseTracker

_TICK_SEC = 1.0
_POSITION_VAR_MAX = 0.10  # m² — the webapp's "confident" localization threshold
_YAW_VAR_MAX = 0.18  # rad² — from the cloud-era pose graph gate
_CONFIDENT_FOR_SEC = 3.0  # covariance must hold below the thresholds this long before recording
_FRESH_FRAME_SEC = 2.5  # the compressed feed runs ~7.5 Hz; older means it died
_MIN_HEAD_PITCH_DEG = -25.0  # looking further down films the floor, not the room


class MemoryRecorder:
    def __init__(
        self,
        node: Node,
        config: BrainConfig,
        *,
        store: MemoryStore,
        pose_tracker: PoseTracker,
        warm_search: Callable[[], None] | None,
        positions_pub: Publisher,
    ):
        self._logger = node.get_logger()
        self._store = store
        self._pose = pose_tracker
        self._warm_search = warm_search
        self._positions_pub = positions_pub

        self._frame: tuple[float, bytes] | None = None  # (monotonic arrival, jpeg)
        self._nav_mode: str | None = None
        self._map_name = ""
        self._covariance: tuple[float, float, float] | None = None  # (var x, var y, var yaw)
        self._head_pitch = 0.0
        self._confident_since: float | None = None

        image_qos = QoSProfile(
            reliability=QoSReliabilityPolicy.BEST_EFFORT, history=QoSHistoryPolicy.KEEP_LAST, depth=1
        )
        # The forked AMCL latches its pose; matching TRANSIENT_LOCAL hands a
        # late-joining recorder the last covariance instead of silence.
        amcl_qos = QoSProfile(
            reliability=QoSReliabilityPolicy.RELIABLE,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=1,
            durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
        )
        node.create_subscription(CompressedImage, config.image_topic, self._on_image, image_qos)
        node.create_subscription(String, config.current_nav_mode_topic, self._on_nav_mode, 10)
        node.create_subscription(String, config.current_map_topic, self._on_current_map, 10)
        node.create_subscription(PoseWithCovarianceStamped, config.amcl_pose_topic, self._on_amcl_pose, amcl_qos)
        node.create_subscription(String, "/mars/head/current_position", self._on_head, 10)
        node.create_timer(_TICK_SEC, self.tick)

    # --- callbacks ---
    def _on_image(self, msg: CompressedImage) -> None:
        if msg.data:
            self._frame = (time.monotonic(), bytes(msg.data))

    def _on_nav_mode(self, msg: String) -> None:
        self._nav_mode = msg.data

    def _on_current_map(self, msg: String) -> None:
        self._map_name = msg.data

    def _on_amcl_pose(self, msg: PoseWithCovarianceStamped) -> None:
        covariance = msg.pose.covariance
        self._covariance = (covariance[0], covariance[7], covariance[35])

    def _on_head(self, msg: String) -> None:
        try:
            self._head_pitch = float(json.loads(msg.data)["current_position"])
        except (json.JSONDecodeError, TypeError, ValueError, KeyError):
            pass  # keep the last known pitch; one corrupt message must not flip the gate

    # --- the 1 Hz tick ---
    def tick(self) -> None:
        self._store.switch_map(self._map_name or None)
        self._maybe_record()
        self._publish_positions()
        if self._warm_search is not None:
            self._warm_search()

    def _maybe_record(self) -> None:
        if not self._localized_long_enough():
            return
        jpeg = self._fresh_jpeg()
        pose = self._pose.map_pose_xyt()
        if jpeg is None or pose is None or self._head_pitch < _MIN_HEAD_PITCH_DEG:
            return
        self._record(*pose, jpeg)

    def _localized_long_enough(self) -> bool:
        if self._nav_mode != "navigation" or not self._map_name or not self._confident():
            self._confident_since = None
            return False
        now = time.monotonic()
        if self._confident_since is None:
            self._confident_since = now
        return now - self._confident_since >= _CONFIDENT_FOR_SEC

    def _confident(self) -> bool:
        if self._covariance is None:
            return False
        var_x, var_y, var_yaw = self._covariance
        return max(var_x, var_y) <= _POSITION_VAR_MAX and var_yaw <= _YAW_VAR_MAX

    def _fresh_jpeg(self) -> bytes | None:
        frame = self._frame
        if frame is None or time.monotonic() - frame[0] > _FRESH_FRAME_SEC:
            return None
        return frame[1]

    def _record(self, x: float, y: float, theta: float, jpeg: bytes) -> None:
        plan = plan_admission(self._store.snapshot().memories, x, y, theta, time.time())
        if not plan.record:
            return
        if plan.replace is not None:
            self._store.replace(plan.replace, x, y, theta, time.time(), jpeg)
            self._logger.info(f"[Memory] refreshed viewpoint {plan.replace.id} at ({x:.2f}, {y:.2f})")
            return
        if plan.evict is not None:
            self._store.evict(plan.evict)
        memory = self._store.add(x, y, theta, time.time(), jpeg)
        if memory is not None:
            evicted = f", evicted {plan.evict.id}" if plan.evict is not None else ""
            self._logger.info(f"[Memory] recorded viewpoint {memory.id} at ({x:.2f}, {y:.2f}){evicted}")

    def _publish_positions(self) -> None:
        payload = {"map": self._map_name, "positions": self._store.positions()}
        self._positions_pub.publish(String(data=json.dumps(payload)))
