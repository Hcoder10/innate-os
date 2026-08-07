# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Which viewpoints earn a slot in the spatial memory. Pure: no ROS, no I/O.

A memory earns its place by showing something no kept frame shows. Two
viewpoints are redundant only when they are both close AND similarly oriented —
either gap alone (same spot facing away, same heading across the room) makes a
genuinely different view. Capacity is bounded; at the cap, the older half of
the most redundant pair makes room.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from brain_client.memory.store import Memory

MAX_MEMORIES = 50
_REDUNDANT_DISTANCE_M = 1.0
_REDUNDANT_ANGLE_RAD = math.radians(100.0)  # most of the camera's 128 deg FOV still overlaps within this
_REFRESH_AGE_SEC = 30 * 60.0  # a revisited viewpoint older than this gets a fresh image


@dataclass(frozen=True)
class Admission:
    """What to do with a candidate viewpoint; replace/evict name existing memories."""

    record: bool
    replace: Memory | None = None  # same viewpoint, stale image: overwrite in place
    evict: Memory | None = None  # at capacity: remove this one to make room


_SKIP = Admission(record=False)


def plan_admission(memories: Sequence[Memory], x: float, y: float, theta: float, stamp: float) -> Admission:
    nearest = min(memories, key=lambda m: redundancy(m, x, y, theta), default=None)
    if nearest is not None and redundancy(nearest, x, y, theta) < 1.0:
        if stamp - nearest.stamp < _REFRESH_AGE_SEC:
            return _SKIP
        return Admission(record=True, replace=nearest)
    if len(memories) < MAX_MEMORIES:
        return Admission(record=True)
    return Admission(record=True, evict=_most_redundant(memories))


def redundancy(memory: Memory, x: float, y: float, theta: float) -> float:
    """How interchangeable a memory is with this viewpoint; < 1 means redundant.

    The max of the normalized position and heading gaps: both must be small
    for the views to overlap, so either being large keeps the pair distinct.
    """
    position = math.hypot(memory.x - x, memory.y - y) / _REDUNDANT_DISTANCE_M
    heading = _angle_diff(memory.theta, theta) / _REDUNDANT_ANGLE_RAD
    return max(position, heading)


def _most_redundant(memories: Sequence[Memory]) -> Memory:
    """The older half of the closest pair — evicting it costs the least coverage."""
    closest = min(
        ((a, b) for i, a in enumerate(memories) for b in memories[i + 1 :]),
        key=lambda pair: redundancy(pair[0], pair[1].x, pair[1].y, pair[1].theta),
    )
    return min(closest, key=lambda m: m.stamp)


def _angle_diff(a: float, b: float) -> float:
    diff = abs(a - b) % (2 * math.pi)
    return min(diff, 2 * math.pi - diff)
