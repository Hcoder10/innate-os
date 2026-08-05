"""Assemble and publish the sim asset bundle (generated geometry that stays
out of git -- collision hulls, SDF shells, room meshes, nav map, GLB/STLs).

Bundle layout (extracted by the launcher's ensure_sim_assets):
    work/    -> sim/assets/          the driver-side store the tools generate
    viewer/  -> sim/viewer/          browser-served assets (public/*, assets/*)
                                     + dist-lib/ (the built SimSession bundle,
                                     so users need no Node.js -- npm is only
                                     required here, at publish time)

The viewer's apartment_collisions_v2 (flat hulls + manifest.json) and
apartment_sdf are REBUILT here from the work/ store, so the two consumers can
never drift apart. models/robot/apartment_obj are carried over from the
checkout (static exports, they change ~never).

The tarball is deterministic (sorted entries, zeroed metadata), so identical
content always yields the identical tag: sim-assets-<sha256[:12]>.

Usage: cd sim && uv run tools/publish_assets.py            # build + verify only
       cd sim && uv run tools/publish_assets.py --publish  # + gh release, lock file
"""

import gzip
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

SIM = Path(__file__).resolve().parent.parent
ASSETS = SIM / "assets"
VIEWER = SIM / "viewer"
LOCK_FILE = SIM / "sim-assets.lock"
MARKER = ASSETS / ".assets-tag"
REPO = (
    "innate-inc/innate-sim-assets"  # dedicated repo: keeps asset tags out of innate-os (robots version-check its tags)
)

WORK_DIRS = ["apartment_split", "apartment_split_v2", "apartment_visual", "sdf_shells", "map", "humans", "objects"]

# Kept in sync with sim/README.md's Credits section.
ATTRIBUTION = (
    "# Attribution\n\n"
    + 'The apartment environment is derived from ["Appartement"](https://sketchfab.com/3d-models/appartement-6a7a5fe208344b2e8123a88923dbd5b3) by [SrMonteiro](https://sketchfab.com/crispimrafael), licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Changes were made: split per room, convex-decomposed for collision, re-exported for rendering (GLB/MuJoCo meshes), and rasterized into a navigation map.\n\n'
    + 'The scenario human is derived from ["Casual Man In Navy T-shirt And Jeans"](https://sketchfab.com/3d-models/casual-man-in-navy-t-shirt-and-jeans-bddc55b5a9d4406b982ec6de9b99531b) by [restore50](https://sketchfab.com/restore50), licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Changes were made: converted to a normalized MuJoCo OBJ (y-up centimeters, feet at origin) with the basecolor texture extracted.'
)
VIEWER_CARRYOVER = ["public/models", "public/robot", "assets/apartment_obj"]


def stage(root: Path) -> None:
    for d in WORK_DIRS:
        src = ASSETS / d
        if not src.is_dir():
            sys.exit(f"missing {src} -- run the asset pipeline first (see sandbox/README.md)")
        shutil.copytree(src, root / "work" / d, ignore=shutil.ignore_patterns("__pycache__", ".DS_Store"))

    hulls_dir = root / "viewer" / "public" / "physics" / "apartment_collisions_v2"
    hulls_dir.mkdir(parents=True)
    names = []
    for obj in sorted((root / "work" / "apartment_split_v2").glob("*/*_collision_*.obj")):
        shutil.copy2(obj, hulls_dir / obj.name)
        names.append(obj.name)
    (hulls_dir / "manifest.json").write_text(json.dumps(names, indent=1) + "\n")
    # One binary triangle soup of ALL hulls (float32 xyz per vertex, 3 verts
    # per tri): the browser overlay loads this in one fetch + zero parsing,
    # vs ~30s for 1300 individual OBJ fetches through the TLS proxy.
    soup = _hull_soup(hulls_dir, names)
    (hulls_dir / "hulls.f32").write_bytes(soup.tobytes())
    print(f"  apartment_collisions_v2: {len(names)} hulls, soup {soup.shape[0] // 3} tris")

    sdf_dir = root / "viewer" / "public" / "physics" / "apartment_sdf"
    sdf_dir.mkdir(parents=True)
    for obj in sorted((root / "work" / "sdf_shells").glob("*.obj")):
        shutil.copy2(obj, sdf_dir / obj.name)

    for rel in VIEWER_CARRYOVER:
        src = VIEWER / rel
        if not src.is_dir():
            sys.exit(f"missing {src} -- need a full asset checkout to publish")
        shutil.copytree(src, root / "viewer" / rel, ignore=shutil.ignore_patterns(".DS_Store"))

    # Ship the BUILT SimSession bundle so `up` never needs Node.js: rebuild
    # here (publishers are developers) so the published artifact can't be
    # stale relative to the viewer sources in this checkout.
    if shutil.which("npm") is None:
        sys.exit("npm is required to publish: the bundle ships the built sim viewer (dist-lib)")
    if not (VIEWER / "node_modules").is_dir():
        subprocess.run(["npm", "ci"], cwd=VIEWER, check=True)
    subprocess.run(["npm", "run", "build:lib"], cwd=VIEWER, check=True)
    shutil.copytree(VIEWER / "dist-lib", root / "viewer" / "dist-lib")

    # Progressive loading: split the apartment glb into per-room files + a
    # manifest (tools/split-apartment.mjs, pure split -- no re-encode), then
    # drop the monolith from the bundle. The webapp is its only runtime
    # consumer, so shipping both would nearly double the apartment's ~31 MB; the
    # source glb stays in the checkout for re-splitting + export_visual_rooms.py.
    staged_models = root / "viewer" / "public" / "models"
    subprocess.run(
        ["node", "tools/split-apartment.mjs", str(staged_models / "appartement.glb"), str(staged_models / "apartment")],
        cwd=VIEWER,
        check=True,
    )
    (staged_models / "appartement.glb").unlink()

    # CC BY attribution must travel with the distributed material -- this
    # bundle is downloadable without ever seeing the innate-os repo.
    (root / "ATTRIBUTION.md").write_text(ATTRIBUTION + "\n")


def _hull_soup(hulls_dir: Path, names: list[str]):
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


def deterministic_targz(root: Path, out: Path) -> str:
    """Write a reproducible .tar.gz of root and return its sha256 hex digest."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            info = tar.gettarinfo(path, arcname=str(path.relative_to(root)))
            info.uid = info.gid = info.mtime = 0
            info.uname = info.gname = ""
            with open(path, "rb") as f:
                tar.addfile(info, f)
    raw = buf.getvalue()
    with open(out, "wb") as f:
        # filename="" keeps the output name out of the gzip header, so the
        # digest depends on content only, not on where the tarball was built.
        with gzip.GzipFile(filename="", fileobj=f, mode="wb", mtime=0) as gz:
            gz.write(raw)
    return hashlib.sha256(out.read_bytes()).hexdigest()


def main() -> None:
    publish = "--publish" in sys.argv

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "bundle"
        root.mkdir()
        print("staging bundle...")
        stage(root)

        tarball = Path(tmp) / "sim-assets.tar.gz"
        sha = deterministic_targz(root, tarball)
        tag = f"sim-assets-{sha[:12]}"
        n_files = sum(1 for p in root.rglob("*") if p.is_file())
        size_mb = tarball.stat().st_size / 1e6
        print(f"{tag}: {n_files} files, {size_mb:.1f} MB compressed, sha256 {sha}")

        if not publish:
            print("dry run (no --publish): nothing uploaded, lock file unchanged")
            return

        asset_name = f"innate-sim-assets-{sha[:12]}.tar.gz"
        upload = tarball.with_name(asset_name)
        tarball.rename(upload)
        existing = subprocess.run(["gh", "release", "view", tag, "--repo", REPO], capture_output=True, text=True)
        if existing.returncode == 0:
            print(f"release {tag} already exists; skipping upload")
        else:
            subprocess.run(
                [
                    "gh",
                    "release",
                    "create",
                    tag,
                    str(upload),
                    "--repo",
                    REPO,
                    "--title",
                    tag,
                    "--notes",
                    f"Sim asset bundle ({n_files} files). Fetched by the launcher via sim/sim-assets.lock; "
                    f"regenerate with sim/tools/ (see sandbox/README.md), republish with tools/publish_assets.py. "
                    f"Apartment model: 'Appartement' by SrMonteiro (sketchfab.com/crispimrafael), CC BY 4.0; "
                    f"human model: 'Casual Man In Navy T-shirt And Jeans' by restore50 (sketchfab.com/restore50), "
                    f"CC BY 4.0; see ATTRIBUTION.md in the bundle.",
                ],
                check=True,
            )

        url = f"https://github.com/{REPO}/releases/download/{tag}/{asset_name}"
        LOCK_FILE.write_text(json.dumps({"tag": tag, "url": url, "sha256": sha}, indent=2) + "\n")

        # Install the staged viewer/ dirs locally: work/ came from this disk,
        # but the viewer's hulls/manifest/sdf were rebuilt above and may be
        # stale in the checkout, and the apartment split was just generated.
        # Only then is the marker's claim true.
        for rel in (
            "public/physics/apartment_collisions_v2",
            "public/physics/apartment_sdf",
            "public/models/apartment",
        ):
            dest = VIEWER / rel
            shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(root / "viewer" / rel, dest)
        MARKER.write_text(tag + "\n")
        print(f"published {url}\nwrote {LOCK_FILE.relative_to(SIM.parent)}, installed viewer dirs + marker")


if __name__ == "__main__":
    main()
