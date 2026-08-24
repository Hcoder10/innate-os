#!/usr/bin/env python3
"""Is this map ambiguous enough to make the localiser jump?

WHY. During live episodes nav2 reported `distance_remaining` stepping between
~4.2m and ~8.8m from one second to the next, with `number_of_recoveries` pinned
at 4 -- so it was one goal, one path, and a robot that cannot physically travel
4.5m in a second. The only thing left that can move that far in a tick is the
POSE ESTIMATE. This checks whether the map itself invites that.

WHAT IT DOES. The grid localizer seeds AMCL by casting rays into the occupancy
grid from candidate poses and scoring each against the live scan. This does the
same thing offline: take the true pose, synthesise the scan it would produce,
then score every pose on a grid. If some pose far from the truth scores about
as well as the truth does, the localiser has no way to tell them apart and will
flip between them -- and that is a property of the MAP, not of the run, so it
can be measured with the stack switched off.

  usage: diag_localise.py <map.yaml> <x> <y> <yaw_deg>
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np

N_RAYS = 72  # 5-degree spacing; enough structure to score a room
MAX_RANGE = 8.0
YAW_STEP_DEG = 10.0
POS_STRIDE_M = 0.20
SAMPLES_PER_M = 20.0  # beam sampling density when marching a ray


def load_map(yaml_path: Path) -> tuple[np.ndarray, float, float, float]:
    """(occupied mask, resolution, origin_x, origin_y) from a nav2 map pair."""
    # A nav2 map yaml is flat scalars and one inline list; parsing it here
    # keeps this diagnostic runnable in the bench venv, which has no PyYAML.
    meta: dict = {}
    for line in yaml_path.read_text().splitlines():
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        key, _, val = line.partition(":")
        val = val.strip()
        if val.startswith("["):
            meta[key.strip()] = [float(v) for v in val.strip("[]").split(",")]
        else:
            try:
                meta[key.strip()] = float(val)
            except ValueError:
                meta[key.strip()] = val
    pgm = yaml_path.parent / meta["image"]
    raw = pgm.read_bytes()

    # P5 header: magic, width, height, maxval -- comments start with '#'.
    fields: list[bytes] = []
    i = 0
    while len(fields) < 4:
        while raw[i : i + 1].isspace():
            i += 1
        if raw[i : i + 1] == b"#":
            while raw[i : i + 1] not in (b"\n", b""):
                i += 1
            continue
        j = i
        while not raw[j : j + 1].isspace():
            j += 1
        fields.append(raw[i:j])
        i = j
    w, h = int(fields[1]), int(fields[2])
    pix = np.frombuffer(raw[i + 1 : i + 1 + w * h], dtype=np.uint8).reshape(h, w)

    # nav2 trinary, negate=0: p_occupied = (255 - value) / 255.
    p = (255.0 - pix.astype(np.float32)) / 255.0
    occupied = p >= meta["occupied_thresh"]
    free = p <= meta["free_thresh"]
    # PGM row 0 is the TOP of the image; map row 0 is the BOTTOM.
    return occupied[::-1], free[::-1], float(meta["resolution"]), *meta["origin"][:2]


def cast(occupied: np.ndarray, res: float, ox: float, oy: float,
         xs: np.ndarray, ys: np.ndarray, yaws: np.ndarray) -> np.ndarray:
    """Range returns for every (pose, ray), marching each beam until it hits.

    Vectorised over poses and rays at once: the sample grid is the same for
    every beam, so the whole thing is one gather into the occupancy array."""
    h, w = occupied.shape
    n_steps = int(MAX_RANGE * SAMPLES_PER_M)
    t = (np.arange(n_steps, dtype=np.float32) + 0.5) / SAMPLES_PER_M  # (S,)
    ray = np.arange(N_RAYS, dtype=np.float32) * (2 * math.pi / N_RAYS)  # (R,)

    ang = yaws[:, None] + ray[None, :]  # (P, R)
    dx, dy = np.cos(ang), np.sin(ang)
    px = xs[:, None, None] + dx[:, :, None] * t[None, None, :]  # (P, R, S)
    py = ys[:, None, None] + dy[:, :, None] * t[None, None, :]

    col = ((px - ox) / res).astype(np.int32)
    row = ((py - oy) / res).astype(np.int32)
    inside = (col >= 0) & (col < w) & (row >= 0) & (row < h)
    hit = np.zeros(px.shape, dtype=bool)
    hit[inside] = occupied[row[inside], col[inside]]
    hit |= ~inside  # leaving the map counts as a return, like a wall

    # First True along the beam; no hit anywhere -> max range.
    idx = np.argmax(hit, axis=2)
    none = ~hit.any(axis=2)
    rng = (idx + 0.5) / SAMPLES_PER_M
    rng[none] = MAX_RANGE
    return rng.astype(np.float32)


def main() -> int:
    if len(sys.argv) < 5:
        print(__doc__.strip().splitlines()[-1].strip())
        return 2
    yaml_path = Path(sys.argv[1])
    tx, ty, tyaw = float(sys.argv[2]), float(sys.argv[3]), math.radians(float(sys.argv[4]))

    occupied, free, res, ox, oy = load_map(yaml_path)
    h, w = occupied.shape
    print(f"map {w}x{h} @ {res}m  origin ({ox:.2f}, {oy:.2f})  "
          f"{free.sum()} free / {occupied.sum()} occupied cells")

    truth = cast(occupied, res, ox, oy,
                 np.array([tx], np.float32), np.array([ty], np.float32),
                 np.array([tyaw], np.float32))[0]
    print(f"true pose ({tx:+.2f}, {ty:+.2f}, {math.degrees(tyaw):.0f}deg): "
          f"scan mean {truth.mean():.2f}m, {int((truth >= MAX_RANGE).sum())}/{N_RAYS} rays unreturned")

    # Candidates: free cells only, on a coarse stride, every YAW_STEP_DEG.
    stride = max(1, int(round(POS_STRIDE_M / res)))
    rows, cols = np.nonzero(free)
    keep = (rows % stride == 0) & (cols % stride == 0)
    cx = ox + (cols[keep] + 0.5) * res
    cy = oy + (rows[keep] + 0.5) * res
    yaw_grid = np.radians(np.arange(0.0, 360.0, YAW_STEP_DEG))
    print(f"scoring {len(cx)} positions x {len(yaw_grid)} headings "
          f"= {len(cx) * len(yaw_grid):,} candidate poses")

    best = []
    for yaw in yaw_grid:  # chunk by heading to keep the arrays a sane size
        rng = cast(occupied, res, ox, oy, cx.astype(np.float32), cy.astype(np.float32),
                   np.full(len(cx), yaw, np.float32))
        err = np.abs(rng - truth[None, :]).mean(axis=1)  # mean |range| error, metres
        for i in np.argsort(err)[:40]:
            best.append((float(err[i]), float(cx[i]), float(cy[i]), math.degrees(yaw)))

    best.sort()
    # `if best` guards the LIST being empty, not the filtered generator, so a
    # pose whose nearest candidate is further than 0.4m away crashed with
    # "min() arg is an empty sequence" -- reachable on the shipped map, whose
    # candidates sit on a 0.2m stride.
    near_truth = [e for e, x, y, _ in best if math.hypot(x - tx, y - ty) < 0.4]
    if not near_truth:
        print(f"\nno candidate within 0.4 m of ({tx:+.2f}, {ty:+.2f}) -- the pose is not on the "
              f"candidate grid (free cells on a {POS_STRIDE_M} m stride), so there is nothing "
              f"to compare against. Pick a pose inside the room.")
        return 2
    truth_err = min(near_truth)

    print(f"\nbest matches (mean range error; the true pose scores {truth_err:.3f}m):")
    shown, far = 0, None
    for err, x, y, yaw in best:
        d = math.hypot(x - tx, y - ty)
        if shown < 12:
            tag = "  <- TRUE" if d < 0.4 else ""
            print(f"  err {err:.3f}m  at ({x:+.2f}, {y:+.2f}, {yaw:3.0f}deg)  {d:.2f}m from truth{tag}")
            shown += 1
        if far is None and d > 2.0:
            far = (err, x, y, yaw, d)

    if far is None:
        print("\nno candidate more than 2m away scored at all -- the map is unambiguous.")
        return 0
    err, x, y, yaw, d = far
    print(f"\nbest DISTANT candidate: err {err:.3f}m at ({x:+.2f}, {y:+.2f}, {yaw:.0f}deg), {d:.2f}m away")
    ratio = err / truth_err if truth_err else float("inf")
    print(f"it is {ratio:.2f}x worse than the truth.")
    if ratio < 1.5:
        print("VERDICT: ambiguous. A pose several metres away explains the scan nearly as\n"
              "well as the real one, so the localiser has no reason to prefer the truth and\n"
              "will flip between them -- which is what the jumping distance_remaining was.")
    else:
        print("VERDICT: not ambiguous. The truth wins clearly, so pose jumps need another\n"
              "explanation -- look at the scan itself rather than the map.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
