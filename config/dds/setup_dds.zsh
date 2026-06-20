#!/bin/zsh
# Shared ROS runtime environment, sourced by every node (via the launch script) and
# by interactive shells (.zshrc): DDS/Zenoh transport config, plus the console-logging
# format the observability tooling depends on (see the RCUTILS export at the bottom).

export RMW_IMPLEMENTATION=rmw_zenoh_cpp
export ROS_DOMAIN_ID=0
export ZENOH_ROUTER_CHECK_ATTEMPTS=0 # wait forever for router start

# rmw_zenoh_cpp allocates one SHM segment per process of ZENOH_SHM_ALLOC_SIZE bytes
# (default 48 MiB). With ~40 processes that's ~1.9 GB locked in /dev/shm.
# 4 MiB is sufficient: largest single message is a 320x240 point cloud (~2.4 MB).
# If a message exceeds the SHM segment, Zenoh falls back to TCP transparently.
# TODO: once nodes are individually launchable, set per-process:
#   camera_container=16M, nav2 nodes=4M, lightweight nodes=2M.
export ZENOH_SHM_ALLOC_SIZE=4194304

export ZENOH_SESSION_CONFIG_OVERRIDE='transport/shared_memory/enabled=true;transport/shared_memory/transport_optimization/pool_size=4194304;transport/link/tx/queue/congestion_control/drop/wait_before_drop=900000;transport/link/tx/queue/congestion_control/drop/max_wait_before_drop_fragments=900000'
export ZENOH_ROUTER_CONFIG_OVERRIDE='transport/shared_memory/enabled=false;transport/shared_memory/transport_optimization/pool_size=4194304;transport/link/tx/queue/congestion_control/drop/wait_before_drop=900000;transport/link/tx/queue/congestion_control/drop/max_wait_before_drop_fragments=900000'
export ZENOH_CONFIG_OVERRIDE="$ZENOH_SESSION_CONFIG_OVERRIDE"
# export RUST_LOG=debug

# Standardize every node's console output so each line is self-describing:
# severity, timestamp, and logger/node name. This is the contract the
# observability tooling relies on to attribute logs by node (see
# innate_console bridge + the webapp Debugging page). It matches rcl's default
# layout; pinning it explicitly guards against drift and per-node overrides.
export RCUTILS_CONSOLE_OUTPUT_FORMAT='[{severity}] [{time}] [{name}]: {message}'
