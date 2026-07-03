"""Convex-decompose each per-room OBJ (from split_apartment_obj.py) for use as
MuJoCo collision geometry, with two improvements over the plain
`uvx obj2mjcf --decompose` pipeline used earlier:

1. Floor faces are stripped from each room mesh before decomposition. CoACD
   has no notion of "this flat expanse should stay one rigid plane" -- when
   the floor is topologically fused with furniture legs/counters/walls, it
   gets carved into several convex slabs near every leg and corner, and the
   seams between those slabs are exactly the kind of "corner catches a hull
   edge" contact that destabilizes the sim. The floor is genuinely flat in
   this apartment (all rooms bottom out at y ~= 0 in local/OBJ space), so we
   remove it here and rely on one explicit MJCF <geom type="plane"> instead
   -- zero seams, by construction.
2. Uses CoACD's real_metric mode (added 2026-04) so the concavity threshold
   is specified directly in meters rather than normalized to each room's own
   (very different) bounding-box diagonal -- obj2mjcf's CLI wrapper doesn't
   expose this yet, so we call the coacd library directly.

Usage: uv run decompose_rooms.py [room_name ...]
    (no args = every room found in work/apartment_split/*.obj)
"""

import sys
from pathlib import Path

import coacd
import numpy as np
import trimesh

SPLIT_DIR = Path(__file__).resolve().parent.parent / "work" / "apartment_split"
OUT_DIR = Path(__file__).resolve().parent.parent / "work" / "apartment_split_v2"

# Local (pre-rotation) OBJ axis: y is up. All rooms bottom out at y ~= 0.
# Anything entirely below this height is treated as floor-level clutter
# (floor slabs, thresholds, kickboards, rugs) and dropped in favor of the
# explicit flat floor plane -- not just perfectly flat faces, since a
# slightly-sloped low slab is just as capable of launching the robot as a
# perfectly flat one (see Sala_Cozinha_collision_59, a ~5cm-tall kickboard
# slab that a stricter "must be flat" filter missed).
FLOOR_Y_MAX = 0.06

THRESHOLD_M = 0.05  # real-metric concavity threshold, in meters

# Controls how much CoACD's manifold-remesh preprocessing inflates every
# surface -- see README's "Hull inflation" section for the measurements.
PREPROCESS_RESOLUTION = 400


def strip_floor(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    verts, faces = mesh.vertices, mesh.faces
    face_y = verts[faces][:, :, 1]
    max_y = face_y.max(axis=1)

    is_floor = max_y < FLOOR_Y_MAX
    kept_faces = faces[~is_floor]
    print(f"    stripped {is_floor.sum()}/{len(faces)} floor-level faces")

    used = sorted(set(kept_faces.flatten().tolist()))
    remap = {old: new for new, old in enumerate(used)}
    new_verts = verts[used]
    new_faces = np.array([[remap[a], remap[b], remap[c]] for a, b, c in kept_faces])
    return trimesh.Trimesh(vertices=new_verts, faces=new_faces, process=False)


def decompose_room(room_obj: Path, out_dir: Path) -> int:
    mesh = trimesh.load(room_obj, process=False, force="mesh")
    mesh = strip_floor(mesh)

    cmesh = coacd.Mesh(mesh.vertices, mesh.faces)
    parts = coacd.run_coacd(
        cmesh,
        threshold=THRESHOLD_M,
        real_metric=True,
        preprocess_resolution=PREPROCESS_RESOLUTION,
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob(f"{room_obj.stem}_collision_*.obj"):
        old.unlink()
    for i, (verts, faces) in enumerate(parts):
        piece = trimesh.Trimesh(vertices=verts, faces=faces, process=False)
        piece.export(out_dir / f"{room_obj.stem}_collision_{i}.obj")
    return len(parts)


def main() -> None:
    room_names = sys.argv[1:]
    if room_names:
        room_objs = [SPLIT_DIR / f"{name}.obj" for name in room_names]
    else:
        room_objs = sorted(SPLIT_DIR.glob("*.obj"))

    for room_obj in room_objs:
        if not room_obj.exists():
            print(f"skip {room_obj}: not found")
            continue
        print(f"{room_obj.stem}:")
        out_dir = OUT_DIR / room_obj.stem
        n = decompose_room(room_obj, out_dir)
        print(f"    -> {n} hulls in {out_dir}")


if __name__ == "__main__":
    main()
