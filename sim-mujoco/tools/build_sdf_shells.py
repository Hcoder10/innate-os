"""Build watertight "shell" meshes for MuJoCo native SDF collision (the
no-decomposition alternative to decompose_rooms.py -- see sdf_spike.py).

The raw room meshes are unwelded triangle soup, and an SDF built from them
has garbage inside/outside sign in places -- confirmed: phantom contacts at
the spawn point launched the robot at 9.5 m/s (floor-stripped mesh) or
diverged to NaN (raw mesh). Voxelizing each room and running marching cubes
over the (boundary-padded) occupancy grid produces a genuinely watertight
shell whose SDF is well-defined everywhere; the robot then drives the tour
cleanly (max speed 2.77 m/s, same as the hull world).

The marching-cubes surface sits ~PITCH/2 outside the true surface -- the
same kind of inflation as CoACD's preprocessing, but since there's no
decomposition step the pitch can be small without paying an hours-long bake.

Marching cubes also over-tessellates (~800k faces for Sala_Cozinha, a 65MB
OBJ -- unusable in the browser), so shells are quadric-decimated; target
face counts keep the geometric error well under a voxel. The decimator must
preserve topology: fast_simplification's plain collapse left ~30
non-manifold edges per room, which corrupted the SDF sign enough to diverge
the drive tour to NaN; pymeshlab's preservetopology QEM leaves the shell
SDF-clean (verified: same 2.77 m/s tour max speed as the undecimated shell).

Marching cubes must run on a SMOOTHED occupancy field, not the raw binary
grid: binary marching cubes leaves non-manifold corner junctions (edges
shared by >2 faces at diagonal voxel contacts), and even a handful of them
corrupts the octree SDF's sign locally -- the drive tour then hits one-step
phantom deep-penetration impulses (hundreds of m/s from a standstill, ncon=0
on either side) and eventually diverges to NaN. A gaussian-smoothed float
field extracted at level 0.5 is watertight with zero non-manifold edges,
before and after decimation, and the tour is clean. Corners get rounded by
~half a voxel, which is within the shell's inherent inflation anyway.

Output: work/sdf_shells/<room>.obj (+ manifest-ready file sizes printed).

Usage: uv run --with scikit-image --with pymeshlab --with scipy build_sdf_shells.py [room ...]
"""

import sys
import time
from pathlib import Path

import numpy as np
import pymeshlab
import trimesh
from scipy.ndimage import gaussian_filter
from skimage import measure

from decompose_rooms import SPLIT_DIR

OUT_DIR = Path(__file__).resolve().parent.parent / "work" / "sdf_shells"

PITCH = 0.025  # voxel size in meters; shell sits ~PITCH/2 off the true surface
TARGET_FACES = 60_000  # per room, post-decimation; error stays well under a voxel


def build_shell(room_obj: Path) -> Path:
    mesh = trimesh.load(room_obj, process=False, force="mesh")

    t0 = time.perf_counter()
    vox = mesh.voxelized(pitch=PITCH)
    # Pad so surfaces touching the grid boundary still close, then smooth --
    # see the module docstring for why binary marching cubes is not an option.
    field = gaussian_filter(np.pad(vox.matrix, 2).astype(np.float32), sigma=1.0)
    mc_verts, mc_faces, _, _ = measure.marching_cubes(field, level=0.5)
    shell = trimesh.Trimesh(vertices=mc_verts * PITCH, faces=mc_faces, process=False)
    shell.apply_translation(vox.translation - 2 * PITCH)
    print(
        f"    voxelized {vox.shape} + smoothed marching cubes in {time.perf_counter() - t0:.0f}s: "
        f"{len(shell.faces)} faces, watertight={shell.is_watertight}"
    )

    if len(shell.faces) > TARGET_FACES:
        t0 = time.perf_counter()
        ms = pymeshlab.MeshSet()
        ms.add_mesh(pymeshlab.Mesh(shell.vertices, shell.faces))
        ms.meshing_decimation_quadric_edge_collapse(
            targetfacenum=TARGET_FACES, preservetopology=True, preservenormal=True
        )
        m = ms.current_mesh()
        shell = trimesh.Trimesh(
            vertices=m.vertex_matrix(), faces=m.face_matrix(), process=False
        )
        print(
            f"    decimated to {len(shell.faces)} faces in {time.perf_counter() - t0:.0f}s, "
            f"watertight={shell.is_watertight}"
        )

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / room_obj.name
    shell.export(out)
    print(f"    -> {out} ({out.stat().st_size / 1e6:.1f} MB)")
    return out


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
        build_shell(room_obj)


if __name__ == "__main__":
    main()
