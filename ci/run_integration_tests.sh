#!/usr/bin/env bash
# Build innate-os and run its test suite inside the test image.
#
# Single source of truth for "the tests", invoked by both:
#   - ci/docker-compose.test.yml         (local / self-hosted)
#   - ci/cloudbuild.integration-test.yaml (Cloud Build)
#
# The image entrypoint has already sourced ROS 2 and set the Zenoh/RMW env.
# We disable Zenoh shared-memory transport below (see the export block): the SHM
# provider rmw_zenoh allocates at startup cannot be created in sandboxed CI such
# as Cloud Build's gVisor, which blocks POSIX shm regardless of /dev/shm size.
# These are correctness tests (orchestration logic, not transport), so loopback
# is equivalent; the robot keeps SHM in production via the image entrypoint.
set -e

ROUTER_PID=""
cleanup() {
  if [ -n "${ROUTER_PID}" ]; then
    kill "${ROUTER_PID}" >/dev/null 2>&1 || true
    wait "${ROUTER_PID}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

run_ros_integration_tests() {
  # Launch-based tests: these actually start nodes (e.g. brain_client_node.py),
  # so a node whose installed executable is missing or non-runnable fails here.
  ros2 run rmw_zenoh_cpp rmw_zenohd &
  ROUTER_PID=$!
  sleep 2
  colcon test --packages-select brain_client \
    --ctest-args -R "test_pose_image|test_fake_cloud_loop" \
    --event-handlers console_direct+
  colcon test-result --verbose
  kill "${ROUTER_PID}" >/dev/null 2>&1 || true
  wait "${ROUTER_PID}" >/dev/null 2>&1 || true
  ROUTER_PID=""
}

echo "=== /dev/shm ==="
df -h /dev/shm || true

# Disable Zenoh shared memory for the test run. The image entrypoint enables it
# (ZENOH_*_CONFIG_OVERRIDE), so override all three to false here. Without this,
# rmw_zenoh tries to create a POSIX SHM provider at rclpy.init, which fails under
# Cloud Build's gVisor sandbox ("Failed to create POSIX SHM provider").
export ZENOH_SESSION_CONFIG_OVERRIDE="transport/shared_memory/enabled=false"
export ZENOH_ROUTER_CONFIG_OVERRIDE="transport/shared_memory/enabled=false"
export ZENOH_CONFIG_OVERRIDE="transport/shared_memory/enabled=false"

# The test image already built the workspace at image-build time
# (ci/Dockerfile.test does an incremental colcon build), so just source it.
cd /root/innate-os/ros2_ws
source install/setup.bash

echo "=== unit tests (fast, no ROS) ==="
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q \
  src/brain/brain_client/test/test_fake_cloud_selftest.py \
  src/brain/brain_client/test/test_backwards_compat.py \
  src/brain/manipulation/test/test_config_validation.py

# Run the launch tests under both install modes. The copy build baked into the
# image (ci/Dockerfile.test) masks missing exec bits by installing 0755 copies;
# the --symlink-install build (what the local sim launcher uses) symlinks the
# install executable straight at the source, so a node script that lost its exec
# bit fails to launch. Running both catches bugs specific to either mode.
echo "=== integration tests: regular (copy) install ==="
run_ros_integration_tests

echo "=== integration tests: --symlink-install ==="
rm -rf build install log
colcon build --symlink-install \
  --parallel-workers "$(( $(nproc) < 4 ? $(nproc) : 4 ))" --cmake-args \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_COMPILER_LAUNCHER=ccache \
  -DCMAKE_CXX_COMPILER_LAUNCHER=ccache
source install/setup.bash
run_ros_integration_tests
