"""Export the apartment occupancy map as a nav2 map_server map
(sim_apartment.yaml + .pgm) into sim/assets/map/. The tmux launch script
seeds it into $INNATE_OS_ROOT/data/maps so the mode manager boots straight
into navigation mode with it.

Usage: cd sim && uv run tools/export_nav_map.py
"""

import json
import sys
from collections import deque
from dataclasses import dataclass
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
DEFAULT_NAVIGATION_ENVIRONMENT = Path(__file__).resolve().parents[1] / "environments" / "apartment" / "navigation.json"


@dataclass(frozen=True)
class NavigationEnvironment:
    navigable_samples: dict[str, tuple[float, float]]
    expected_unknown_samples: tuple[tuple[float, float], ...]
    exterior_flood_seeds: tuple[tuple[float, float], ...]
    exterior_boundary_polylines: tuple[tuple[tuple[float, float], ...], ...]
    exterior_boundary_half_width_m: float


def _point(value, label: str) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2 or not all(isinstance(item, int | float) for item in value):
        raise ValueError(f"{label} must be an [x, y] number pair")
    return float(value[0]), float(value[1])


def _load_navigation_environment(path: Path = DEFAULT_NAVIGATION_ENVIRONMENT) -> NavigationEnvironment:
    """Load semantic map boundaries owned by the selected environment."""
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise ValueError("navigation environment must be an object")
    samples = raw.get("navigable_samples")
    unknown_samples = raw.get("expected_unknown_samples")
    exterior = raw.get("exterior")
    if not isinstance(samples, dict) or not samples:
        raise ValueError("navigable_samples must be a non-empty object")
    if not isinstance(unknown_samples, list) or not unknown_samples:
        raise ValueError("expected_unknown_samples must be a non-empty list")
    if not isinstance(exterior, dict):
        raise ValueError("exterior must be an object")

    flood_seeds = exterior.get("flood_seeds")
    boundary_polylines = exterior.get("boundary_polylines")
    if not isinstance(flood_seeds, list) or not flood_seeds:
        raise ValueError("exterior flood_seeds must be a non-empty list")
    if not isinstance(boundary_polylines, list) or not boundary_polylines:
        raise ValueError("exterior boundary_polylines must be a non-empty list")
    if not all(isinstance(polyline, list) for polyline in boundary_polylines):
        raise ValueError("every exterior boundary polyline must be a list")

    polylines = tuple(
        tuple(_point(point, "exterior boundary point") for point in polyline) for polyline in boundary_polylines
    )
    if any(len(polyline) < 2 for polyline in polylines):
        raise ValueError("every exterior boundary polyline must contain at least two points")
    half_width = exterior.get("boundary_half_width_m")
    if not isinstance(half_width, int | float) or half_width <= 0:
        raise ValueError("exterior boundary_half_width_m must be positive")

    return NavigationEnvironment(
        navigable_samples={
            str(label): _point(point, f"navigable sample {label!r}") for label, point in samples.items()
        },
        expected_unknown_samples=tuple(_point(point, "expected unknown sample") for point in unknown_samples),
        exterior_flood_seeds=tuple(_point(point, "exterior flood seed") for point in flood_seeds),
        exterior_boundary_polylines=polylines,
        exterior_boundary_half_width_m=float(half_width),
    )


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


def _apply_indoor_boundary(grid: np.ndarray, ox: float, oy: float, environment: NavigationEnvironment) -> np.ndarray:
    """Turn environment-authored exterior floor into unknown map space."""
    rows, cols = grid.shape
    xs = ox + (np.arange(cols) + 0.5) * RESOLUTION
    ys = oy + (np.arange(rows) + 0.5) * RESOLUTION
    cell_x, cell_y = np.meshgrid(xs, ys)

    boundary = np.zeros(grid.shape, dtype=bool)
    for polyline in environment.exterior_boundary_polylines:
        for start, end in zip(polyline, polyline[1:], strict=False):
            boundary |= _distance_to_segment(cell_x, cell_y, start, end) <= environment.exterior_boundary_half_width_m

    floodable = (grid == 0) & ~boundary
    exterior = np.zeros(grid.shape, dtype=bool)
    pending: deque[tuple[int, int]] = deque()
    for x, y in environment.exterior_flood_seeds:
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

    for label, (x, y) in {
        "spawn": (world.SPAWN_X, world.SPAWN_Y),
        **environment.navigable_samples,
    }.items():
        cell = _cell_index(grid, ox, oy, x, y)
        if cell is None or exterior[cell] or boundary[cell]:
            raise RuntimeError(f"indoor boundary incorrectly excludes {label} sample ({x:.2f}, {y:.2f})")

    result = grid.copy()
    result[(exterior | boundary) & (result == 0)] = -1
    return result


def _validate_navigation_grid(grid: np.ndarray, ox: float, oy: float, environment: NavigationEnvironment) -> None:
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
    for label, (x, y) in environment.navigable_samples.items():
        if _cell_index(grid, ox, oy, x, y) not in reachable:
            raise RuntimeError(f"{label} sample ({x:.2f}, {y:.2f}) is not reachable from spawn")

    for x, y in environment.expected_unknown_samples:
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
    environment = _load_navigation_environment()
    sim = VirtualMars()
    # Lidar-consistent map (virtual SLAM at the laser's true height): AMCL
    # localizes against what the lidar actually returns, exactly like a real
    # robot localizing against its own SLAM map. occupancy_grid() (collision
    # slab) systematically disagrees with the lidar around furniture and
    # walks AMCL off the map.
    grid, ox, oy = sim.lidar_occupancy_grid(RESOLUTION)
    grid = _apply_indoor_boundary(grid, ox, oy, environment)
    _validate_navigation_grid(grid, ox, oy, environment)
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
