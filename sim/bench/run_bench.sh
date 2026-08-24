#!/usr/bin/env bash
set -uo pipefail
cd "$HOME/innate-os"
export MUJOCO_GL="${MUJOCO_GL:-osmesa}"
export PYTHONPATH="$PWD/ros2_ws/src/mars_bot/mars_sim_driver${PYTHONPATH:+:$PYTHONPATH}"
exec ./sim/.venv/bin/python sim/bench/main.py "$@"
