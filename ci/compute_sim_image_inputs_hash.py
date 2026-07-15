#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
#
# Content hash of everything that goes into the simulator images. Prints the
# hex digest for the checkout rooted at CWD.
#
# Called by:
#   - .github/workflows/publish-sim-images.yml (tags job -> inputs-<hash> tags)
#   - .github/workflows/cleanup-sim-images.yml (pins main's inputs-<hash>
#     versions so retention can never delete the tag main users pull)
#
# The two must agree exactly -- cleanup deletes what publish names -- which is
# why this lives in one file. sim/launcher/config.py computes the same hash
# independently; keep them in sync.
import hashlib
import subprocess
from pathlib import Path

root = Path(".")
static_paths = (
    ".dockerignore",
    "sim/Dockerfile",
    "sim/Dockerfile.ros-prebuilt",
    "sim/Dockerfile.ros-prebuilt.dockerignore",
    "ros2_ws/apt-dependencies.common.txt",
    "ros2_ws/apt-dependencies.hardware.txt",
    "ros2_ws/apt-dependencies.sim.txt",
)
paths = [Path(path) for path in static_paths if (root / path).is_file()]
# Only git-tracked files, NOT a filesystem walk: rglob would also pull in
# untracked/gitignored cruft (macOS .DS_Store, gitignored dirs, build
# artifacts) that a dev's working tree has but this clean checkout does not,
# making the hash non-reproducible between a dev machine and CI. Keep this in
# sync with sim/launcher/config.py.
tracked = subprocess.run(
    ["git", "ls-files", "-z", "--", "ros2_ws/src"],
    cwd=root,
    capture_output=True,
    text=True,
    check=True,
).stdout.split("\0")
for name in tracked:
    if not name:
        continue
    relative_path = Path(name)
    if "__pycache__" in relative_path.parts or relative_path.suffix == ".pyc":
        continue
    if (root / relative_path).is_file():
        paths.append(relative_path)

digest = hashlib.sha256()
for path in sorted(paths, key=lambda item: item.as_posix()):
    digest.update(path.as_posix().encode("utf-8"))
    digest.update(b"\0")
    digest.update((root / path).read_bytes())
    digest.update(b"\0")
print(digest.hexdigest())
