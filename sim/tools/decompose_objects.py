"""Convex-decompose the standalone props in sim/assets/objects for MuJoCo.

The props (a dog, a ball) are authored meshes; their collision hulls are not.
The hulls are regenerated here from the authored `<thing>.obj`, so exactly one
file per asset needs pinning.

Deliberately NOT decompose_rooms.py: that one strips floor-level faces, because
a room's floor is topologically fused to its furniture and CoACD would carve it
into seam-generating slabs. A prop has no floor -- running the room pipeline on
a dog would delete its paws.

Every `<thing>.obj` that is not itself a `<thing>_collision_<n>.obj` is a
source. Outputs land beside their source, which is where the driver and
tools/build_viewer_physics.py both look for them.

Usage: cd sim && uv run tools/decompose_objects.py [name ...]
       (no args = every source mesh in sim/assets/objects)
"""

import sys
from pathlib import Path

import coacd
import trimesh

OBJECTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "objects"

# Props are small and hand-modelled next to a whole apartment, so a tighter
# threshold than decompose_rooms' 0.05 m keeps a dog's legs and head separable
# without exploding the hull count.
THRESHOLD_M = 0.02
PREPROCESS_RESOLUTION = 200

# Explicit rather than relying on coacd 1.0.11's default of 0 -- see the same
# constant in decompose_rooms.py for why an implicit seed is a liability once
# the output is a content-addressed image.
COACD_SEED = 0


def source_meshes(objects_dir: Path) -> list[Path]:
    return sorted(p for p in objects_dir.glob("*.obj") if "_collision_" not in p.name)


def decompose_object(src: Path) -> int:
    mesh = trimesh.load(src, process=False, force="mesh")
    parts = coacd.run_coacd(
        coacd.Mesh(mesh.vertices, mesh.faces),
        threshold=THRESHOLD_M,
        real_metric=True,
        preprocess_resolution=PREPROCESS_RESOLUTION,
        seed=COACD_SEED,
    )
    # Clear first: a re-run that produces fewer hulls must not leave the tail
    # of the previous run behind, or the soup would carry orphaned geometry.
    for old in src.parent.glob(f"{src.stem}_collision_*.obj"):
        old.unlink()
    for i, (verts, faces) in enumerate(parts):
        trimesh.Trimesh(vertices=verts, faces=faces, process=False).export(src.parent / f"{src.stem}_collision_{i}.obj")
    return len(parts)


def main() -> None:
    if not OBJECTS_DIR.is_dir():
        print(f"no {OBJECTS_DIR}, nothing to decompose")
        return
    names = sys.argv[1:]
    sources = [OBJECTS_DIR / f"{n}.obj" for n in names] if names else source_meshes(OBJECTS_DIR)
    for src in sources:
        if not src.is_file():
            print(f"skip {src}: not found")
            continue
        print(f"{src.stem}:")
        print(f"    -> {decompose_object(src)} hulls")


if __name__ == "__main__":
    main()
