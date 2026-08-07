"""Pack the browser-side physics assets from the generated collision store.

The driver and the viewer must never drift apart, so the viewer's copies are
always REBUILT from sim/assets rather than edited by hand:

    assets/apartment_split_v2/<Room>/<Room>_collision_<n>.obj
        -> viewer/public/physics/apartment_collisions_v2/   (flat, + manifest.json + hulls.f32)
    assets/objects/<thing>_collision_<n>.obj
        -> viewer/public/models/<thing>_hulls.f32

Runs as a stage of sim/Dockerfile.assets, and stands alone for local
regeneration.

Usage: cd sim && uv run tools/build_viewer_physics.py [physics_dir] [models_dir]
       (defaulting to viewer/public/physics and viewer/public/models)
"""

import json
import shutil
import sys
from pathlib import Path

SIM = Path(__file__).resolve().parent.parent
ASSETS = SIM / "assets"
DEFAULT_OUT = SIM / "viewer" / "public" / "physics"
DEFAULT_MODELS_OUT = SIM / "viewer" / "public" / "models"


def hull_soup(hulls_dir: Path, names: list[str]):
    """Concatenate every hull OBJ into one (N*3, 3) float32 triangle soup."""
    import numpy as np

    chunks = []
    for name in names:
        verts, faces = [], []
        for line in (hulls_dir / name).read_text().splitlines():
            if line.startswith("v "):
                verts.append([float(v) for v in line.split()[1:4]])
            elif line.startswith("f "):
                faces.append([int(t.split("/")[0]) - 1 for t in line.split()[1:4]])
        v = np.asarray(verts, dtype=np.float32)
        chunks.append(v[np.asarray(faces, dtype=np.int32)].reshape(-1, 3))
    return np.concatenate(chunks)


def build_collisions(out_root: Path) -> int:
    src = ASSETS / "apartment_split_v2"
    if not src.is_dir():
        sys.exit(f"missing {src} -- run the decomposition first (see sandbox/README.md)")

    hulls_dir = out_root / "apartment_collisions_v2"
    shutil.rmtree(hulls_dir, ignore_errors=True)
    hulls_dir.mkdir(parents=True)

    names = []
    for obj in sorted(src.glob("*/*_collision_*.obj")):
        shutil.copy2(obj, hulls_dir / obj.name)
        names.append(obj.name)
    if not names:
        sys.exit(f"no collision hulls under {src}")

    (hulls_dir / "manifest.json").write_text(json.dumps(names, indent=1) + "\n")
    # One binary triangle soup of ALL hulls (float32 xyz per vertex, 3 verts
    # per tri): the browser overlay loads this in one fetch + zero parsing,
    # vs ~30s for 1300 individual OBJ fetches through the TLS proxy.
    soup = hull_soup(hulls_dir, names)
    (hulls_dir / "hulls.f32").write_bytes(soup.tobytes())
    print(f"  apartment_collisions_v2: {len(names)} hulls, soup {soup.shape[0] // 3} tris")
    return len(names)


def build_object_hulls(models_root: Path) -> int:
    """One `<thing>_hulls.f32` per prop in assets/objects.

    Same pack as the apartment's, from the same collision OBJs MuJoCo itself
    loads -- so the browser overlay cannot drift from the driver.
    """
    src = ASSETS / "objects"
    if not src.is_dir():
        print("  object hulls: no objects store, skipping")
        return 0
    models_root.mkdir(parents=True, exist_ok=True)
    n = 0
    for first in sorted(src.glob("*_collision_0.obj")):
        stem = first.name[: -len("_collision_0.obj")]
        # Plain lexicographic order (_0, _1, _10, ... _2), matching how the
        # apartment soup is packed.
        names = sorted(p.name for p in src.glob(f"{stem}_collision_*.obj"))
        soup = hull_soup(src, names)
        (models_root / f"{stem}_hulls.f32").write_bytes(soup.tobytes())
        print(f"  {stem}_hulls.f32: {len(names)} hulls, soup {soup.shape[0] // 3} tris")
        n += 1
    return n


def main() -> None:
    out_root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_OUT
    models_root = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else DEFAULT_MODELS_OUT
    out_root.mkdir(parents=True, exist_ok=True)
    print(f"packing viewer physics into {out_root}")
    build_collisions(out_root)
    build_object_hulls(models_root)


if __name__ == "__main__":
    main()
