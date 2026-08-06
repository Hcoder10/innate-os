#!/bin/zsh
# Launch the arm SDK playground backend against the live robot stack.
# The UI is the webapp's /armsdk page (proxied through the front door).
HERE="$(cd "$(dirname "$0")" && pwd)"
source /opt/ros/humble/setup.zsh
source "$HERE/../../ros2_ws/install/setup.zsh"
source "$HERE/../../config/dds/setup_dds.zsh"
exec python3 "$HERE/server.py"
