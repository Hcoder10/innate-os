# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Robot forward reach: the max +x of the live /footprint hull.

dynamic_footprint publishes the robot's convex hull (base_link frame, arm
included, padded) at 15 Hz; the same polygon nav2's costmaps use. The brain
reads its front extent to size the go_to_point_in_view standoff: with the arm
out the gripper leads the bumper by up to ~25 cm, and a goal placed from a
base_link constant put it into a wall (R7-44, 2026-08-06).

A stale or absent feed reads as None, and the grounding falls back to the
worst-case reach — degrading to stopping early, never to driving too far.
"""

from __future__ import annotations

import time

from geometry_msgs.msg import Polygon

# The publisher runs at 15 Hz; this long without a message means the node is
# down (or the brain just started), not slow.
_FRESH_SEC = 2.0


class FootprintTracker:
    def __init__(self, node, *, footprint_topic: str):
        self._node = node
        self._footprint_topic = footprint_topic
        self._sub = None
        # (monotonic arrival, max x, max |y|): forward reach and half-width.
        self._extents: tuple[float, float, float] | None = None

    # --- on-demand lifecycle (brain active) ---
    def start(self) -> None:
        if self._sub is not None:
            return
        self._sub = self._node.create_subscription(Polygon, self._footprint_topic, self._on_footprint, 10)

    def stop(self) -> None:
        # Same single-threaded-executor contract as CameraCapture.stop().
        if self._sub is not None:
            self._node.destroy_subscription(self._sub)
        self._sub = None
        self._extents = None

    def _on_footprint(self, msg: Polygon) -> None:
        if msg.points:
            self._extents = (
                time.monotonic(),
                max(p.x for p in msg.points),
                max(abs(p.y) for p in msg.points),
            )

    def _fresh(self) -> tuple[float, float, float] | None:
        if self._extents is None or time.monotonic() - self._extents[0] > _FRESH_SEC:
            return None
        return self._extents

    def front_extent(self) -> float | None:
        """Forward reach from base_link in metres; None when the feed is stale."""
        extents = self._fresh()
        return None if extents is None else extents[1]

    def half_width(self) -> float | None:
        """Half-width of the hull in metres; None when the feed is stale."""
        extents = self._fresh()
        return None if extents is None else extents[2]
