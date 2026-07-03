#!/bin/bash
# In-container entry point for the virtual MARS driver: robot_state_publisher
# (static frames from the real URDF) + the driver node. Run inside the innate
# dev container (sim/docker-compose.dev.yml mounts this directory):
#   /root/innate-os/sim-mujoco/run_virtual_mars.sh
# No -u: ROS's setup.bash reads unbound variables internally.
set -eo pipefail

INNATE_OS_ROOT="${INNATE_OS_ROOT:-/root/innate-os}"
URDF="${INNATE_OS_ROOT}/ros2_ws/src/mars_bot/mars_sim/urdf/mars.urdf"

source /opt/ros/humble/setup.bash
# Workspace overlay for mars_msgs (goto_js services); topics work without it.
if [ -f "${INNATE_OS_ROOT}/ros2_ws/install/setup.bash" ]; then
  source "${INNATE_OS_ROOT}/ros2_ws/install/setup.bash"
fi
# Zenoh transport env (RMW_IMPLEMENTATION etc.) -- without it the driver
# would publish into the default-DDS graph nobody else is on.
if [ -f "${INNATE_OS_ROOT}/config/dds/setup_dds.zsh" ]; then
  source "${INNATE_OS_ROOT}/config/dds/setup_dds.zsh"
fi

# Software GL: the container has no GPU.
export MUJOCO_GL="${MUJOCO_GL:-osmesa}"

trap 'kill 0' INT TERM

# Inline -p robot_description:=<xml> breaks rcl's argument parser; a params
# file with the URDF as a block scalar is the robust way in.
RSP_PARAMS="$(mktemp /tmp/virtual_mars_rsp_XXXXXX.yaml)"
{
  echo "robot_state_publisher:"
  echo "  ros__parameters:"
  echo "    robot_description: |"
  sed 's/^/      /' "${URDF}"
} > "${RSP_PARAMS}"
ros2 run robot_state_publisher robot_state_publisher --ros-args --params-file "${RSP_PARAMS}" &

cd "$(dirname "$0")"
exec python3 virtual_mars_node.py
