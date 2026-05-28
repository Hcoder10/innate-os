# Dependencies Guide

This directory contains mode-specific dependency files for the Innate OS ROS2 workspace.

## Files

### `apt-dependencies.common.txt`
Common system dependencies required for **both simulation and hardware modes**.
This includes ROS2 packages, GStreamer, Python packages, and navigation/manipulation tools.

### `apt-dependencies.hardware.txt`
Hardware-specific dependencies **only for physical robots** (NVIDIA Jetson).
This includes:
- `nvidia-vpi-dev` - NVIDIA Vision Programming Interface for stereo depth
- `nvidia-l4t-gstreamer` - NVIDIA hardware-accelerated GStreamer plugins

### `apt-dependencies.txt` (Deprecated)
Legacy file kept for backwards compatibility. Points to the new mode-specific files.

## Usage

### Docker

The dev Dockerfile at [sim/Dockerfile](../sim/Dockerfile) builds a simulation-only image using [sim/apt-dependencies.txt](../sim/apt-dependencies.txt):
```bash
docker compose -f sim/docker-compose.dev.yml build
# or
docker build -t innate-os -f sim/Dockerfile .
```

Hardware (Jetson) builds run natively on the robot via `scripts/update/post_update.sh`, not through Docker.

### Manual Installation

**For Simulation (Mac/PC):**
```bash
cd ros2_ws
xargs sudo apt-get install -y < apt-dependencies.common.txt
```

**For Physical Robot (Jetson):**
```bash
cd ros2_ws
cat apt-dependencies.common.txt apt-dependencies.hardware.txt | \
  grep -v '^#' | grep -v '^$' | \
  xargs sudo apt-get install -y
```

Or install separately:
```bash
xargs sudo apt-get install -y < apt-dependencies.common.txt
xargs sudo apt-get install -y < apt-dependencies.hardware.txt
```

## Why Separate Files?

- **Cleaner builds**: Simulation environments don't need Jetson-specific packages
- **Cross-platform support**: Build on Mac, Linux, or ARM without modification
- **Faster iterations**: Skip unnecessary hardware packages in development
- **Clear separation**: Easy to see which dependencies are platform-specific

## Migration Notes

If you have scripts or documentation referencing `apt-dependencies.txt`, they will continue to work but should be updated to use the mode-specific files for better clarity.
