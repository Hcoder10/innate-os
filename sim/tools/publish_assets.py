"""Assemble and publish the sim asset bundle (generated geometry that stays
out of git -- collision hulls, SDF shells, room meshes, nav map, GLB/STLs).

Bundle layout (extracted by the launcher's ensure_sim_assets):
    work/    -> sim/assets/          the driver-side store the tools generate
    viewer/  -> sim/viewer/          browser-served assets (public/*, assets/*)

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
REPO = "innate-inc/innate-os"

WORK_DIRS = ["apartment_split", "apartment_split_v2", "apartment_visual", "sdf_shells", "map"]
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
    print(f"  apartment_collisions_v2: {len(names)} hulls")

    sdf_dir = root / "viewer" / "public" / "physics" / "apartment_sdf"
    sdf_dir.mkdir(parents=True)
    for obj in sorted((root / "work" / "sdf_shells").glob("*.obj")):
        shutil.copy2(obj, sdf_dir / obj.name)

    for rel in VIEWER_CARRYOVER:
        src = VIEWER / rel
        if not src.is_dir():
            sys.exit(f"missing {src} -- need a full asset checkout to publish")
        shutil.copytree(src, root / "viewer" / rel, ignore=shutil.ignore_patterns(".DS_Store"))


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
                    f"regenerate with sim/tools/ (see sandbox/README.md), republish with tools/publish_assets.py.",
                ],
                check=True,
            )

        url = f"https://github.com/{REPO}/releases/download/{tag}/{asset_name}"
        LOCK_FILE.write_text(json.dumps({"tag": tag, "url": url, "sha256": sha}, indent=2) + "\n")

        # Install the staged viewer/ dirs locally: work/ came from this disk,
        # but the viewer's hulls/manifest/sdf were rebuilt above and may be
        # stale in the checkout. Only then is the marker's claim true.
        for rel in ("public/physics/apartment_collisions_v2", "public/physics/apartment_sdf"):
            dest = VIEWER / rel
            shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(root / "viewer" / rel, dest)
        MARKER.write_text(tag + "\n")
        print(f"published {url}\nwrote {LOCK_FILE.relative_to(SIM.parent)}, installed viewer dirs + marker")


if __name__ == "__main__":
    main()
