# Dependencies Guide

This directory contains mode-specific apt dependency files for the Innate OS ROS2
workspace. `common` is the shared base; `sim` and `hardware` are overlays
installed on top of it.

## Files

### `apt-dependencies.common.txt`
Packages required by **both simulation and hardware modes**, plus the build/test
tooling CI relies on (colcon, vcstool, rosdep, pip). Includes core ROS2 packages,
GStreamer, and the shared message/transform/nav subset.

### `apt-dependencies.sim.txt`
**Simulation-only** overlay. C++ libs and the trimmed navigation sub-packages the
sim build needs because it omits the heavy meta-packages (`navigation2`,
`moveit`, …) that the robot installs. The robot does **not** use this file; it
gets these transitively from the hardware meta-packages.

### `apt-dependencies.hardware.txt`
**Physical-robot-only** overlay. Two groups:
- NVIDIA/Jetson packages (`nvidia-vpi-dev`, `cuda-toolkit-*`, `tensorrt`, …)
- Robot-only ROS/system packages the sim build does not need (full `navigation2`
  / `moveit` / SLAM stack, lidar/point-cloud, USB/serial/audio, on-robot tools).

## Usage

### Docker

The dev image at [sim/Dockerfile](../sim/Dockerfile) installs `common + sim`:
```bash
docker compose -f sim/docker-compose.dev.yml build
# or
docker build -t innate-os -f sim/Dockerfile .
```

The CI test base ([ci/Dockerfile.test-base](../ci/Dockerfile.test-base)) installs
`common` alone and resolves any robot-only build deps it needs via
`rosdep install --from-paths src` (they are declared in each `package.xml`).

Hardware (Jetson) builds run natively on the robot via
`scripts/update/post_update.sh`, which installs `common + hardware`.

### Manual Installation

**For Simulation (Mac/PC):**
```bash
cd ros2_ws
cat apt-dependencies.common.txt apt-dependencies.sim.txt | \
  grep -v '^#' | grep -v '^$' | \
  xargs sudo apt-get install -y
```

**For Physical Robot (Jetson):**
```bash
cd ros2_ws
cat apt-dependencies.common.txt apt-dependencies.hardware.txt | \
  grep -v '^#' | grep -v '^$' | \
  xargs sudo apt-get install -y
```

## Why Separate Files?

- **`common` stays truly common**: only what both modes need, so neither image
  carries the other's packages.
- **Smaller sim image**: simulation skips Jetson packages and the heavy robot-only
  ROS meta-packages.
- **Cross-platform**: build the sim image on Mac, Linux, or ARM without changes.
- **Clear separation**: easy to see which dependencies are mode-specific.

## Adding a Dependency

Put it in `common` if **both** sim and robot need it. Otherwise add it to the
matching overlay (`sim` or `hardware`). If a robot-only package is also a build
dependency declared in a `package.xml`, CI will still pull it via `rosdep`.
