"""Restore the viewer's prop GLBs after an asset-bundle re-fetch wipes them.

The browser viewer draws GLBs from sim/viewer/public/models/ -- a gitignored,
bundle-managed directory that any asset re-fetch replaces wholesale. This
tool restores each prop's GLB, preferring the ORIGINAL (full PBR maps) from
the source-asset checkout next to the repos (../assets, see SOURCE_GLBS), and
falling back to synthesizing one from sim/assets/objects/{kind}.obj +
{kind}_basecolor.png -- the exact mesh+texture the robot's physics and
cameras use (convert_objects.py output) -- so a lost original degrades the
looks, never the availability. It also rebuilds labrador_hulls.f32 (the
collision-overlay soup) from the CoACD pieces.

Converted OBJs are Z-up (MuJoCo body frame); glTF is Y-up and scene.ts
rotates props back with rotateToZUp, so synthesized vertices are rotated
Z-up -> Y-up here ((x, y, z) -> (x, z, -y)).

Usage: cd sim && uv run tools/build_prop_glbs.py
"""

import shutil
import sys
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image

SIM = Path(__file__).resolve().parent.parent
OBJECTS = SIM / "assets" / "objects"
MODELS = SIM / "viewer" / "public" / "models"
SRC_ASSETS = SIM.parent.parent / "assets"

PROPS = ["soccer_ball", "labrador"]

# kind (viewer name) -> original download in the source-asset checkout.
SOURCE_GLBS = {
    "soccer_ball": SRC_ASSETS / "objects" / "generic_soccer_ball.glb",
    "labrador": SRC_ASSETS / "objects" / "labrador_dog.glb",
    "human": SRC_ASSETS / "humans" / "casual-man-in-navy-t-shirt-and-jeans" / "source"
    / "Casual man in navy t-shirt and jeans.glb",
}


def load_obj_with_uv(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Parse v/vt/f, welding per-corner (v, vt) pairs so every vertex has one
    UV (glTF requirement; OBJ indexes positions and UVs independently)."""
    verts, uvs, corners = [], [], []
    for line in path.read_text().splitlines():
        if line.startswith("v "):
            verts.append([float(c) for c in line.split()[1:4]])
        elif line.startswith("vt "):
            uvs.append([float(c) for c in line.split()[1:3]])
        elif line.startswith("f "):
            face = []
            for part in line.split()[1:4]:
                idx = part.split("/")
                face.append((int(idx[0]) - 1, int(idx[1]) - 1 if len(idx) > 1 and idx[1] else 0))
            corners.append(face)
    verts, uvs = np.asarray(verts), np.asarray(uvs)
    pairs = {}
    out_v, out_uv, faces = [], [], []
    for face in corners:
        tri = []
        for pair in face:
            if pair not in pairs:
                pairs[pair] = len(out_v)
                out_v.append(verts[pair[0]])
                out_uv.append(uvs[pair[1]] if len(uvs) else (0.0, 0.0))
            tri.append(pairs[pair])
        faces.append(tri)
    return np.asarray(out_v), np.asarray(out_uv), np.asarray(faces)


def build_glb(kind: str) -> None:
    obj = OBJECTS / f"{kind}.obj"
    png = OBJECTS / f"{kind}_basecolor.png"
    if not obj.exists() or not png.exists():
        sys.exit(f"missing {obj} / {png} -- run tools/convert_objects.py first")
    v, uv, f = load_obj_with_uv(obj)
    v = v @ np.array([[1, 0, 0], [0, 0, -1], [0, 1, 0]], dtype=float)  # Z-up -> glTF Y-up
    mesh = trimesh.Trimesh(vertices=v, faces=f, process=False)
    mesh.visual = trimesh.visual.texture.TextureVisuals(
        uv=uv, material=trimesh.visual.material.PBRMaterial(baseColorTexture=Image.open(png))
    )
    out = MODELS / f"{kind}.glb"
    out.write_bytes(trimesh.Scene([mesh]).export(file_type="glb"))
    print(f"{out.name}: {len(v)} verts, extents {np.round(mesh.extents, 3)}")


def build_hull_soup(kind: str) -> None:
    pieces = sorted(OBJECTS.glob(f"{kind}_collision_*.obj"))
    if not pieces:
        return
    chunks = []
    for piece in pieces:
        verts, faces = [], []
        for line in piece.read_text().splitlines():
            if line.startswith("v "):
                verts.append([float(c) for c in line.split()[1:4]])
            elif line.startswith("f "):
                faces.append([int(t.split("/")[0]) - 1 for t in line.split()[1:4]])
        v = np.asarray(verts, dtype=np.float32)
        chunks.append(v[np.asarray(faces, dtype=np.int32)].reshape(-1, 3))
    soup = np.concatenate(chunks)
    (MODELS / f"{kind}_hulls.f32").write_bytes(soup.tobytes())
    print(f"{kind}_hulls.f32: {len(pieces)} hulls, {soup.shape[0] // 3} tris")


def main() -> None:
    MODELS.mkdir(parents=True, exist_ok=True)
    for kind in PROPS:
        source = SOURCE_GLBS.get(kind)
        if source is not None and source.exists():
            shutil.copy2(source, MODELS / f"{kind}.glb")
            print(f"{kind}.glb: installed original ({source.name})")
        else:
            build_glb(kind)
        build_hull_soup(kind)
    human_src = SOURCE_GLBS["human"]
    if human_src.exists():
        shutil.copy2(human_src, MODELS / "human.glb")
        print(f"human.glb: installed from {human_src.name}")
    elif not (MODELS / "human.glb").exists():
        print(f"WARNING: no human.glb and no source at {human_src} -- the viewer will miss the human")


if __name__ == "__main__":
    main()
