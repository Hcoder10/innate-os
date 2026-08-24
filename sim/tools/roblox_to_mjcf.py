#!/usr/bin/env python3
"""Convert a Roblox map export (see roblox-studio-mcp/export_scene.mjs) to MJCF.

Roblox primitives map 1:1 onto MuJoCo geoms, so this skips meshing and convex
decomposition entirely -- a box stays a box. That is both lossless and far
cheaper than the apartment's 1200+ hulls.

AXES.  Roblox is right-handed, Y-up, -Z forward. MuJoCo is right-handed, Z-up.
The change of basis is

    P(x, y, z) = (x, -z, y)          det(P) = +1, so handedness is preserved

applied to positions, and to rotations as R_mj = P @ R_rbx @ P.T. Getting this
wrong is the failure mode that looks fine in a top-down render and is mirrored
in every side view, so there is a self-check at the bottom of this file.

SHAPES.  A Roblox cylinder's axis is its local X; MuJoCo's is local Z. Rather
than composing conventions, this permutes the local axes (x,y,z) -> (y,z,x),
which is cyclic and therefore still right-handed.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

# Change of basis, Roblox -> MuJoCo.
P = ((1.0, 0.0, 0.0), (0.0, 0.0, -1.0), (0.0, 1.0, 0.0))


def _mat_mul(a, b):
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3))


def _transpose(m):
    return tuple(tuple(m[j][i] for j in range(3)) for i in range(3))


def to_mj_vec(v: list[float]) -> tuple[float, float, float]:
    """Roblox (x, y, z) -> MuJoCo (x, -z, y)."""
    return (v[0], -v[2], v[1])


def to_mj_rot(r9: list[float]) -> tuple[tuple[float, ...], ...]:
    """Roblox row-major CFrame rotation -> MuJoCo rotation matrix."""
    r = ((r9[0], r9[1], r9[2]), (r9[3], r9[4], r9[5]), (r9[6], r9[7], r9[8]))
    return _mat_mul(_mat_mul(P, r), _transpose(P))


def mat_to_quat(m) -> tuple[float, float, float, float]:
    """Rotation matrix -> (w, x, y, z). Shepperd's method: pick the branch with
    the largest divisor so the square root never loses precision near a
    half-turn, where the naive trace formula degenerates."""
    t = m[0][0] + m[1][1] + m[2][2]
    if t > 0.0:
        s = math.sqrt(t + 1.0) * 2.0
        return (0.25 * s, (m[2][1] - m[1][2]) / s, (m[0][2] - m[2][0]) / s, (m[1][0] - m[0][1]) / s)
    if m[0][0] > m[1][1] and m[0][0] > m[2][2]:
        s = math.sqrt(1.0 + m[0][0] - m[1][1] - m[2][2]) * 2.0
        return ((m[2][1] - m[1][2]) / s, 0.25 * s, (m[0][1] + m[1][0]) / s, (m[0][2] + m[2][0]) / s)
    if m[1][1] > m[2][2]:
        s = math.sqrt(1.0 + m[1][1] - m[0][0] - m[2][2]) * 2.0
        return ((m[0][2] - m[2][0]) / s, (m[0][1] + m[1][0]) / s, 0.25 * s, (m[1][2] + m[2][1]) / s)
    s = math.sqrt(1.0 + m[2][2] - m[0][0] - m[1][1]) * 2.0
    return ((m[1][0] - m[0][1]) / s, (m[0][2] + m[2][0]) / s, (m[1][2] + m[2][1]) / s, 0.25 * s)


def part_to_geom(part: dict, studs_per_metre: float) -> dict:
    """One Roblox part -> {type, size, pos, quat, rgba} in MuJoCo metres.

    MuJoCo sizes are HALF-extents; Roblox Size is the full extent."""
    s = studs_per_metre
    sx, sy, sz = (v / s for v in part["s"])
    pos = to_mj_vec([v / s for v in part["p"]])
    rot = to_mj_rot(part["r"])
    shape = part["h"]

    if shape == "Ball":
        # A sphere is rotation-invariant, so drop the quaternion entirely.
        return dict(type="sphere", size=(sx / 2,), pos=pos, quat=(1.0, 0.0, 0.0, 0.0), rgba=_rgba(part))

    # R_mj = P @ R_rbx @ P.T rotates the FRAME, but it also permutes which local
    # axis is which: P.T @ e2 = -e3 and P.T @ e3 = e2, so MuJoCo local (x,y,z)
    # corresponds to Roblox local (X, -Z, Y). The half-extents have to follow
    # that permutation or a 9x2.4x0.15 wall compiles as 2.4 m THICK and 0.15 m
    # tall -- geometry that still renders, just as the wrong building.
    col = _transpose(rot)  # col[i] is column i of rot, i.e. a local axis

    if shape == "Cylinder":
        # Roblox cylinders run along local X; MuJoCo's along local Z. Rebuild
        # the frame as [P a2 | P a3 | P a1] = [col2 | -col1 | col0] so local z
        # lands on the Roblox X axis, det = +1 preserved.
        axis_z = _transpose((col[2], tuple(-v for v in col[1]), col[0]))
        # size is (radius, half-height); the cylinder helper builds (h, dia, dia).
        return dict(type="cylinder", size=(sy / 2, sx / 2), pos=pos, quat=mat_to_quat(axis_z), rgba=_rgba(part))

    return dict(type="box", size=(sx / 2, sz / 2, sy / 2), pos=pos, quat=mat_to_quat(rot), rgba=_rgba(part))


def _rgba(part: dict) -> tuple[float, float, float, float]:
    r, g, b = part["c"]
    return (r / 255.0, g / 255.0, b / 255.0, 1.0)


def _fmt(vals) -> str:
    return " ".join(f"{v:.6g}" for v in vals)


def geom_xml(name: str, g: dict, group: int, physical: bool = True) -> str:
    quat = "" if g["type"] == "sphere" else f' quat="{_fmt(g["quat"])}"'
    contact = "" if physical else ' contype="0" conaffinity="0"'
    return (
        f'    <geom name="{name}" type="{g["type"]}" size="{_fmt(g["size"])}"'
        f' pos="{_fmt(g["pos"])}"{quat} rgba="{_fmt(g["rgba"])}" group="{group}"{contact}'
        f' friction="0.9 0.01 0.001" condim="4"/>'
    )


def convert(scene: dict, include_decor: bool = True) -> tuple[list[str], list[str], list[str]]:
    """-> (static_geoms, prop_geoms, decor_geoms) as MJCF lines."""
    s = scene["studs_per_metre"]
    static, props, decor = [], [], []
    seen: dict[str, int] = {}
    for part in scene["parts"]:
        # Names repeat across prop models (every mug has a "body"), and MuJoCo
        # requires unique geom names, so qualify and de-duplicate.
        base = f'{part["m"] + "_" if part["m"] else ""}{part["n"]}'
        seen[base] = seen.get(base, 0) + 1
        name = base if seen[base] == 1 else f"{base}_{seen[base]}"
        g = part_to_geom(part, s)
        if part["e"] == "decor":
            if include_decor:
                decor.append(geom_xml(name, g, group=1, physical=False))
        elif part["e"] == "prop":
            props.append(geom_xml(name, g, group=1))
        else:
            static.append(geom_xml(name, g, group=0))
    return static, props, decor


PREVIEW = """<mujoco model="{name}">
  <option timestep="0.002" gravity="0 0 -9.81" integrator="implicitfast"/>
  <visual><global offwidth="1600" offheight="1000"/></visual>
  <worldbody>
    <light pos="3 -3 5" dir="-3 3 -5" diffuse="1 1 1"/>
    <light pos="-3 3 4" dir="3 -3 -4" diffuse="0.6 0.7 0.9"/>
    <geom name="ground" type="plane" size="20 20 0.1" rgba="0.35 0.35 0.35 1"/>
{body}
  </worldbody>
</mujoco>
"""


def self_check() -> None:
    """Guard the two things that fail silently: handedness and the cylinder axis."""
    # det(P) must be +1. A reflection would mirror the map -- invisible from
    # directly overhead, obvious and wrong from every other angle.
    det = sum(
        P[0][i] * (P[1][(i + 1) % 3] * P[2][(i + 2) % 3] - P[1][(i + 2) % 3] * P[2][(i + 1) % 3]) for i in range(3)
    )
    assert abs(det - 1.0) < 1e-9, f"P is not a proper rotation (det={det})"

    # Roblox up (+Y) must become MuJoCo up (+Z).
    assert to_mj_vec([0, 1, 0]) == (0, 0, 1), "Roblox +Y must map to MuJoCo +Z"

    ident = [1, 0, 0, 0, 1, 0, 0, 0, 1]

    # An unrotated Roblox cylinder (axis = world +X) must come out with its
    # MuJoCo local z along world +X.
    g = part_to_geom({"h": "Cylinder", "s": [1, 1, 1], "p": [0, 0, 0], "r": ident, "c": [0, 0, 0]}, 1.0)
    w, x, y, z = g["quat"]
    axis = (2 * (x * z + w * y), 2 * (y * z - w * x), 1 - 2 * (x * x + y * y))  # R @ (0,0,1)
    assert max(abs(a - b) for a, b in zip(axis, (1, 0, 0))) < 1e-9, f"cylinder axis wrong: {axis}"

    # A wall: 9 m long, 2.4 m TALL, 0.15 m thick in Roblox (x, y, z) must become
    # half-extents (4.5, 0.075, 1.2) in MuJoCo. The rotation alone looks correct
    # while this is wrong, so assert the sizes explicitly -- an earlier version
    # passed every other check here and still built 0.15 m tall walls.
    g = part_to_geom({"h": "Block", "s": [9, 2.4, 0.15], "p": [0, 0, 0], "r": ident, "c": [0, 0, 0]}, 1.0)
    assert max(abs(a - b) for a, b in zip(g["size"], (4.5, 0.075, 1.2))) < 1e-9, f"box size wrong: {g['size']}"

    # And the floor: 9 x 0.1 x 9 -> (4.5, 4.5, 0.05), a slab not a fin.
    g = part_to_geom({"h": "Block", "s": [9, 0.1, 9], "p": [0, 0, 0], "r": ident, "c": [0, 0, 0]}, 1.0)
    assert max(abs(a - b) for a, b in zip(g["size"], (4.5, 4.5, 0.05))) < 1e-9, f"floor size wrong: {g['size']}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scene", type=Path)
    ap.add_argument("--out", type=Path, default=Path("/tmp/roblox_preview.xml"))
    ap.add_argument("--render", type=Path, default=None)
    args = ap.parse_args()

    self_check()
    scene = json.loads(args.scene.read_text())
    static, props, decor = convert(scene)
    body = "\n".join(static + props + decor)
    xml = PREVIEW.format(name=scene["map"], body=body)
    args.out.write_text(xml)
    print(f"{scene['map']}: {len(static)} static + {len(props)} prop + {len(decor)} decor geoms -> {args.out}")

    import mujoco  # deferred so the converter is importable without a GL stack

    model = mujoco.MjModel.from_xml_string(xml)
    data = mujoco.MjData(model)
    mujoco.mj_forward(model, data)
    print(f"compiled OK: {model.ngeom} geoms, {model.nbody} bodies")

    lo = [min(float(data.geom_xpos[i][k]) for i in range(model.ngeom)) for k in range(3)]
    hi = [max(float(data.geom_xpos[i][k]) for i in range(model.ngeom)) for k in range(3)]
    print("geom centre bounds (m): " + ", ".join(f"{a:+.2f}..{b:+.2f}" for a, b in zip(lo, hi)))

    if args.render:
        import PIL.Image

        renderer = mujoco.Renderer(model, height=1000, width=1600)
        # The room has four walls and no ceiling, so a shallow elevation just
        # renders wall exteriors. Look down INTO it.
        views = {
            "iso": dict(lookat=[0, 0, 0.2], distance=12.0, azimuth=125.0, elevation=-58.0),
            "overhead": dict(lookat=[0, 0, 0.0], distance=11.0, azimuth=90.0, elevation=-89.0),
            "ladder": dict(lookat=[0, 3.2, 0.25], distance=3.4, azimuth=-90.0, elevation=-18.0),
        }
        for tag, v in views.items():
            cam = mujoco.MjvCamera()
            cam.lookat[:] = v["lookat"]
            cam.distance, cam.azimuth, cam.elevation = v["distance"], v["azimuth"], v["elevation"]
            renderer.update_scene(data, cam)
            out = args.render.with_name(f"{args.render.stem}_{tag}{args.render.suffix}")
            PIL.Image.fromarray(renderer.render()).save(out)
            print(f"rendered -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
