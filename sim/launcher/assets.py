from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from config import CLI_SIM, REPO_ROOT, STATE_DIR, StackError, log

ASSET_LOCK_PATH = REPO_ROOT / "sim" / "assets.lock.json"
ASSET_STATE_PATH = STATE_DIR / "sim-assets.sha256"
DEFAULT_ASSET_IMAGE = "ghcr.io/innate-inc/innate-os-sim-assets"
ASSET_CONTAINER_PATH = "/sim-data"

DEFAULT_ASSET_FILES = (
    "data/assets/walking_man/man.jpg",
    "data/assets/walking_man/man.mtl",
    "data/assets/walking_man/man.obj",
    "data/replica_scene.stl",
    "data/urdf/maurice.urdf",
    "data/urdf/meshes/base.STL",
    "data/urdf/meshes/link1.STL",
    "data/urdf/meshes/link2.STL",
    "data/urdf/meshes/link3.STL",
    "data/urdf/meshes/link4.STL",
    "data/urdf/meshes/link5.STL",
    "data/urdf/meshes/link61.STL",
    "data/urdf/meshes/link62.STL",
)
ASSET_PATH_PATTERN = re.compile(r"data/(?:assets/[^\"'\\\s]+|urdf/[^\"'\\\s]+|replica_scene\.stl)")
ASSET_REFERENCE_SUFFIXES = (
    ".bmp",
    ".dae",
    ".glb",
    ".jpg",
    ".jpeg",
    ".mtl",
    ".obj",
    ".png",
    ".stl",
    ".STL",
    ".urdf",
)
ASSET_RELEVANT_PREFIXES = (
    "sim/assets.lock.json",
    "sim/data/",
    "sim/src/simulation/",
)
ASSET_PAYLOAD_PREFIXES = (
    "sim/data/assets/",
    "sim/data/urdf/",
)
ASSET_PAYLOAD_EXACT_PATHS = ("sim/data/replica_scene.stl",)


def run_checked(cmd: list[str], *, cwd: Path, failure_message: str) -> str:
    result = subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode != 0:
        output = (result.stdout or "").strip()
        if len(output) > 4000:
            output = output[-4000:]
        raise StackError(f"{failure_message}\n{output}")
    return result.stdout or ""


def load_asset_lock() -> dict[str, Any]:
    if not ASSET_LOCK_PATH.exists():
        raise StackError(
            f"Simulator asset lock is missing: {ASSET_LOCK_PATH}\n"
            "Run the asset publishing flow before merging simulator asset references."
        )
    try:
        data = json.loads(ASSET_LOCK_PATH.read_text())
    except json.JSONDecodeError as exc:
        raise StackError(f"Invalid simulator asset lock JSON: {exc}") from exc
    if data.get("version") != 1:
        raise StackError("Unsupported simulator asset lock version.")
    if not data.get("image_ref"):
        raise StackError("Simulator asset lock is missing image_ref.")
    if not data.get("content_sha256"):
        raise StackError("Simulator asset lock is missing content_sha256.")
    if not isinstance(data.get("required_files"), list):
        raise StackError("Simulator asset lock is missing required_files.")
    return data


def write_asset_lock(lock: dict[str, Any]) -> None:
    ASSET_LOCK_PATH.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")


def required_asset_paths(lock: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []
    for raw_path in lock["required_files"]:
        if not isinstance(raw_path, str):
            raise StackError("Simulator asset lock required_files must contain strings.")
        path = Path(raw_path)
        if path.is_absolute() or ".." in path.parts:
            raise StackError(f"Unsafe simulator asset path in lock: {raw_path}")
        paths.append(path)
    return sorted(paths, key=lambda item: item.as_posix())


def missing_asset_files(sim_repo: Path, lock: dict[str, Any]) -> list[Path]:
    return [sim_repo / path for path in required_asset_paths(lock) if not (sim_repo / path).is_file()]


def compute_asset_content_sha(sim_repo: Path, paths: list[Path]) -> str:
    digest = hashlib.sha256()
    missing = [path for path in paths if not (sim_repo / path).is_file()]
    if missing:
        details = "\n".join(f"- {sim_repo / path}" for path in missing)
        raise StackError(f"Simulator asset files are missing.\n{details}")
    for path in paths:
        digest.update(path.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update((sim_repo / path).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def current_asset_content_sha(sim_repo: Path, lock: dict[str, Any]) -> str:
    return compute_asset_content_sha(sim_repo, required_asset_paths(lock))


def asset_state_is_current(sim_repo: Path, lock: dict[str, Any]) -> bool:
    expected = str(lock["content_sha256"])
    if not ASSET_STATE_PATH.exists() or ASSET_STATE_PATH.read_text().strip() != expected:
        return False
    if missing_asset_files(sim_repo, lock):
        return False
    try:
        return current_asset_content_sha(sim_repo, lock) == expected
    except StackError:
        return False


def write_asset_state(lock: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    ASSET_STATE_PATH.write_text(f"{lock['content_sha256']}\n")


def ensure_sim_assets(config: dict[str, object], *, allow_fetch: bool) -> None:
    sim_repo: Path = config["sim_repo"]  # type: ignore[assignment]
    lock = load_asset_lock()
    if asset_state_is_current(sim_repo, lock):
        return

    if not allow_fetch:
        missing = missing_asset_files(sim_repo, lock)
        missing_details = ""
        if missing:
            missing_details = "\nMissing files:\n" + "\n".join(f"- {path}" for path in missing)
        raise StackError(
            "Simulator asset pack is missing or stale."
            f"{missing_details}\nRun `{CLI_SIM} setup` to download and verify assets."
        )

    pull_asset_image(lock, sim_repo)
    actual = current_asset_content_sha(sim_repo, lock)
    expected = str(lock["content_sha256"])
    if actual != expected:
        raise StackError(
            f"Downloaded simulator asset pack did not match assets.lock.json.\nExpected: {expected}\nActual:   {actual}"
        )
    write_asset_state(lock)


def pull_asset_image(lock: dict[str, Any], sim_repo: Path, *, target_repo: Path | None = None) -> None:
    image_ref = str(lock["image_ref"])
    target = target_repo or sim_repo
    log(f"Pulling simulator asset pack {image_ref}...")
    run_checked(
        ["docker", "pull", image_ref],
        cwd=REPO_ROOT,
        failure_message="Pulling simulator asset pack failed.",
    )
    container_name = f"innate-sim-assets-{os.getpid()}"
    run_checked(
        ["docker", "create", "--name", container_name, image_ref, "/noop"],
        cwd=REPO_ROOT,
        failure_message="Creating simulator asset extraction container failed.",
    )
    try:
        target.mkdir(parents=True, exist_ok=True)
        run_checked(
            ["docker", "cp", f"{container_name}:{ASSET_CONTAINER_PATH}/.", str(target)],
            cwd=REPO_ROOT,
            failure_message="Extracting simulator asset pack failed.",
        )
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            cwd=REPO_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def build_asset_lock(sim_repo: Path, *, image: str = DEFAULT_ASSET_IMAGE) -> dict[str, Any]:
    paths = [Path(path) for path in DEFAULT_ASSET_FILES]
    content_sha = compute_asset_content_sha(sim_repo, paths)
    return {
        "version": 1,
        "image": image,
        "tag": f"assets-{content_sha}",
        "image_ref": f"{image}:assets-{content_sha}",
        "content_sha256": content_sha,
        "container_path": ASSET_CONTAINER_PATH,
        "required_files": [path.as_posix() for path in paths],
    }


def pack_assets(sim_repo: Path, *, image: str = DEFAULT_ASSET_IMAGE, write_lock: bool) -> dict[str, Any]:
    lock = build_asset_lock(sim_repo, image=image)
    if write_lock:
        write_asset_lock(lock)
    print(lock["image_ref"])
    print(lock["content_sha256"])
    return lock


def publish_assets(sim_repo: Path, *, image: str = DEFAULT_ASSET_IMAGE) -> dict[str, Any]:
    lock = pack_assets(sim_repo, image=image, write_lock=True)
    image_ref = str(lock["image_ref"])
    paths = required_asset_paths(lock)
    with tempfile.TemporaryDirectory(prefix="innate-sim-assets-") as tmp:
        tmp_path = Path(tmp)
        data_root = tmp_path / "sim-data"
        for path in paths:
            destination = data_root / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sim_repo / path, destination)
        (tmp_path / "Dockerfile").write_text(
            "FROM scratch\n"
            'LABEL org.opencontainers.image.source="https://github.com/innate-inc/innate-os"\n'
            'LABEL org.opencontainers.image.description="Innate OS simulator asset pack"\n'
            f"COPY sim-data {ASSET_CONTAINER_PATH}\n"
        )
        run_checked(
            [
                "docker",
                "buildx",
                "build",
                "--platform",
                "linux/amd64,linux/arm64",
                "-t",
                image_ref,
                "--push",
                ".",
            ],
            cwd=tmp_path,
            failure_message=("Building and pushing simulator asset image failed. Log in to ghcr.io and retry."),
        )
    print(f"Published {image_ref}")
    return lock


def referenced_asset_paths(sim_repo: Path) -> set[str]:
    references: set[str] = set()
    for path in sorted((sim_repo / "data" / "environments").glob("*.json")):
        collect_json_asset_references(json.loads(path.read_text()), references)

    source_paths = [
        sim_repo / "src" / "simulation" / "simulation_node.py",
    ]
    for path in source_paths:
        if not path.exists():
            continue
        for match in ASSET_PATH_PATTERN.findall(path.read_text()):
            if match.endswith(ASSET_REFERENCE_SUFFIXES):
                references.add(match)
    return references


def collect_json_asset_references(value: Any, references: set[str]) -> None:
    if isinstance(value, dict):
        for child in value.values():
            collect_json_asset_references(child, references)
        return
    if isinstance(value, list):
        for child in value:
            collect_json_asset_references(child, references)
        return
    if isinstance(value, str) and value.startswith("data/") and value.endswith(ASSET_REFERENCE_SUFFIXES):
        if value.startswith("data/assets/") or value.startswith("data/urdf/") or value == "data/replica_scene.stl":
            references.add(value)


def staged_asset_relevant_changes(repo_root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        return []
    changed = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return [
        path for path in changed if any(path == prefix or path.startswith(prefix) for prefix in ASSET_RELEVANT_PREFIXES)
    ]


def is_asset_payload_repo_path(path: str) -> bool:
    return (
        path in ASSET_PAYLOAD_EXACT_PATHS or any(path.startswith(prefix) for prefix in ASSET_PAYLOAD_PREFIXES)
    ) and path.endswith(ASSET_REFERENCE_SUFFIXES)


def staged_asset_payload_changes(repo_root: Path) -> list[str]:
    result = subprocess.run(
        [
            "git",
            "diff",
            "--cached",
            "--name-only",
            "--diff-filter=ACMR",
            "--",
            "sim/data/assets",
            "sim/data/urdf",
            "sim/data/replica_scene.stl",
        ],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [
        path.strip() for path in result.stdout.splitlines() if path.strip() and is_asset_payload_repo_path(path.strip())
    ]


def tracked_asset_payload_files(repo_root: Path) -> list[str]:
    result = subprocess.run(
        [
            "git",
            "ls-files",
            "--",
            "sim/data/assets",
            "sim/data/urdf",
            "sim/data/replica_scene.stl",
        ],
        cwd=repo_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [
        path.strip() for path in result.stdout.splitlines() if path.strip() and is_asset_payload_repo_path(path.strip())
    ]


def raise_if_payload_files_are_committed(repo_root: Path) -> None:
    paths = sorted(set(staged_asset_payload_changes(repo_root) + tracked_asset_payload_files(repo_root)))
    if not paths:
        return
    details = "\n".join(f"- {path}" for path in paths)
    raise StackError(
        "Simulator asset payload files must not be committed directly.\n"
        f"{details}\n"
        f"Publish the simulator asset pack with `{CLI_SIM} assets publish`, "
        "then commit sim/assets.lock.json and the code/config change instead."
    )


def validate_assets(
    sim_repo: Path,
    *,
    mode: str,
    staged_only: bool = False,
) -> None:
    if staged_only and not staged_asset_relevant_changes(REPO_ROOT):
        return

    raise_if_payload_files_are_committed(REPO_ROOT)

    lock = load_asset_lock()
    locked_paths = {path.as_posix() for path in required_asset_paths(lock)}
    references = referenced_asset_paths(sim_repo)
    missing_from_lock = sorted(references - locked_paths)
    if missing_from_lock:
        details = "\n".join(f"- {path}" for path in missing_from_lock)
        raise StackError(
            "Simulator code/config references assets that are not declared in sim/assets.lock.json.\n"
            f"{details}\nUpdate and publish the simulator asset pack before committing."
        )

    if mode == "ci":
        with tempfile.TemporaryDirectory(prefix="innate-sim-assets-ci-") as tmp:
            temp_sim = Path(tmp)
            pull_asset_image(lock, sim_repo, target_repo=temp_sim)
            actual = current_asset_content_sha(temp_sim, lock)
    else:
        actual = current_asset_content_sha(sim_repo, lock)

    expected = str(lock["content_sha256"])
    if actual != expected:
        raise StackError(
            "Simulator asset files do not match sim/assets.lock.json.\n"
            f"Expected: {expected}\nActual:   {actual}\n"
            f"Run `{CLI_SIM} setup` to restore the locked asset pack."
        )

    print("Simulator asset validation passed.")
