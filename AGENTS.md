# Innate OS — Agent Reference

## What is Innate?

Innate OS is a lightweight, agentic operating system for the **MARS** — a small mobile robot with an arm.
It runs on an **NVIDIA Jetson Orin Nano 8GB** (resource-constrained by design) and boots the full stack in under a minute.

The OS is built on **ROS 2 (Humble)** with **Zenoh** as the DDS transport, and natively supports agentic workflows, vision-language navigation (VLN), and vision-language-action (VLA) models via a cloud brain.

For full documentation and hardware details, see [innate.bot](https://www.innate.bot/) and [docs.innate.bot](https://docs.innate.bot).

## System Modes

| Mode | Description |
|---|---|
| `hardware` | Running on the physical MARS robot |
| `sim` | Running in the software simulator (same ROS nodes, mock sensors) |

## CLI — `innate`

Run `innate` with no arguments to print the current system status (version, mode, ROS/DDS state).

| Command | Description |
|---|---|
| `innate view` | Attach to running nodes |
| `innate restart` | Restart ROS nodes |
| `innate build` | Build and restart |
| `innate build release` | Build release mode and restart |
| `innate skill list` | List available robot skills |
| `innate update` | Check and apply updates |
| `innate diag` | Check hardware diagnostics |
| `innate volume` | Get or set speaker volume |
| `innate --help` | Show all commands |

## Key ROS Packages

| Package | Role |
|---|---|
| `maurice_control` | Top-level app node, rosbridge websocket, teleop receiver |
| `maurice_bringup` | Motor/IMU/LiDAR hardware drivers, TF tree |
| `maurice_arm` | Arm + head servos, MoveIt, IK solver |
| `maurice_cam` | Stereo cameras, depth estimation, WebRTC stream |
| `maurice_nav` | Nav2 navigation, SLAM, mode manager |
| `brain_client` | Cloud brain bridge (STT/TTS, skills action server) |
| `manipulation` | Records/replays and runs manipulation policies |
