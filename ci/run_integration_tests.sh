#!/usr/bin/env bash
# Build innate-os and run its test suite inside the test image.
#
# Single source of truth for "the tests", invoked by both:
#   - ci/docker-compose.test.yml         (local / self-hosted)
#   - ci/cloudbuild.integration-test.yaml (Cloud Build)
#
# The image entrypoint has already sourced ROS 2 and set the Zenoh/RMW env.
# Zenoh SHM stays enabled (production parity); callers must give the container a
# large enough /dev/shm (rmw_zenoh pre-allocates ~48 MiB per node) — e.g.
# `docker run --shm-size=2g` or compose `shm_size: "2g"`.
set -e

ROUTER_PID=""
cleanup() {
  if [ -n "${ROUTER_PID}" ]; then
    kill "${ROUTER_PID}" >/dev/null 2>&1 || true
    wait "${ROUTER_PID}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

echo "=== /dev/shm (rmw_zenoh needs room here; ~48 MiB per node) ==="
df -h /dev/shm || true

cd /root/innate-os/ros2_ws
colcon build
source install/setup.bash

echo "=== unit tests (fast, no ROS) ==="
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q \
  src/brain/brain_client/test/test_fake_cloud_selftest.py \
  src/brain/manipulation/test/test_config_validation.py

echo "=== integration tests (ROS launch) ==="
ros2 run rmw_zenoh_cpp rmw_zenohd &
ROUTER_PID=$!
sleep 2
colcon test --packages-select brain_client \
  --ctest-args -R "test_pose_image|test_fake_cloud_loop" \
  --event-handlers console_direct+
colcon test-result --verbose
