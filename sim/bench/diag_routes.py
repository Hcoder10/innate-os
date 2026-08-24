#!/usr/bin/env python3
"""Why does nav2 keep changing its mind about the route?

THE OBSERVATION. In live episodes `distance_remaining` stepped between roughly
4.3m and 8.8m from one second to the next, for about eleven seconds a side,
while `number_of_recoveries` stayed pinned at 4 -- one goal, one action, no new
plan requests from the brain. A robot that tops out well under a metre a second
cannot move 4.5m in a tick, so the thing changing is the PATH, not the pose.

THE CANDIDATE. Both the global and local costmaps inflate obstacles by
`inflation_radius` (0.3m global) with `cost_scaling_factor` 10. The robot's
footprint is only 0.165m half-width, so a gap between 0.33m and 0.6m wide is
one the robot physically fits through but the costmap prices as expensive. When
the short route runs through such a gap and a long way round exists, the two
routes can sit close enough in cost that live scan updates -- which nudge the
obstacle layer every 0.2s -- flip the winner back and forth. That is a property
of the MAP plus the config, so it can be measured with the stack switched off.

This measures, for a start and a goal:
  * the shortest route the robot physically fits through, and its bottleneck
  * the shortest route that also respects the inflation radius
If those two differ by metres, the planner has two candidates to oscillate
between and the gap is the reason.

  usage: diag_routes.py <map.yaml> <x0> <y0> [<x1> <y1>]
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lint_navmap import bfs, read_pgm, read_yaml  # noqa: E402

HALF_WIDTH_M = 0.165  # footprint y half-extent: the hard "does it fit" limit
INFLATION_M = 0.30  # global costmap inflation_radius


def clearance(occupied: np.ndarray, res: float) -> np.ndarray:
    """Metres to the nearest occupied cell, by two-pass chamfer.

    Chamfer rather than an exact Euclidean transform: the error is under a
    couple of percent at these distances and it needs no scipy, which the bench
    venv does not have."""
    big = 1e6
    d = np.where(occupied, 0.0, big).astype(np.float64)
    h, w = d.shape
    a, b = 1.0, math.sqrt(2.0)
    for r in range(h):  # forward pass
        for c in range(w):
            if d[r, c] == 0.0:
                continue
            best = d[r, c]
            if r > 0:
                best = min(best, d[r - 1, c] + a)
                if c > 0:
                    best = min(best, d[r - 1, c - 1] + b)
                if c + 1 < w:
                    best = min(best, d[r - 1, c + 1] + b)
            if c > 0:
                best = min(best, d[r, c - 1] + a)
            d[r, c] = best
    for r in range(h - 1, -1, -1):  # backward pass
        for c in range(w - 1, -1, -1):
            best = d[r, c]
            if r + 1 < h:
                best = min(best, d[r + 1, c] + a)
                if c > 0:
                    best = min(best, d[r + 1, c - 1] + b)
                if c + 1 < w:
                    best = min(best, d[r + 1, c + 1] + b)
            if c + 1 < w:
                best = min(best, d[r, c + 1] + a)
            d[r, c] = best
    return d * res


def route(passable: np.ndarray, start: tuple[int, int], goal: tuple[int, int],
          res: float) -> tuple[float, list[tuple[int, int]]]:
    """(length in metres, cells) of the shortest 8-connected route, or (inf, [])."""
    dist = bfs(passable, start)
    if not np.isfinite(dist[goal]):
        return float("inf"), []
    # Walk the gradient back from the goal.
    path = [goal]
    cur = goal
    steps = [(-1, 0, 1.0), (1, 0, 1.0), (0, -1, 1.0), (0, 1, 1.0),
             (-1, -1, 1.4142), (-1, 1, 1.4142), (1, -1, 1.4142), (1, 1, 1.4142)]
    while cur != start:
        r, c = cur
        best, pick = dist[cur], None
        for dr, dc, cost in steps:
            nr, nc = r + dr, c + dc
            if 0 <= nr < dist.shape[0] and 0 <= nc < dist.shape[1] and dist[nr, nc] + cost <= best + 1e-6:
                best, pick = dist[nr, nc], (nr, nc)
        if pick is None:
            break
        cur = pick
        path.append(cur)
    return float(dist[goal]) * res, path[::-1]


def main() -> int:
    if len(sys.argv) < 4:
        print(__doc__.strip().splitlines()[-1].strip())
        return 2
    yaml_path = Path(sys.argv[1])
    meta = read_yaml(yaml_path)
    grey = read_pgm(yaml_path.parent / meta["image"])
    res = float(meta["resolution"])
    ox, oy = meta["origin"][0], meta["origin"][1]

    occ = (255.0 - grey.astype(np.float32)) / 255.0
    lethal = occ > float(meta["occupied_thresh"])
    drivable = occ < float(meta["free_thresh"])  # as nav2 reads it today

    clear = clearance(lethal, res)
    fits = drivable & (clear >= HALF_WIDTH_M)
    comfortable = drivable & (clear >= INFLATION_M)

    def cell(x: float, y: float) -> tuple[int, int]:
        return int((y - oy) / res), int((x - ox) / res)

    sx, sy = float(sys.argv[2]), float(sys.argv[3])
    start = cell(sx, sy)

    if len(sys.argv) >= 6:
        goals = [(float(sys.argv[4]), float(sys.argv[5]))]
    else:
        # No goal given: find the interior cell whose two route costs disagree
        # most -- the worst case the planner could be handed.
        d_fit = bfs(fits, start)
        d_com = bfs(comfortable, start)
        spread = np.where(np.isfinite(d_fit) & np.isfinite(d_com), d_com - d_fit, -np.inf)
        worst = np.unravel_index(np.argmax(spread), spread.shape)
        goals = [(ox + (worst[1] + 0.5) * res, oy + (worst[0] + 0.5) * res)]
        print("no goal given; worst-disagreement cell picked automatically")

    print(f"{yaml_path.name}: footprint half-width {HALF_WIDTH_M}m, inflation {INFLATION_M}m")
    print(f"start ({sx:+.2f}, {sy:+.2f})  clearance there {clear[start]:.2f}m")

    for gx, gy in goals:
        goal = cell(gx, gy)
        len_fit, path_fit = route(fits, start, goal, res)
        len_com, _ = route(comfortable, start, goal, res)
        print(f"\ngoal ({gx:+.2f}, {gy:+.2f}):")
        if not math.isfinite(len_fit):
            print("  unreachable even at the physical footprint limit")
            continue
        bottleneck = min(clear[r, c] for r, c in path_fit)
        tight = [(r, c) for r, c in path_fit if clear[r, c] < INFLATION_M]
        print(f"  squeeze-through route : {len_fit:5.2f}m, bottleneck {bottleneck:.2f}m clearance"
              f" ({len(tight)} of {len(path_fit)} cells inside the inflation radius)")
        if math.isfinite(len_com):
            print(f"  inflation-clean route : {len_com:5.2f}m")
            print(f"  the two differ by {len_com - len_fit:.2f}m")
        else:
            print("  inflation-clean route : none exists")

        # The verdict must distinguish the two ways the interesting case can
        # fail to hold. An earlier version printed "only one route exists"
        # while its own output showed two finite routes, because the `else`
        # caught both "no second route" and "the spread is below an arbitrary
        # 1.0 m" -- and this is the sentence that redirects the investigation.
        if not math.isfinite(len_com):
            print("\n  VERDICT: only one route exists -- there is no inflation-clean way to this\n"
                  "  goal at all, so the planner has nothing to oscillate between. Another cause.")
        elif bottleneck >= INFLATION_M:
            print("\n  VERDICT: the short route is comfortably clear of the inflation radius, so\n"
                  "  live scan updates cannot re-price it into second place. Another cause.")
        elif len_com - len_fit <= 1.0:
            print(f"\n  VERDICT: two routes exist and the short one squeezes ({bottleneck:.2f} m), but\n"
                  f"  they differ by only {len_com - len_fit:.2f} m. That is a weaker version of the\n"
                  f"  same mechanism -- a flip costs little here, so it explains a wobble, not the\n"
                  f"  4.5 m jumps that were observed.")
        else:
            print("\n  VERDICT: two viable routes whose costs are set by a gap the robot fits\n"
                  "  through but the costmap penalises. Live scan updates re-price that gap\n"
                  "  every 0.2s, so the global planner can prefer either one from replan to\n"
                  "  replan -- which is the jumping distance_remaining, and why the robot\n"
                  "  burns its budget without making progress.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
