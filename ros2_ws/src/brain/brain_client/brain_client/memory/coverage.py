# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Visibility paint over the occupancy grid. Pure: no ROS, no I/O.

Every kept memory "paints" the floor its camera saw — a field-of-view wedge
raycast from its pose until walls (or unknown, or range) stop each ray. The
union of that paint is what the robot's memory can vouch for, and coverage —
not pose arithmetic — is the ground truth for novelty: a picture is worth
taking when the wedge in front of the camera is mostly unpainted, and the
memories worth keeping are the ones whose paint nothing else replaces.

Masks are cached per memory keyed by (id, stamp), pruned to the live set, and
rebuilt when the grid object changes; the steady-state cost per assessment is
one wedge raster and one masked count.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

import cv2
import numpy as np

if TYPE_CHECKING:
    from collections.abc import Sequence

    from brain_client.memory.store import Memory
    from brain_client.state.map import Map

OCCUPIED_THRESHOLD = 50  # nav grid: -1 unknown, 0 free, 100 occupied
_CAMERA_FOV_RAD = math.radians(128.0)
_PAINT_RANGE_M = 4.0  # beyond this the picture stops carrying useful floor detail
_RAY_COUNT = 96


@dataclass(frozen=True)
class CoverageView:
    """What one pose's camera wedge amounts to against the kept paint."""

    painted_fraction: float  # how much of the wedge the memories already show
    visible_m2: float  # how much floor the wedge paints at all


class Coverage:
    """Paint bookkeeping over the live memory set. Owned by the recorder;
    everything here is derived — forgetting a memory un-paints it."""

    def __init__(self) -> None:
        self._grid: Map | None = None
        self._masks: dict[tuple[int, float], np.ndarray] = {}

    def assess(self, memories: Sequence[Memory], grid: Map, x: float, y: float, theta: float) -> CoverageView | None:
        """This pose's wedge against the union of kept paint; None when the
        grid is unreadable. A pose that sees nothing reads as fully painted —
        a blind spot earns no slot."""
        wedge = wedge_mask(grid, x, y, theta)
        if wedge is None:
            return None
        visible = int(wedge.sum())
        if visible == 0:
            return CoverageView(painted_fraction=1.0, visible_m2=0.0)
        painted = self._union(memories, grid)
        if painted is None:
            return None
        fraction = float((wedge & painted).sum()) / visible
        return CoverageView(painted_fraction=fraction, visible_m2=visible * grid.resolution**2)

    def least_unique(self, memories: Sequence[Memory], grid: Map) -> Memory | None:
        """The memory whose paint is most replaceable — evicting it loses the
        least coverage; None when any mask is unavailable."""
        masks: list[tuple[Memory, np.ndarray]] = []
        for memory in memories:
            mask = self._mask(memory, grid)
            if mask is None:
                return None
            masks.append((memory, mask))
        cheapest: Memory | None = None
        cheapest_unique = 0
        for memory, mask in masks:
            others = np.zeros_like(mask)
            for other, other_mask in masks:
                if other.id != memory.id:
                    others |= other_mask
            unique = int((mask & ~others).sum())
            if cheapest is None or unique < cheapest_unique:
                cheapest, cheapest_unique = memory, unique
        return cheapest

    def _union(self, memories: Sequence[Memory], grid: Map) -> np.ndarray | None:
        live = {(m.id, m.stamp) for m in memories}
        for stale in [key for key in self._masks if key not in live]:
            del self._masks[stale]
        union = np.zeros((grid.height, grid.width), dtype=bool)
        for memory in memories:
            mask = self._mask(memory, grid)
            if mask is None:
                return None
            union |= mask
        return union

    def _mask(self, memory: Memory, grid: Map) -> np.ndarray | None:
        if grid is not self._grid:  # a new map message repaints the world
            self._grid = grid
            self._masks.clear()
        key = (memory.id, memory.stamp)
        mask = self._masks.get(key)
        if mask is None:
            mask = wedge_mask(grid, memory.x, memory.y, memory.theta)
            if mask is not None:
                self._masks[key] = mask
        return mask


def wedge_mask(grid: Map, x: float, y: float, theta: float) -> np.ndarray | None:
    """The known-free cells this pose's camera sees, as a boolean grid mask —
    rays fan across the FOV and stop at occupied, unknown, out-of-map, or
    range; the swept wedge is filled between the origin and the ray ends."""
    cells = grid.grid
    if cells is None:
        return None
    points = [_to_cell(grid, x, y)]
    for i in range(_RAY_COUNT):
        angle = theta - _CAMERA_FOV_RAD / 2 + _CAMERA_FOV_RAD * i / (_RAY_COUNT - 1)
        points.append(_ray_end(grid, cells, x, y, angle))
    mask = np.zeros((grid.height, grid.width), dtype=np.uint8)
    cv2.fillPoly(mask, [np.array(points, dtype=np.int32)], (1.0,))
    free = (cells >= 0) & (cells < OCCUPIED_THRESHOLD)
    return mask.astype(bool) & free


def _ray_end(grid: Map, cells: np.ndarray, x: float, y: float, angle: float) -> tuple[int, int]:
    """The last known-free cell along the ray before something stops it."""
    step = grid.resolution
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    end = _to_cell(grid, x, y)
    for i in range(1, int(_PAINT_RANGE_M / step) + 1):
        col, row = _to_cell(grid, x + cos_a * step * i, y + sin_a * step * i)
        if not (0 <= row < grid.height and 0 <= col < grid.width):
            break
        value = int(cells[row, col])
        if value < 0 or value >= OCCUPIED_THRESHOLD:
            break
        end = (col, row)
    return end


def _to_cell(grid: Map, x: float, y: float) -> tuple[int, int]:
    return int((x - grid.origin_x) / grid.resolution), int((y - grid.origin_y) / grid.resolution)
