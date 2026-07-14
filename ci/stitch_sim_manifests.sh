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

# Every tag points at the same per-arch image pair, so stitch each image with
# ONE imagetools call carrying all tags: the index is uploaded once and the
# tag pointers are written back-to-back, keeping the window where a cancelled
# run leaves some tags stitched and others stale to near zero.
for image in "${IMAGE_PREFIX}/innate-os-sim-deps" "${IMAGE_PREFIX}/innate-os-sim-ros"; do
  tag_args=()
  seen="|"
  for tag in "${tags[@]}"; do
    if [[ "$seen" == *"|$tag|"* ]]; then
      continue
    fi
    tag_args+=(--tag "$image:$tag")
    seen="$seen$tag|"
  done

  docker buildx imagetools create \
    "${tag_args[@]}" \
    "$image:${IMAGE_TAG}-amd64" \
    "$image:${IMAGE_TAG}-arm64"
done
