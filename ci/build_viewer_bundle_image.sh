#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
#
# Build and push the SimSession bundle image (sim/viewer/Dockerfile).
#
# The launcher builds the SAME Dockerfile when the working tree is dirty
# (runtime._build_viewer_image_locally), so nobody needs Node.js on the host to
# run the sim. This script adds only what publishing needs: both arches, the
# registry cache, the tags, and the anonymous-pull check.
#
# The tag IS the address: every input is on disk under sim/viewer, so a content
# hash over that tree names the payload exactly.
#
# Env:
#   IMAGE_PREFIX   ghcr.io/innate-inc
#   VIEWER_HASH    from `config.py viewer-image-hash`; defaults to computing it
#   IMAGE_TAG      sha-<short12>; defaults to HEAD's
#   PUSH_MAIN_TAGS "true" on main
set -euo pipefail

IMAGE_PREFIX="${IMAGE_PREFIX:-ghcr.io/innate-inc}"
VIEWER_HASH="${VIEWER_HASH:-$(python3 sim/launcher/config.py viewer-image-hash)}"
IMAGE_TAG="${IMAGE_TAG:-sha-$(git rev-parse HEAD | cut -c1-12)}"
viewer_image="${IMAGE_PREFIX}/innate-os-sim-viewer"
inputs_tag="inputs-${VIEWER_HASH}"

# inputs- is the address the launcher resolves. sha- exists purely for
# retention: ci/sim_image_retention.jq prefix-matches sha- tag components
# against live branch heads to give head-of-branch builds a longer window, and
# a version with no sha- tag at all falls into the short bucket.
tags=(--tag "${viewer_image}:${inputs_tag}" --tag "${viewer_image}:${IMAGE_TAG}")
if [ "${PUSH_MAIN_TAGS:-false}" = "true" ]; then
  tags+=(--tag "${viewer_image}:main")
fi

# Same shape as the asset image, for the same reasons: multi-arch in one run
# because the payload is static data, attestations off because they push extra
# unknown/unknown manifests the pull paths would have to tolerate.
echo "=== building ${viewer_image}:${inputs_tag} ==="
DOCKER_BUILDKIT=1 docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --file sim/viewer/Dockerfile \
  --provenance=false --sbom=false \
  --cache-from "type=registry,ref=${viewer_image}:buildcache" \
  --cache-to "type=registry,ref=${viewer_image}:buildcache,mode=min" \
  --label "org.opencontainers.image.source=https://github.com/innate-inc/innate-os" \
  "${tags[@]}" \
  --push \
  .

# GHCR makes a brand-new package PRIVATE, so compose would fail to pull for
# every user while CI stayed green. The asset image gets this check for free
# from ci/verify_assets_image.py; this one has to spell it out.
echo "=== verifying anonymous pullability ==="
python3 - "${viewer_image}" "${inputs_tag}" <<'PY'
import sys

# Run from the repo root, like every other ci/ script.
sys.path.insert(0, "sim/launcher")
import oci

image, tag = sys.argv[1], sys.argv[2]
try:
    manifest = oci.manifest_for_image(f"{image}:{tag}")
except oci.OciError as exc:
    package = oci.repo_path(image).split("/")[-1]
    sys.exit(
        f"{image}:{tag} is not anonymously pullable: {exc}\n"
        "If this is the first publish, the GHCR package defaults to private: open\n"
        f"  https://github.com/orgs/innate-inc/packages/container/{package}/settings\n"
        "and change the visibility to public."
    )

layers = manifest.get("layers") or []
if len(layers) != 1:
    sys.exit(f"expected exactly one layer (the /bundle subtree), got {len(layers)}")
print(f"{image}:{tag} -> 1 layer, {layers[0]['size']} bytes, anonymously pullable")
PY

echo "=== published ==="
echo "${viewer_image}:${inputs_tag}"
