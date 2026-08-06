# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Skill-facing lidar state. ROS-free on purpose."""

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Lidar:
    """One lidar sweep, read via ``self.lidar`` in skills.

    Beam ``i`` points at ``angle_min + i * angle_increment`` radians in the
    scan frame (counter-clockwise positive, 0 = the lidar's forward axis).
    Invalid returns appear as inf/0 in ``ranges``; use :meth:`min_range`,
    which filters them.
    """

    ranges: tuple
    """Measured distances in meters, one per beam."""
    angle_min: float
    """Angle of beam 0 in radians."""
    angle_increment: float
    """Angle between consecutive beams in radians."""
    range_min: float
    """Sensor minimum valid distance in meters."""
    range_max: float
    """Sensor maximum valid distance in meters."""
    stamp: float = 0.0
    """Sensor timestamp in seconds (ROS time)."""
    frame_id: str = ""

    def min_range(self, angle_from_deg: float | None = None, angle_to_deg: float | None = None) -> float | None:
        """Closest valid return in meters, or None if the sweep has none.

        Optionally restrict to the sector from ``angle_from_deg`` to
        ``angle_to_deg`` (degrees, scan frame, counter-clockwise). The pair
        may wrap through +-180: ``min_range(150, -150)`` looks behind the
        robot, ``min_range(-30, 30)`` ahead.
        """
        lo = -180.0 if angle_from_deg is None else angle_from_deg
        hi = 180.0 if angle_to_deg is None else angle_to_deg
        best = None
        for i, r in enumerate(self.ranges):
            if not math.isfinite(r) or not self.range_min <= r <= self.range_max:
                continue
            angle = _wrap_deg(math.degrees(self.angle_min + i * self.angle_increment))
            in_sector = lo <= angle <= hi if lo <= hi else (angle >= lo or angle <= hi)
            if in_sector and (best is None or r < best):
                best = r
        return best


def _wrap_deg(angle: float) -> float:
    """Wrap an angle in degrees to [-180, 180)."""
    return (angle + 180.0) % 360.0 - 180.0
