#!/usr/bin/env python3
"""How much of an exported nav map is floor OUTSIDE the building?

The exporter restricts free space to what is 4-connected-reachable from the
robot's spawn, to stop the planner routing through walls into an exterior apron
it can never reach. That works on a SEALED world. It does not work on a world
with a doorway: a 4-connected fill over raw cells has no notion of a building
envelope, so it walks out through a 0.6m gap and keeps everything beyond it.

This measures the result rather than assuming it. The building envelope is
taken as the bounding box of the largest occupied component -- the outer walls
-- and every free cell outside it is apron.

  usage: diag_apron.py <map.yaml>
"""

from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lint_navmap import read_pgm, read_yaml  # noqa: E402

UNKNOWN_GREY = 205


def largest_occupied_bbox(occupied: np.ndarray) -> tuple[int, int, int, int]:
    """(r0, r1, c0, c1) of the biggest occupied component -- the outer walls."""
    seen = np.zeros(occupied.shape, dtype=bool)
    best: tuple[int, list] = (0, [])
    for r in range(occupied.shape[0]):
        for c in range(occupied.shape[1]):
            if not occupied[r, c] or seen[r, c]:
                continue
            queue, cells = deque([(r, c)]), []
            seen[r, c] = True
            while queue:
                y, x = queue.popleft()
                cells.append((y, x))
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        ny, nx = y + dy, x + dx
                        if (0 <= ny < occupied.shape[0] and 0 <= nx < occupied.shape[1]
                                and occupied[ny, nx] and not seen[ny, nx]):
                            seen[ny, nx] = True
                            queue.append((ny, nx))
            if len(cells) > best[0]:
                best = (len(cells), cells)
    if not best[1]:
        return 0, occupied.shape[0] - 1, 0, occupied.shape[1] - 1
    rows = [r for r, _ in best[1]]
    cols = [c for _, c in best[1]]
    return min(rows), max(rows), min(cols), max(cols)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.strip().splitlines()[-1].strip())
        return 2
    path = Path(sys.argv[1])
    meta = read_yaml(path)
    grey = read_pgm(path.parent / meta["image"])
    res = float(meta["resolution"])
    occ = (255.0 - grey.astype(np.float32)) / 255.0
    occupied = occ > float(meta["occupied_thresh"])
    free = occ < float(meta["free_thresh"])

    r0, r1, c0, c1 = largest_occupied_bbox(occupied)
    inside = np.zeros(free.shape, dtype=bool)
    inside[r0:r1 + 1, c0:c1 + 1] = True

    cell_m2 = res * res
    total = free.sum() * cell_m2
    within = (free & inside).sum() * cell_m2
    apron = (free & ~inside).sum() * cell_m2
    share = 100.0 * apron / total if total else 0.0

    print(f"{path.name}: {grey.shape[1]}x{grey.shape[0]} @ {res}m")
    print(f"  wall bbox      rows {r0}-{r1}, cols {c0}-{c1} "
          f"({(c1 - c0 + 1) * res:.2f} x {(r1 - r0 + 1) * res:.2f} m)")
    print(f"  free floor     {total:7.1f} m2")
    print(f"    inside walls {within:7.1f} m2")
    print(f"    OUTSIDE      {apron:7.1f} m2   ({share:.0f}% of the exported floor)")
    print(f"  unknown cells  {(grey == UNKNOWN_GREY).sum():,}")
    if share > 5.0:
        print(f"\nFAIL: {share:.0f}% of the drivable map is outside the building. The planner "
              f"can route\nthrough a doorway and around the outside, which is the route "
              f"oscillation this\nwas supposed to remove.")
        return 1
    print("\nOK: the drivable map is the building interior.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
