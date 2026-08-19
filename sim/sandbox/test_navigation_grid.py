"""Fast invariant checks for the lidar navigation-grid floor mask.

Usage: cd sim && uv run sandbox/test_navigation_grid.py
"""

import _driver_pkg  # noqa: F401
import numpy as np
from mars_sim_driver.core import _navigation_grid


def test_navigation_grid_fusion() -> None:
    # -1 is outside authored floor. A base value of 100 is a thick collision
    # footprint (for example, a tabletop) that must not replace lidar geometry.
    base = np.array(
        [
            [-1, -1, -1, -1],
            [-1, 0, 100, -1],
            [-1, 0, 0, -1],
        ],
        dtype=np.int8,
    )
    seen_free = np.ones_like(base, dtype=bool)
    seen_occ = np.zeros_like(base, dtype=bool)
    seen_occ[0, 0] = True
    seen_occ[2, 2] = True

    fused = _navigation_grid(base, seen_free, seen_occ)
    expected = np.array(
        [
            [-1, -1, -1, -1],
            [-1, 0, 0, -1],
            [-1, 0, 100, -1],
        ],
        dtype=np.int8,
    )
    np.testing.assert_array_equal(fused, expected)

    # Neither a clear beam nor an obstacle endpoint on the infinite ground can
    # turn exterior cells into mapped apartment space.
    assert np.all(fused[:, (0, -1)] == -1)
    assert fused[0, 0] == -1
    # Inside, preserve the old lidar map: a low beam that passes beneath a
    # tabletop stays free instead of inheriting the thick collision footprint.
    assert fused[1, 2] == 0

    try:
        _navigation_grid(base, seen_free[:, :-1], seen_occ)
    except ValueError:
        pass
    else:
        raise AssertionError("mismatched navigation-grid shapes were accepted")


def main() -> None:
    test_navigation_grid_fusion()
    print("PASS: lidar geometry is preserved inside and exterior stays unknown")


if __name__ == "__main__":
    main()
