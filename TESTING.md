# Testing protocol (innate-os)

Three gates, every one runs in CI on push/PR to `main`/`develop`. Keep it this simple.

| Gate | What | Workflow | Run locally |
|---|---|---|---|
| **1. Format** | `ruff` (Python) + `clang-format` (C++) | `format.yml` | `ruff check ros2_ws/src/` |
| **2. Build** | `colcon build` — compiles **all** packages; catches interface/import/message breaks across the whole tree | `integration-test.yml` (builds `ci/Dockerfile.test`) | see below |
| **3. Test** | unit tests (fast, no ROS) **then** integration tests (real ROS nodes) | `integration-test.yml` → `ci/docker-compose.test.yml` | see below |

Build is the broad safety net for refactors (all 19 packages). Tests are the behavioral net for the parts we've covered.

## What runs in Gate 3

**Unit (fast, pure Python, no ROS)** — `pytest`:
- `brain_client/test/test_fake_cloud_selftest.py` — the FakeCloud test double speaks the cloud protocol correctly.
- `manipulation/test/test_config_validation.py` — manipulation config validation.

**Integration (real ROS nodes)** — `colcon test`:
- `brain_client/test/test_pose_image.launch.py` — pose-image pipeline.
- `brain_client/test/test_fake_cloud_loop.launch.py` — full brain loop (real `brain_client` + `ws_client` + `skills_action_server`) against a scripted `FakeCloud`: connect → register → dispatch primitive → execute → complete → chat. (See the module docstring in that file for protocol details.)

## Run it locally

```bash
# Build the test image
docker build -t innate-os-test:latest -f ci/Dockerfile.test .

# Run all of Gate 3 exactly as CI does
INNATE_TEST_IMAGE=innate-os-test:latest \
  docker compose -f ci/docker-compose.test.yml up --abort-on-container-exit --exit-code-from integration-test
```

Just the fast unit tests, no Docker:
```bash
pytest ros2_ws/src/brain/brain_client/test/test_fake_cloud_selftest.py \
       ros2_ws/src/brain/manipulation/test/test_config_validation.py
```

## Adding a test

- **Pure-Python (no ROS):** drop a `test_*.py` next to the code, then add its path to the `pytest` line in `ci/docker-compose.test.yml`.
- **Needs real ROS nodes:** write a `*.launch.py` `launch_testing` test (copy `test_fake_cloud_loop.launch.py`), register it with `add_ros_isolated_launch_test(...)` in the package `CMakeLists.txt`, and add its name to the `-R` regex in `ci/docker-compose.test.yml`.

## Known gaps (not yet wired — honest list, not coverage)

- `maurice_bt_provisioner/test/test_command_layer.py` — needs system `gi`/GLib; verify it's in the image, then add to `--packages-select` + `-R`.
- `innate_training_node/test/test_training_node.py` — needs `rclpy`/a live node; wire as a launch test once verified.
- **Untested behaviorally:** teleop (joystick/UDP/arm), nav, camera. Same recipe — fake the seam, assert the ROS output. For these, Gates 1–2 (lint + build) are the only net today.
