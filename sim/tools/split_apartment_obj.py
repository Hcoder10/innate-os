"""Splits the full Blender export (assets/apartment_obj/apartment.obj) into
one clean OBJ per named object ('o' group), keeping only positions + face
topology (no materials/UVs/normals -- collision doesn't need them).

The file has no vertex welding between adjacent surfaces (each named group
is thousands of disconnected triangle islands), so per-group is the coarsest
sane unit to feed a convex decomposer -- finer splitting by connectivity
would yield tens of thousands of pieces, most of them tiny fragments of the
same physical wall/floor slab.

Usage: uv run split_apartment_obj.py [output_dir]
"""

import sys
from pathlib import Path

SIM_WEB = Path(__file__).resolve().parent.parent / "viewer"
SRC = SIM_WEB / "assets" / "apartment_obj" / "apartment.obj"


def split(src: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)

    all_verts: list[tuple[float, float, float]] = []
    groups: list[tuple[str, list[tuple[int, int, int]]]] = []
    current_name: str | None = None
    current_faces: list[tuple[int, int, int]] = []

    def flush() -> None:
        if current_name is not None:
            groups.append((current_name, current_faces))

    for line in src.read_text().splitlines():
        if line.startswith("v "):
            x, y, z = (float(p) for p in line.split()[1:4])
            all_verts.append((x, y, z))
        elif line.startswith("o "):
            flush()
            current_name = line[2:].strip()
            current_faces = []
        elif line.startswith("f "):
            idx = [int(p.split("/")[0]) for p in line.split()[1:]]
            for i in range(1, len(idx) - 1):
                current_faces.append((idx[0], idx[i], idx[i + 1]))
    flush()

    print(f"total vertices in file: {len(all_verts)}")
    for name, faces in groups:
        used = sorted({v for f in faces for v in f})
        remap = {g: i + 1 for i, g in enumerate(used)}
        local_verts = [all_verts[g - 1] for g in used]

        lines = [f"v {x} {y} {z}" for x, y, z in local_verts]
        lines += [f"f {remap[a]} {remap[b]} {remap[c]}" for a, b, c in faces]

        out_path = out_dir / f"{name}.obj"
        out_path.write_text("\n".join(lines) + "\n")
        print(f"{name}: verts={len(local_verts)} faces={len(faces)} -> {out_path}")


if __name__ == "__main__":
    out_dir = (
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else Path(__file__).resolve().parent.parent / "assets" / "apartment_split"
    )
    split(SRC, out_dir)
