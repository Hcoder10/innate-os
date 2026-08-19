"""Export the apartment occupancy map as a nav2 map_server map
(sim_apartment.yaml + .pgm) into sim/assets/map/. The tmux launch script
seeds it into $INNATE_OS_ROOT/data/maps so the mode manager boots straight
into navigation mode with it.

Usage: cd sim && uv run tools/export_nav_map.py
"""

import sys
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sandbox"))
import _driver_pkg  # noqa: F401
from mars_sim_driver import world
from mars_sim_driver.core import VirtualMars

RESOLUTION = 0.05
PGM_OCCUPIED = 0
PGM_UNKNOWN = 205
PGM_FREE = 254
OCCUPIED_THRESHOLD = 0.65
# 205 encodes an occupancy probability of 50/255 ~= 0.196078. Keep the free
# threshold just below it so map_server loads 205 as unknown, not free.
FREE_THRESHOLD = 0.196
MIN_SPAWN_COMPONENT_FRACTION = 0.95
# Representative floor in each region the household search must be able to
# reach. These are apartment topology checks, not resident locations.
NAVIGABLE_ROOM_SAMPLES = {
    "northwest room": (-4.00, 4.00),
    "south room": (-1.50, -3.00),
    "northeast room": (-1.00, 3.50),
}
REGRESSION_EXTERIOR_GOALS = (
    (-8.7134, 0.7144),
    (-7.2134, -1.0356),
    (1.29, -3.79),
    (2.29, -3.54),
    (-1.21, -5.29),
    (0.29, -5.54),
    # The visual apartment mesh includes a southwest exterior courtyard. It
    # looks like authored floor to the ray caster, but is outside the home.
    (-5.9634, -3.7856),
    (-5.2134, -3.7856),
    (-4.93, -1.35),
)

# Two open visual thresholds connect the southwest exterior courtyard to the
# apartment. Close those semantic boundaries before flood-filling from the
# exterior seed. The static map then publishes the courtyard as unknown, so
# Nav2, skill-facing maps, and the web UI all share one indoor boundary.
EXTERIOR_FLOOD_SEEDS = ((-5.9634, -3.7856),)
EXTERIOR_BOUNDARY_POLYLINES = (
    ((-4.7134, -1.5356), (-5.3634, -0.8856), (-5.0634, -0.6856)),
    ((-3.3634, -3.7856), (-3.5134, -3.6356)),
)
EXTERIOR_BOUNDARY_HALF_WIDTH_M = 0.10


def _cell_index(grid: np.ndarray, ox: float, oy: float, x: float, y: float) -> tuple[int, int] | None:
    col = int(np.floor((x - ox) / RESOLUTION))
    row = int(np.floor((y - oy) / RESOLUTION))
    if not (0 <= row < grid.shape[0] and 0 <= col < grid.shape[1]):
        return None
    return row, col


def _cell_value(grid: np.ndarray, ox: float, oy: float, x: float, y: float) -> int | None:
    index = _cell_index(grid, ox, oy, x, y)
    return None if index is None else int(grid[index])


def _free_component(grid: np.ndarray, start: tuple[int, int]) -> set[tuple[int, int]]:
    """Return the four-connected known-free component containing ``start``."""
    if grid[start] != 0:
        return set()
    reached = {start}
    pending = deque([start])
    while pending:
        row, col = pending.popleft()
        for next_row, next_col in ((row - 1, col), (row + 1, col), (row, col - 1), (row, col + 1)):
            candidate = (next_row, next_col)
            if (
                0 <= next_row < grid.shape[0]
                and 0 <= next_col < grid.shape[1]
                and candidate not in reached
                and grid[candidate] == 0
            ):
                reached.add(candidate)
                pending.append(candidate)
    return reached


def _distance_to_segment(
    x: np.ndarray,
    y: np.ndarray,
    start: tuple[float, float],
    end: tuple[float, float],
) -> np.ndarray:
    """Vectorized distance from cell centers to a finite world-space segment."""
    start_x, start_y = start
    end_x, end_y = end
    dx = end_x - start_x
    dy = end_y - start_y
    length_squared = dx * dx + dy * dy
    if length_squared <= 0.0:
        return np.hypot(x - start_x, y - start_y)
    fraction = np.clip(((x - start_x) * dx + (y - start_y) * dy) / length_squared, 0.0, 1.0)
    return np.hypot(x - (start_x + fraction * dx), y - (start_y + fraction * dy))


def _apply_indoor_boundary(grid: np.ndarray, ox: float, oy: float) -> np.ndarray:
    """Turn the authored southwest exterior floor into unknown map space."""
    rows, cols = grid.shape
    xs = ox + (np.arange(cols) + 0.5) * RESOLUTION
    ys = oy + (np.arange(rows) + 0.5) * RESOLUTION
    cell_x, cell_y = np.meshgrid(xs, ys)

    boundary = np.zeros(grid.shape, dtype=bool)
    for polyline in EXTERIOR_BOUNDARY_POLYLINES:
        for start, end in zip(polyline, polyline[1:], strict=False):
            boundary |= _distance_to_segment(cell_x, cell_y, start, end) <= EXTERIOR_BOUNDARY_HALF_WIDTH_M

    floodable = (grid == 0) & ~boundary
    exterior = np.zeros(grid.shape, dtype=bool)
    pending: deque[tuple[int, int]] = deque()
    for x, y in EXTERIOR_FLOOD_SEEDS:
        seed = _cell_index(grid, ox, oy, x, y)
        if seed is None or not floodable[seed]:
            raise RuntimeError(f"southwest exterior seed ({x:.2f}, {y:.2f}) is not known-free")
        if not exterior[seed]:
            exterior[seed] = True
            pending.append(seed)

    # Treat diagonal contact as connected. This is deliberately stricter than
    # Nav2: the semantic boundary must close even a one-cell diagonal squeeze.
    while pending:
        row, col = pending.popleft()
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if not (dr or dc):
                    continue
                next_row = row + dr
                next_col = col + dc
                if (
                    0 <= next_row < rows
                    and 0 <= next_col < cols
                    and floodable[next_row, next_col]
                    and not exterior[next_row, next_col]
                ):
                    exterior[next_row, next_col] = True
                    pending.append((next_row, next_col))

    for label, (x, y) in {"spawn": (world.SPAWN_X, world.SPAWN_Y), **NAVIGABLE_ROOM_SAMPLES}.items():
        cell = _cell_index(grid, ox, oy, x, y)
        if cell is None or exterior[cell] or boundary[cell]:
            raise RuntimeError(f"indoor boundary incorrectly excludes {label} sample ({x:.2f}, {y:.2f})")

    result = grid.copy()
    result[(exterior | boundary) & (result == 0)] = -1
    return result


def _validate_navigation_grid(grid: np.ndarray, ox: float, oy: float) -> None:
    """Fail the asset build when apartment safety invariants regress."""
    values = {int(value) for value in np.unique(grid)}
    if not values <= {-1, 0, 100} or not np.any(grid == 0):
        raise RuntimeError(f"navigation grid has invalid values or no free floor: {sorted(values)}")

    if any(np.any(edge != -1) for edge in (grid[0, :], grid[-1, :], grid[:, 0], grid[:, -1])):
        raise RuntimeError("navigation grid outer border is not entirely unknown")
    if _cell_value(grid, ox, oy, world.SPAWN_X, world.SPAWN_Y) != 0:
        raise RuntimeError("robot spawn is not known-free on the navigation grid")

    spawn_index = _cell_index(grid, ox, oy, world.SPAWN_X, world.SPAWN_Y)
    assert spawn_index is not None  # established as known-free above
    reachable = _free_component(grid, spawn_index)
    total_free = int(np.count_nonzero(grid == 0))
    reachable_fraction = len(reachable) / total_free
    if reachable_fraction < MIN_SPAWN_COMPONENT_FRACTION:
        raise RuntimeError(
            f"only {reachable_fraction:.0%} of known-free floor connects to spawn; "
            f"expected at least {MIN_SPAWN_COMPONENT_FRACTION:.0%}"
        )
    for label, (x, y) in NAVIGABLE_ROOM_SAMPLES.items():
        if _cell_index(grid, ox, oy, x, y) not in reachable:
            raise RuntimeError(f"{label} sample ({x:.2f}, {y:.2f}) is not reachable from spawn")

    for x, y in REGRESSION_EXTERIOR_GOALS:
        if _cell_value(grid, ox, oy, x, y) != -1:
            raise RuntimeError(f"known exterior regression goal ({x:.2f}, {y:.2f}) is not unknown")


def _validate_pgm_roundtrip(grid: np.ndarray, img: np.ndarray) -> None:
    """Mirror map_server's trinary thresholds and catch unsafe encodings."""
    occupancy = (255.0 - img[::-1].astype(np.float32)) / 255.0
    decoded = np.full(grid.shape, -1, dtype=np.int8)
    decoded[occupancy < FREE_THRESHOLD] = 0
    decoded[occupancy > OCCUPIED_THRESHOLD] = 100
    if not np.array_equal(decoded, grid):
        mismatched = int(np.count_nonzero(decoded != grid))
        raise RuntimeError(f"PGM/YAML roundtrip changes {mismatched} navigation cells")


def main() -> None:
    sim = VirtualMars()
    # Lidar-consistent map (virtual SLAM at the laser's true height): AMCL
    # localizes against what the lidar actually returns, exactly like a real
    # robot localizing against its own SLAM map. occupancy_grid() (collision
    # slab) systematically disagrees with the lidar around furniture and
    # walks AMCL off the map.
    grid, ox, oy = sim.lidar_occupancy_grid(RESOLUTION)
    grid = _apply_indoor_boundary(grid, ox, oy)
    _validate_navigation_grid(grid, ox, oy)
    # map_server PGM, row 0 at the TOP. The threshold validation is essential:
    # with free_thresh=0.25, conventional gray 205 silently reloads as free.
    img = np.where(grid == 100, PGM_OCCUPIED, np.where(grid == 0, PGM_FREE, PGM_UNKNOWN)).astype(np.uint8)[::-1]
    _validate_pgm_roundtrip(grid, img)
    out = Path(__file__).resolve().parents[1] / "assets" / "map"
    out.mkdir(parents=True, exist_ok=True)
    Image.fromarray(img).save(out / "sim_apartment.pgm")
    (out / "sim_apartment.yaml").write_text(
        f"image: sim_apartment.pgm\nmode: trinary\nresolution: {RESOLUTION}\n"
        f"origin: [{ox:.4f}, {oy:.4f}, 0.0]\nnegate: 0\n"
        f"occupied_thresh: {OCCUPIED_THRESHOLD}\nfree_thresh: {FREE_THRESHOLD}\n"
    )
    print(f"wrote {out}/sim_apartment.yaml ({grid.shape[1]}x{grid.shape[0]} @ {RESOLUTION}m)")


if __name__ == "__main__":
    main()
