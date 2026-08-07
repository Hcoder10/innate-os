# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""What the viewer bundle image's address must and must not react to.

The bundle is its own image so a ~1 MB artifact changing on every viewer commit
does not rename a ~156 MB one that costs hours to build. Two directions to
defend: the asset image must not react to bundle sources, and every file in
sim/viewer must trigger a bundle publish (its tag is a content hash of that
tree).
"""

import sys
from pathlib import Path

import pytest
from workflow_paths import covered, push_path_globs

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "publish-viewer-bundle.yml"
ASSETS_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "publish-sim-images.yml"
DOCKERFILE = REPO_ROOT / "sim" / "viewer" / "Dockerfile"
DOCKERIGNORE = REPO_ROOT / "sim" / "viewer" / "Dockerfile.dockerignore"

sys.path.insert(0, str(REPO_ROOT / "sim" / "launcher"))


def test_one_hash_names_both_the_published_and_local_bundle():
    """One hashing scheme for both images -- repo name is the only difference,
    and the names stay distinct so a local build cannot shadow the registry's
    copy in docker_image_present.
    """
    import config

    digest = config.compute_viewer_inputs_hash(REPO_ROOT)
    assert config.resolve_viewer_image(REPO_ROOT) == f"{config.DEFAULT_SIM_VIEWER_IMAGE}:inputs-{digest}"
    assert config.resolve_local_viewer_image(REPO_ROOT) == f"{config.LOCAL_VIEWER_IMAGE_REPO}:inputs-{digest}"
    assert config.DEFAULT_SIM_VIEWER_IMAGE.rsplit("/", 1)[-1] != config.LOCAL_VIEWER_IMAGE_REPO


def test_asset_image_no_longer_reacts_to_viewer_sources():
    """If bundle sources re-enter the asset image's input set, every viewer
    edit renames the asset image again and `up` 404s on an unpublished tag.
    """
    import config

    hashed = {path.as_posix() for path in config.iter_assets_image_input_files(REPO_ROOT)}
    leaked = sorted(
        path
        for path in hashed
        if path.startswith("sim/viewer/src/") or path in {"sim/viewer/tsconfig.json", "sim/viewer/vite.lib.config.ts"}
    )
    assert not leaked, (
        "these bundle-image inputs feed the ASSET image's content hash again, so editing one "
        "renames the asset image and `up` asks GHCR for a tag CI never published:\n  " + "\n  ".join(leaked)
    )


@pytest.mark.skipif(not WORKFLOW.is_file(), reason="viewer publish workflow not present")
def test_every_file_in_the_tree_triggers_a_publish():
    """The tag covers the whole tree, so the trigger must too; a miss makes
    every clean checkout pay a local build a publish would have saved it.
    """
    import config

    globs = push_path_globs(WORKFLOW)
    # The launcher's own enumeration: assert against the same file set that
    # feeds the tag (minus untracked files, which a push cannot carry anyway).
    tracked = [path.as_posix() for path in config.git_tracked_files(REPO_ROOT, "sim/viewer")]
    assert tracked, "no tracked files under sim/viewer -- the collector is broken"

    missing = sorted(path for path in tracked if not covered(path, globs))
    assert not missing, (
        "these files are inside the sim/viewer tree (so they move the bundle's tag) but no "
        "`paths:` entry in publish-viewer-bundle.yml matches them, so editing one renames the "
        "image without publishing it:\n  " + "\n  ".join(missing)
    )


@pytest.mark.skipif(not WORKFLOW.is_file(), reason="viewer publish workflow not present")
def test_the_module_that_computes_the_hash_triggers_a_publish():
    """The tree is covered above; its hash FUNCTION lives outside the tree.

    compute_viewer_inputs_hash decides both membership and framing, so an edit
    there renames the bundle with nothing under sim/viewer touched -- and every
    clean checkout then builds it locally.
    """
    import config

    owner = Path(config.__file__).resolve().relative_to(REPO_ROOT).as_posix()
    assert covered(owner, push_path_globs(WORKFLOW)), (
        f"{owner} computes the bundle's content hash but no `paths:` entry in {WORKFLOW.name} "
        f"matches it, so editing it renames the image without publishing it."
    )


@pytest.mark.skipif(not DOCKERIGNORE.is_file(), reason="viewer dockerignore not present")
def test_the_build_context_stays_inside_the_tree():
    """A file admitted from outside sim/viewer would change the image's
    contents without moving its address -- two payloads under one content
    address, the one thing a content address must never do.
    """
    admitted = [line[1:] for line in DOCKERIGNORE.read_text().splitlines() if line.startswith("!")]
    assert admitted, "the dockerignore admits nothing; the build context would be empty"
    outside = sorted(rule for rule in admitted if not rule.startswith("sim/viewer/"))
    assert not outside, (
        "these dockerignore rules admit files from outside sim/viewer, whose content hash is "
        "the image's entire address:\n  " + "\n  ".join(outside)
    )


@pytest.mark.skipif(not DOCKERFILE.is_file(), reason="viewer Dockerfile not present")
def test_the_dockerfile_lives_inside_the_tree_it_is_addressed_by():
    """A Dockerfile outside the hashed tree could change what gets built
    without moving the address it is built under.
    """
    assert DOCKERFILE.parent == REPO_ROOT / "sim" / "viewer"
    assert DOCKERIGNORE.parent == REPO_ROOT / "sim" / "viewer"


@pytest.mark.skipif(not DOCKERIGNORE.is_file(), reason="viewer dockerignore not present")
def test_the_local_build_hash_covers_everything_the_build_reads():
    """A file the build COPYs but the hash omits would mount a stale bundle
    with nothing to suggest why. The dockerignore is the authority on what the
    build sees; git supplies the hashed set, so this checks files on disk.
    """
    import config

    hashed = set(config._viewer_input_files(REPO_ROOT))
    missing = []
    for line in DOCKERIGNORE.read_text().splitlines():
        if not line.startswith("!"):
            continue
        pattern = line[1:].removeprefix("sim/viewer/")
        base = REPO_ROOT / "sim" / "viewer"
        found = base.glob(pattern.replace("/**", "/**/*")) if "**" in pattern else [base / pattern]
        for path in found:
            if path.is_file() and path.relative_to(REPO_ROOT) not in hashed:
                missing.append(str(path.relative_to(REPO_ROOT)))
    assert not missing, (
        "these reach the bundle build but do not feed its tag, so editing one "
        "would reuse the previously built image:\n  " + "\n  ".join(sorted(missing))
    )


@pytest.mark.skipif(
    not (DOCKERFILE.is_file() and ASSETS_WORKFLOW.is_file()),
    reason="viewer/asset tooling not present",
)
def test_the_asset_workflow_does_not_rebuild_on_viewer_sources():
    """Cadence, not just identity: a `paths:` entry for sim/viewer/src would
    drag the multi-arch asset pipeline through every viewer commit. The asset
    image legitimately keeps sim/viewer/package*.json and sim/viewer/tools, so
    this checks the bundle-only sources, not the whole directory.
    """
    globs = push_path_globs(ASSETS_WORKFLOW)
    moved = ["sim/viewer/src/simSession.ts", "sim/viewer/tsconfig.json", "sim/viewer/vite.lib.config.ts"]
    still_listed = sorted(path for path in moved if covered(path, globs))
    assert not still_listed, (
        "publish-sim-images.yml still rebuilds the asset image for these bundle-only sources:\n  "
        + "\n  ".join(still_listed)
    )
