"""Extracts per-room textured visual meshes from the same appartement.glb
the viewer renders (via Three.js's GLTFLoader) into OBJ+JPEG files MuJoCo can
load directly, so the native viewer can show the same look instead of only
the palette-colored collision hulls.

Why not just point MuJoCo at the .glb? MuJoCo has no glTF importer -- meshes
must come from OBJ/STL/MSH/PLY, and textures from PNG/JPEG referenced by an
explicit <material>/<texture> pair in the MJCF. trimesh's glTF loader also
doesn't work here: each room's baseColorTexture is bound to TEXCOORD_1 (a
second, baked-lightmap UV channel; TEXCOORD_0 is present but unrelated) or,
for one room, defaults to TEXCOORD_0 -- a per-material detail trimesh's
simple scene loader doesn't resolve, so it silently returns
material.baseColorTexture = None. This script instead reads the GLB's
JSON+BIN chunks directly and honors each primitive's own material.

Each of the 5 room meshes in the GLB has its own baked material + texture
image and its own node translation (no rotation/scale) -- verified against
assets/apartment_obj/apartment.obj (the Blender OBJ export the collision
pipeline already uses) by comparing bounding boxes: they match exactly once
the node translation is added to the raw mesh-local vertex positions.

Usage: uv run export_visual_rooms.py [out_dir]
    (default out_dir: sim/assets/apartment_visual)
"""

import io
import json
import struct
import sys
from pathlib import Path

import numpy as np
from PIL import Image

GLB_PATH = Path(__file__).resolve().parent.parent / "viewer" / "public" / "models" / "appartement.glb"

_COMPONENT_DTYPES = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
_TYPE_NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}


def load_glb(path: Path) -> tuple[dict, bytes]:
    data = path.read_bytes()
    magic, version, _length = struct.unpack("<4sII", data[:12])
    assert magic == b"glTF", f"not a GLB file: {path}"

    gltf, bin_chunk = None, None
    offset = 12
    while offset < len(data):
        chunk_len, chunk_type = struct.unpack("<I4s", data[offset : offset + 8])
        chunk_data = data[offset + 8 : offset + 8 + chunk_len]
        if chunk_type == b"JSON":
            gltf = json.loads(chunk_data)
        elif chunk_type == b"BIN\x00":
            bin_chunk = chunk_data
        offset += 8 + chunk_len
    assert gltf is not None and bin_chunk is not None, f"missing JSON or BIN chunk in {path}"
    return gltf, bin_chunk


def read_accessor(gltf: dict, bin_chunk: bytes, accessor_idx: int) -> np.ndarray:
    acc = gltf["accessors"][accessor_idx]
    bv = gltf["bufferViews"][acc["bufferView"]]
    dtype = _COMPONENT_DTYPES[acc["componentType"]]
    ncomp = _TYPE_NCOMP[acc["type"]]
    elem_size = np.dtype(dtype).itemsize * ncomp
    start = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    stride = bv.get("byteStride", elem_size)
    count = acc["count"]

    if stride == elem_size:
        raw = bin_chunk[start : start + count * elem_size]
        return np.frombuffer(raw, dtype=dtype).reshape(count, ncomp)

    out = np.zeros((count, ncomp), dtype=dtype)
    for i in range(count):
        off = start + i * stride
        out[i] = np.frombuffer(bin_chunk[off : off + elem_size], dtype=dtype)
    return out


def export_room(gltf: dict, bin_chunk: bytes, node: dict, out_dir: Path) -> Path:
    name = node["name"]
    mesh = gltf["meshes"][node["mesh"]]
    prim = mesh["primitives"][0]
    material = gltf["materials"][prim["material"]]
    base_color_tex = material["pbrMetallicRoughness"]["baseColorTexture"]
    tex_coord_set = base_color_tex.get("texCoord", 0)

    positions = read_accessor(gltf, bin_chunk, prim["attributes"]["POSITION"]).astype(np.float64)
    positions += np.array(node.get("translation", [0.0, 0.0, 0.0]))
    normals = read_accessor(gltf, bin_chunk, prim["attributes"]["NORMAL"])
    uvs = read_accessor(gltf, bin_chunk, prim["attributes"][f"TEXCOORD_{tex_coord_set}"])
    indices = read_accessor(gltf, bin_chunk, prim["indices"]).reshape(-1)

    texture = gltf["textures"][base_color_tex["index"]]
    image = gltf["images"][texture["source"]]
    image_bv = gltf["bufferViews"][image["bufferView"]]
    image_start = image_bv.get("byteOffset", 0)
    image_bytes = bin_chunk[image_start : image_start + image_bv["byteLength"]]

    room_dir = out_dir / name
    room_dir.mkdir(parents=True, exist_ok=True)
    # MuJoCo's <texture> loader only accepts PNG (or its own raw binary
    # format) -- the GLB's baked textures are JPEG, so re-encode.
    png_path = room_dir / f"{name}.png"
    Image.open(io.BytesIO(image_bytes)).convert("RGB").save(png_path)

    obj_path = room_dir / f"{name}.obj"
    lines = [f"v {x:.6f} {y:.6f} {z:.6f}" for x, y, z in positions]
    lines += [f"vt {u:.6f} {1.0 - v:.6f}" for u, v in uvs]  # glTF V is top-down; OBJ/OpenGL is bottom-up
    lines += [f"vn {x:.6f} {y:.6f} {z:.6f}" for x, y, z in normals]
    for i in range(0, len(indices), 3):
        a, b, c = int(indices[i]) + 1, int(indices[i + 1]) + 1, int(indices[i + 2]) + 1
        lines.append(f"f {a}/{a}/{a} {b}/{b}/{b} {c}/{c}/{c}")
    obj_path.write_text("\n".join(lines) + "\n")

    print(f"{name}: {len(positions)} verts, {len(indices) // 3} tris -> {obj_path.name} + {png_path.name}")
    return obj_path


def main() -> None:
    out_dir = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else Path(__file__).resolve().parent.parent / "assets" / "apartment_visual"
    )
    gltf, bin_chunk = load_glb(GLB_PATH)
    for node in gltf["nodes"]:
        if "mesh" in node:
            export_room(gltf, bin_chunk, node, out_dir)


if __name__ == "__main__":
    main()
