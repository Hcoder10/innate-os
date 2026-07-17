"""Convert Blender-exported scenario prop OBJs into the MuJoCo convention the
driver loads (see world.py OBJECT_KINDS): Z-up meters, centered on the bbox
center, resized to the real-world size below. The basecolor texture is pulled
out of the matching viewer GLB (Blender's OBJ export writes no textures), so
the robot cameras see the same object the browser viewer draws.

Kinds listed in DECOMPOSE also get CoACD convex-decomposed collision pieces
({kind}_collision_*.obj, same body frame) so they collide as their real shape
rather than a bounding primitive, plus a {kind}_hulls.f32 triangle soup
written next to the viewer GLBs for the browser's collision overlay (the same
format as the apartment's hulls.f32; public/models ships wholesale in the
asset bundle, so it needs no extra staging).

Inputs: a Blender OBJ export (default Y-up / -Z-forward) per prop from the
source-asset checkout next to this repo (../assets/objects, like the human's
../assets/humans), plus the viewer GLB in sim/viewer/public/models/. Outputs
land in sim/assets/objects/ (bundled by publish_assets.py, like humans/).

The Blender scene, its OBJ export, and the viewer GLB must be the SAME
geometry: the OBJ's bbox defines the physics frame and scene.ts normalizes
the GLB by its own bbox, so any geometry present in only one of them (e.g.
deleted scan debris) shifts the two out of alignment.

Usage: cd sim && uv run tools/convert_objects.py
"""

import io
import json
import struct
import sys
from pathlib import Path

SIM = Path(__file__).resolve().parent.parent
SRC_DIR = SIM.parent.parent / "assets" / "objects"
OUT_DIR = SIM / "assets" / "objects"
VIEWER_MODELS = SIM / "viewer" / "public" / "models"

# kind -> (blender OBJ, viewer GLB, real-world size in meters of the LONGEST
# bbox side). Sizes must match world.py's collision primitives and the
# viewer's scene.ts OBJECTS fitSizeM.
PROPS = {
    "soccer_ball": (SRC_DIR / "soccer_ball.obj", VIEWER_MODELS / "soccer_ball.glb", 0.22),
    "labrador": (SRC_DIR / "dog_fixed.obj", VIEWER_MODELS / "labrador.glb", 1.0),
}

# kind -> CoACD real-metric concavity threshold (m). The ball stays a sphere
# primitive (exact and it rolls); the dog gets its true shape -- legs, head,
# the gap under the belly.
DECOMPOSE = {"labrador": 0.03}


def extract_basecolor(glb_path: Path, out_png: Path) -> None:
    """Write material 0's baseColor image from a GLB as PNG (MuJoCo textures
    are PNG-only, so JPEG payloads are converted)."""
    data = glb_path.read_bytes()
    assert data[:4] == b"glTF", f"{glb_path} is not a GLB"
    json_len = struct.unpack("<I", data[12:16])[0]
    gltf = json.loads(data[20 : 20 + json_len])
    bin_start = 20 + json_len + 8  # skip the BIN chunk header
    material = gltf["materials"][0]
    image_index = gltf["textures"][material["pbrMetallicRoughness"]["baseColorTexture"]["index"]]["source"]
    image = gltf["images"][image_index]
    view = gltf["bufferViews"][image["bufferView"]]
    payload = data[bin_start + view.get("byteOffset", 0) :][: view["byteLength"]]

    if image["mimeType"] == "image/png":
        out_png.write_bytes(payload)
    else:
        from PIL import Image

        Image.open(io.BytesIO(payload)).convert("RGB").save(out_png)


def convert_obj(src: Path, dst: Path, size_m: float) -> tuple[float, float, float]:
    """Rewrite a Blender OBJ (Y-up) as Z-up meters, longest side = size_m,
    bbox-centered; returns the final (x, y, z) extents for the collision
    primitive in world.py. Vertices and normals rotate Y-up -> Z-up
    ((x, y, z) -> (x, -z, y)); texcoords and faces pass through. Object/group/
    material lines are DROPPED to merge everything into one shape: MuJoCo's
    OBJ loader keeps only the first object, silently discarding the rest."""
    verts: list[tuple[float, float, float]] = []
    lines = src.read_text().splitlines()
    for line in lines:
        if line.startswith("v "):
            x, y, z = (float(c) for c in line.split()[1:4])
            verts.append((x, -z, y))
    lo = [min(v[i] for v in verts) for i in range(3)]
    hi = [max(v[i] for v in verts) for i in range(3)]
    scale = size_m / max(h - l for h, l in zip(hi, lo, strict=True))
    center = [(h + l) / 2 for h, l in zip(hi, lo, strict=True)]

    out = [f"# {src.name} converted to sim prop convention (z-up meters, bbox-centered)"]
    vi = 0
    for line in lines:
        if line.startswith("v "):
            v = [(verts[vi][i] - center[i]) * scale for i in range(3)]
            vi += 1
            out.append(f"v {v[0]:.6f} {v[1]:.6f} {v[2]:.6f}")
        elif line.startswith("vn "):
            x, y, z = (float(c) for c in line.split()[1:4])
            out.append(f"vn {x:.4f} {-z:.4f} {y:.4f}")
        elif line.startswith(("vt ", "f ")):
            out.append(line)
    dst.write_text("\n".join(out) + "\n")
    return tuple((h - l) * scale for h, l in zip(hi, lo, strict=True))  # type: ignore[return-value]


def decompose(kind: str, mesh_obj: Path, threshold_m: float) -> int:
    """CoACD the converted mesh into convex collision pieces (driver side)
    and one wireframe triangle soup (viewer side); returns the piece count."""
    import coacd
    import numpy as np
    import trimesh

    # preprocess_resolution as in decompose_rooms.py: the default (50) remeshes
    # a ~1m prop with ~2cm voxels, aliasing thin parts (paws, tail) into
    # detached junk fragments.
    mesh = trimesh.load(mesh_obj, process=False, force="mesh")
    parts = coacd.run_coacd(
        coacd.Mesh(mesh.vertices, mesh.faces),
        threshold=threshold_m,
        real_metric=True,
        preprocess_resolution=200,
    )

    for old in OUT_DIR.glob(f"{kind}_collision_*.obj"):
        old.unlink()
    soup = []
    for i, (verts, faces) in enumerate(parts):
        piece = trimesh.Trimesh(vertices=verts, faces=faces, process=False)
        piece.export(OUT_DIR / f"{kind}_collision_{i}.obj")
        soup.append(piece.vertices[piece.faces].reshape(-1, 3))
    (VIEWER_MODELS / f"{kind}_hulls.f32").write_bytes(np.concatenate(soup).astype(np.float32).tobytes())
    return len(parts)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for kind, (obj_path, glb_path, size_m) in PROPS.items():
        if not obj_path.exists():
            sys.exit(f"missing {obj_path} -- export it from Blender first")
        png = OUT_DIR / f"{kind}_basecolor.png"
        extract_basecolor(glb_path, png)
        extents = convert_obj(obj_path, OUT_DIR / f"{kind}.obj", size_m)
        hulls = f", {decompose(kind, OUT_DIR / f'{kind}.obj', DECOMPOSE[kind])} hulls" if kind in DECOMPOSE else ""
        print(f"{kind}: extents ({extents[0]:.3f}, {extents[1]:.3f}, {extents[2]:.3f}) m, texture {png.name}{hulls}")


if __name__ == "__main__":
    main()
