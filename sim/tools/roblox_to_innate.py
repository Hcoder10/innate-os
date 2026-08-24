#!/usr/bin/env python3
"""Turn a Roblox map export into innate-os artifacts.

Writes three things:

  sim/rooms/<NN>_<map>.py            static shell + furniture (statics.Room)
  sim/props/<NN>_<map>_<prop>.py     one droppable Prop per movable object
  sim/assets/objects/roblox/*.obj    each prop's visual mesh

WHY PROPS GET A MESH.  A Prop is one free body with ONE collision primitive, so
a multi-part object cannot map onto it directly. But `mesh` is optional and
independent of `collision`, so the supported shape is: the full multi-primitive
silhouette as the visual mesh, one primitive underneath as the collider. That
matters because pick_any_object.py asks Gemini to find an object by natural-
language description -- a bare cylinder is not a mug, and "find the red mug"
degenerates into colour-matching if the handle is missing.

The mesh carries no texture, so it renders in a single rgba. Shape survives,
per-part colour does not; the handle is the part that makes a mug a mug.

Contact parameters are left at the dataclass defaults and are NOT tuned. See
the comments on sim/props/11_can.py for what real tuning looks like -- those
numbers came from watching the gripper fail.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
from pathlib import Path

from roblox_to_mjcf import _rgba, part_to_geom, self_check

# --- primitive tessellation (visual meshes only; MuJoCo needs no convexity
# --- for a contype=0 geom, so these can be as detailed as they like) ---

SEGMENTS = 24  # around a cylinder
RINGS = 10  # latitude bands on a sphere


def _quat_to_mat(q) -> tuple[tuple[float, ...], ...]:
    w, x, y, z = q
    return (
        (1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)),
        (2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)),
        (2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)),
    )


def _box(hx: float, hy: float, hz: float):
    v = [
        (sx * hx, sy * hy, sz * hz)
        for sx in (-1, 1)
        for sy in (-1, 1)
        for sz in (-1, 1)
    ]
    # Indices into the (x,y,z) sign-product order above.
    f = [
        (0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5),
        (0, 4, 5), (0, 5, 1), (2, 3, 7), (2, 7, 6),
        (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3),
    ]
    return v, f


def _cylinder(r: float, hh: float):
    v, f = [], []
    for i in range(SEGMENTS):
        a = 2 * math.pi * i / SEGMENTS
        v.append((r * math.cos(a), r * math.sin(a), -hh))
        v.append((r * math.cos(a), r * math.sin(a), hh))
    for i in range(SEGMENTS):
        b0, b1 = 2 * i, 2 * ((i + 1) % SEGMENTS)
        f += [(b0, b1, b1 + 1), (b0, b1 + 1, b0 + 1)]
    bot, top = len(v), len(v) + 1
    v += [(0.0, 0.0, -hh), (0.0, 0.0, hh)]
    for i in range(SEGMENTS):
        b0, b1 = 2 * i, 2 * ((i + 1) % SEGMENTS)
        f += [(bot, b1, b0), (top, b0 + 1, b1 + 1)]
    return v, f


def _sphere(r: float):
    v, f = [], []
    for i in range(RINGS + 1):
        theta = math.pi * i / RINGS
        for j in range(SEGMENTS):
            phi = 2 * math.pi * j / SEGMENTS
            v.append(
                (r * math.sin(theta) * math.cos(phi), r * math.sin(theta) * math.sin(phi), r * math.cos(theta))
            )
    for i in range(RINGS):
        for j in range(SEGMENTS):
            a = i * SEGMENTS + j
            b = i * SEGMENTS + (j + 1) % SEGMENTS
            c = a + SEGMENTS
            d = b + SEGMENTS
            f += [(a, b, d), (a, d, c)]
    return v, f


def tessellate(g: dict):
    """A geom dict (from part_to_geom) -> (verts, faces) in WORLD coordinates."""
    if g["type"] == "box":
        v, f = _box(*g["size"])
    elif g["type"] == "cylinder":
        v, f = _cylinder(g["size"][0], g["size"][1])
    else:
        v, f = _sphere(g["size"][0])
    m, p = _quat_to_mat(g["quat"]), g["pos"]
    world = [
        tuple(sum(m[i][k] * vert[k] for k in range(3)) + p[i] for i in range(3))
        for vert in v
    ]
    return world, f


def write_obj(path: Path, meshes: list[tuple[list, list]]) -> int:
    """Concatenate tessellated parts into one OBJ. Faces are 1-based and
    offset per sub-mesh, which is the classic place this goes wrong."""
    lines, offset = [], 0
    for verts, faces in meshes:
        for x, y, z in verts:
            lines.append(f"v {x:.6f} {y:.6f} {z:.6f}")
        for a, b, c in faces:
            lines.append(f"f {a + 1 + offset} {b + 1 + offset} {c + 1 + offset}")
        offset += len(verts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return offset


# --- conversion ---

LABELS = {"mug": "☕", "can": "\U0001f96b", "bottle": "\U0001f376", "book": "\U0001f4d5", "ball": "\U0001f3d0"}


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def _volume(g: dict) -> float:
    if g["type"] == "box":
        return 8 * g["size"][0] * g["size"][1] * g["size"][2]
    if g["type"] == "cylinder":
        return math.pi * g["size"][0] ** 2 * 2 * g["size"][1]
    return 4 / 3 * math.pi * g["size"][0] ** 3


def _z_extent(g: dict) -> float:
    """Half-extent along WORLD z, exactly for a rotated box and tightly for the
    others. Using max(size) instead over-estimates a flat rotated slab badly,
    which shows up as a prop that rests visibly above its own support."""
    if g["type"] == "sphere":
        return g["size"][0]
    m = _quat_to_mat(g["quat"])
    if g["type"] == "box":
        return sum(abs(m[2][i]) * g["size"][i] for i in range(3))
    r, hh = g["size"][0], g["size"][1]  # cylinder, axis along local z
    return abs(m[2][2]) * hh + math.hypot(m[2][0], m[2][1]) * r


def _lowest_z(g: dict) -> float:
    return g["pos"][2] - _z_extent(g)


def convert_props(scene: dict, obj_dir: Path, prop_dir: Path, prefix: str, start_index: int) -> list[str]:
    s = scene["studs_per_metre"]
    prop_dir.mkdir(parents=True, exist_ok=True)  # write_obj makes obj_dir, nothing makes this one
    models: dict[str, list[dict]] = {}
    for part in scene["parts"]:
        if part["e"] == "prop" and part["m"]:
            models.setdefault(part["m"], []).append(part)

    written = []
    for n, (model, parts) in enumerate(sorted(models.items())):
        geoms = [part_to_geom(p, s) for p in parts]
        dominant = max(zip(geoms, parts), key=lambda gp: _volume(gp[0]))
        dg, dp = dominant
        name = f"{prefix}_{_slug(model)}"

        # The collision primitive sits at the body origin with no offset (see
        # Prop._primitive_geom), so the origin MUST be that primitive's centre.
        origin = dg["pos"]
        # Bake the built-in yaw out so the prop is canonical; the sim applies
        # its own yaw when it places one.
        m = _quat_to_mat(dg["quat"])
        yaw = math.atan2(m[1][0], m[0][0])
        cos, sin = math.cos(-yaw), math.sin(-yaw)

        meshes = []
        for g in geoms:
            verts, faces = tessellate(g)
            local = []
            for vx, vy, vz in verts:
                dx, dy, dz = vx - origin[0], vy - origin[1], vz - origin[2]
                local.append((dx * cos - dy * sin, dx * sin + dy * cos, dz))
            meshes.append((local, faces))
        obj_path = obj_dir / f"{name}.obj"
        nverts = write_obj(obj_path, meshes)

        floor = min(_lowest_z(g) for g in geoms)
        rest_z = origin[2] - floor
        shape = dg["type"]
        size = tuple(round(v, 5) for v in dg["size"])
        rgba = tuple(round(v, 4) for v in _rgba(dp))
        label = next((v for k, v in LABELS.items() if k in model.lower()), "?")

        sidecar = f'''"""{model} from the {scene["map"]} map (generated by sim/tools/roblox_to_innate.py).

Visual mesh is the full multi-primitive shape; the collider is its {shape}
body. Contact parameters are DEFAULTS and have not been tuned against the
gripper -- see 11_can.py for what tuned numbers look like.

drop_z is the height this object was AUTHORED at plus 1 cm, not the default
rest_z. Drop carries no z, so a prop meant to sit on a plinth would otherwise
be released below it and get shoved onto the floor; releasing from just above
its own support also avoids the bounce a long fall onto hard ground causes.
"""

from mars_sim_driver.props import Prop

PROP = Prop(
    name="{name}",
    label="{label}",
    title="{model.replace("_", " ")}",
    mesh="../objects/{name}.obj",
    collision="{shape}",
    size={size},
    rgba={rgba},
    rest_z={round(rest_z, 5)},
    drop_z={round(origin[2] + 0.01, 5)},
)
'''
        out = prop_dir / f"{start_index + n:02d}_{name}.py"
        out.write_text(sidecar)
        written.append(f"{out.name} ({len(parts)} parts, {nverts} verts, {shape}{size})")
    return written


def convert_room(scene: dict, room_path: Path, spawn: tuple[float, float, float]) -> tuple[int, int]:
    s = scene["studs_per_metre"]
    lines, solid, ghost = [], 0, 0
    for part in scene["parts"]:
        if part["e"] == "prop" and part["m"]:
            continue  # movable: becomes a Prop instead
        g = part_to_geom(part, s)
        # CanCollide=false is authority, not a hint. Floor paint lives in the
        # scene rather than in Decor (it belongs to the thing it marks), and
        # add_planar_base gives the robot no z at all -- so a 12 mm painted
        # outline left collidable is a wall the planner routes around, and the
        # symptom is "no path" to a spot standing on open floor.
        collide = part["e"] != "decor" and part.get("k", True)
        solid, ghost = (solid + 1, ghost) if collide else (solid, ghost + 1)
        quat = "" if g["type"] == "sphere" else f", quat={tuple(round(v, 6) for v in g['quat'])}"
        extra = "" if collide else ", collide=False"
        lines.append(
            f'    Geom("{g["type"]}", {tuple(round(v, 5) for v in g["size"])}, '
            f'{tuple(round(v, 5) for v in g["pos"])}{quat}, '
            f'rgba={tuple(round(v, 4) for v in _rgba(part))}, name="{_slug(part["n"])}"{extra}),'
        )

    body = "\n".join(lines)
    room_path.parent.mkdir(parents=True, exist_ok=True)
    room_path.write_text(
        f'''"""{scene["map"]} benchmark map (generated by sim/tools/roblox_to_innate.py).

Authored in Roblox Studio and exported as primitives -- see that tool's header
for the axis conversion. {solid} collidable geoms plus {ghost} decor geoms
(floor seams and skirting) that are drawn but never collided with.

Do not hand-edit: rebuild the map and re-run the exporter.
"""

from mars_sim_driver.statics import Geom, Room

ROOM = Room(
    name="{_slug(scene["map"])}",
    title="{scene["map"]}",
    spawn={spawn},
    geoms=[
{body}
    ],
)
'''
    )
    return solid, ghost


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scene", type=Path)
    ap.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    ap.add_argument("--index", type=int, default=60, help="numeric prefix for generated prop sidecars")
    ap.add_argument("--yaw", type=float, default=90.0, help="spawn heading, degrees")
    ap.add_argument("--spawn", type=float, nargs=3, default=None, metavar=("X", "Y", "YAW"))
    args = ap.parse_args()

    self_check()
    scene = json.loads(args.scene.read_text())
    prefix = _slug(scene["map"])

    # One ASSET BUNDLE per map, not a shared sim/rooms. RoomRegistry loads every
    # sidecar under its roots, so N maps in one directory means N maps in one
    # world, all overlapping at the origin. VIRTUAL_MARS_ASSETS already selects
    # a bundle, so pointing it at sim/bundles/<map> selects the map -- no extra
    # switch to invent, and the apartment simply isn't in the bundle.
    bundle = args.repo / "sim" / "bundles" / prefix
    # Wipe only what this tool generates. Leaving stale sidecars behind is
    # silent: a prop whose file was renamed between runs still loads under its
    # old name, and two files defining the same prop name collapse to one
    # registry entry, so the counts stop meaning anything.
    #
    # challenges/ is deliberately NOT in this list -- those are hand-authored
    # and live in the bundle so a pack ships with the scenarios it needs.
    for generated in ("rooms", "props", "objects"):
        shutil.rmtree(bundle / generated, ignore_errors=True)

    # The spawn comes from the map's own StartPad, not a CLI guess. Defaulting
    # to (0,0) put the Household spawn exactly where two interior walls cross,
    # and physics ejected the robot on the first step.
    if args.spawn is not None:
        spawn = tuple(args.spawn)
    else:
        pad = next((p for p in scene["parts"] if p["n"] == "StartPad"), None)
        if pad is None:
            raise SystemExit(f"{scene['map']}: no StartPad part -- add one or pass --spawn")
        g = part_to_geom(pad, scene["studs_per_metre"])
        spawn = (round(g["pos"][0], 3), round(g["pos"][1], 3), args.yaw)
    print(f"spawn -> {spawn}")

    room_path = bundle / "rooms" / f"10_{prefix}.py"
    solid, ghost = convert_room(scene, room_path, spawn)
    print(f"room  -> {room_path.relative_to(args.repo)}  ({solid} solid + {ghost} decor geoms)")

    props = convert_props(
        scene,
        obj_dir=bundle / "objects",
        prop_dir=bundle / "props",
        prefix=prefix,
        start_index=args.index,
    )
    for line in props:
        print(f"prop  -> {line}")
    print(f"{len(props)} props -> {bundle.relative_to(args.repo)}/props")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
