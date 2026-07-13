#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
#
# Stitch the per-arch simulator image tags (pushed as -amd64 / -arm64 by
# ci/build_sim_images.sh) into multi-arch manifests under the unsuffixed names.
#
# Called by:
#   - .github/workflows/publish-sim-images.yml (manifest job)
#
# Required env: IMAGE_PREFIX, IMAGE_TAG, SHORT_SHA
# Optional env: IMAGE_INPUTS_HASH, PUSH_MAIN_TAGS
# The caller must already be logged in to the registry.
set -euo pipefail

: "${IMAGE_PREFIX:?}" "${IMAGE_TAG:?}" "${SHORT_SHA:?}"

tags=("${IMAGE_TAG}" "sha-${SHORT_SHA}")
if [[ -n "${IMAGE_INPUTS_HASH:-}" && "${IMAGE_INPUTS_HASH}" != "manual" ]]; then
  tags+=("inputs-${IMAGE_INPUTS_HASH}")
fi
if [[ "${PUSH_MAIN_TAGS:-false}" == "true" ]]; then
  tags+=("main")
fi

for image in "${IMAGE_PREFIX}/innate-os-sim-deps" "${IMAGE_PREFIX}/innate-os-sim-ros"; do
  for tag in "${tags[@]}"; do
    docker buildx imagetools create \
      --tag "$image:$tag" \
      "$image:$tag-amd64" \
      "$image:$tag-arm64"
  done
done
