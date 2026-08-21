#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
#
# Build + push the simulator images for ONE architecture. Every published
# tag is suffixed with -$ARCH; a later workflow step stitches the per-arch
# tags into multi-arch manifests under the unsuffixed names.
#
# Called by:
#   - ci/cloudbuild.sim-images.yaml         (ARCH=amd64, on Cloud Build)
#   - .github/workflows/publish-sim-images.yml (ARCH=arm64, on a native
#     ubuntu-24.04-arm runner -- QEMU emulation would turn the ros2_ws
#     colcon build into a multi-hour crawl)
#
# Required env: IMAGE_PREFIX, IMAGE_TAG, COMMIT_SHA, SHORT_SHA, ARCH
# Optional env: IMAGE_INPUTS_HASH, DEPS_INPUTS_HASH, PUSH_MAIN_TAGS
# The caller must already be logged in to the registry.
set -euo pipefail

: "${IMAGE_PREFIX:?}" "${IMAGE_TAG:?}" "${COMMIT_SHA:?}" "${SHORT_SHA:?}" "${ARCH:?}"
export DOCKER_BUILDKIT=1

deps_image="${IMAGE_PREFIX}/innate-os-sim-deps"
ros_image="${IMAGE_PREFIX}/innate-os-sim-ros"
sha_tag="sha-${SHORT_SHA}"
cache_tag="buildcache-${ARCH}"
inputs_tag=""
if [[ -n "${IMAGE_INPUTS_HASH:-}" && "${IMAGE_INPUTS_HASH}" != "manual" ]]; then
  inputs_tag="inputs-${IMAGE_INPUTS_HASH}"
fi
# The deps image's own content address (config.resolve_deps_image): the files
# sim/Dockerfile reads, not ros2_ws/src, which it does not contain. This is
# what the launcher pulls when no ROS prebuilt matches a checkout.
deps_inputs_tag=""
if [[ -n "${DEPS_INPUTS_HASH:-}" && "${DEPS_INPUTS_HASH}" != "manual" ]]; then
  deps_inputs_tag="deps-${DEPS_INPUTS_HASH}"
fi

push_tags() {
  local image="$1"
  local source_tag="$2"
  shift 2

  local pushed=""
  for tag in "$@"; do
    if [[ -z "$tag" || "$pushed" == *"|$tag|"* ]]; then
      continue
    fi

    if [[ "$tag" != "$source_tag" ]]; then
      docker tag "$image:$source_tag" "$image:$tag"
    fi

    docker push "$image:$tag"
    pushed="$pushed|$tag|"
  done
}

# Reuse rather than rebuild when this arch already has one for these inputs: a
# rebuild that misses its cache re-resolves apt and pip into layers that are
# byte-different for identical content, and each difference is a
# multi-hundred-MB download for every user downstream.
deps_reused=false
if [[ -n "$deps_inputs_tag" ]] && docker pull "$deps_image:${deps_inputs_tag}-${ARCH}"; then
  docker tag "$deps_image:${deps_inputs_tag}-${ARCH}" "$deps_image:${IMAGE_TAG}-${ARCH}"
  deps_reused=true
  echo "Reusing published deps image $deps_image:${deps_inputs_tag}-${ARCH}"
else
  # The plain `buildcache` pulls seed the cache from the pre-multi-arch tags
  # during the transition; harmless no-ops once the -$ARCH caches exist.
  docker pull "$deps_image:$cache_tag" || docker pull "$deps_image:buildcache" || true
  docker build \
    --progress=plain \
    --build-arg BUILDKIT_INLINE_CACHE=1 \
    --cache-from "$deps_image:$cache_tag" \
    --cache-from "$deps_image:buildcache" \
    --label "org.opencontainers.image.source=https://github.com/innate-inc/innate-os" \
    --label "org.opencontainers.image.revision=${COMMIT_SHA}" \
    --tag "$deps_image:${IMAGE_TAG}-${ARCH}" \
    --file sim/Dockerfile \
    .
fi

deps_tags=("${IMAGE_TAG}-${ARCH}" "${sha_tag}-${ARCH}" "$cache_tag")
if [[ -n "$inputs_tag" ]]; then
  deps_tags+=("${inputs_tag}-${ARCH}")
fi
if [[ -n "$deps_inputs_tag" ]]; then
  deps_tags+=("${deps_inputs_tag}-${ARCH}")
fi
if [[ "${PUSH_MAIN_TAGS:-false}" == "true" ]]; then
  deps_tags+=("main-${ARCH}")
fi
push_tags "$deps_image" "${IMAGE_TAG}-${ARCH}" "${deps_tags[@]}"

# By digest, never the mutable buildcache tag it used to be: `docker push`
# records the pushed manifest as a RepoDigest, so this is the exact image just
# published. `|| true` because pipefail would make a no-match grep abort here,
# and the explicit check below is the better error.
deps_ros_base="$(
  docker inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$deps_image:${IMAGE_TAG}-${ARCH}" |
    grep "^${deps_image}@sha256:" | head -n1 || true
)"
if [[ -z "$deps_ros_base" ]]; then
  echo "No RepoDigest for $deps_image:${IMAGE_TAG}-${ARCH} after push -- refusing to fall back to a mutable tag." >&2
  exit 1
fi
echo "ROS image base: $deps_ros_base (reused=${deps_reused})"
docker pull "$ros_image:$cache_tag" || docker pull "$ros_image:buildcache" || true
docker build \
  --progress=plain \
  --build-arg "BASE_IMAGE=$deps_ros_base" \
  --build-arg BUILDKIT_INLINE_CACHE=1 \
  --cache-from "$ros_image:$cache_tag" \
  --cache-from "$ros_image:buildcache" \
  --label "org.opencontainers.image.source=https://github.com/innate-inc/innate-os" \
  --label "org.opencontainers.image.revision=${COMMIT_SHA}" \
  --tag "$ros_image:${IMAGE_TAG}-${ARCH}" \
  --file sim/Dockerfile.ros-prebuilt \
  .

ros_tags=("${IMAGE_TAG}-${ARCH}" "${sha_tag}-${ARCH}" "$cache_tag")
if [[ -n "$inputs_tag" ]]; then
  ros_tags+=("${inputs_tag}-${ARCH}")
fi
if [[ "${PUSH_MAIN_TAGS:-false}" == "true" ]]; then
  ros_tags+=("main-${ARCH}")
fi
push_tags "$ros_image" "${IMAGE_TAG}-${ARCH}" "${ros_tags[@]}"
