# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from launch import LaunchDescription
from launch_ros.actions import Node
from mars_bringup.config_loader import innate_os_root, settings_params


def generate_launch_description():
    data_directory = str(innate_os_root() / "data")

    default_hardware_revision = "R6"

    rosbridge_node = Node(
        package="rws",
        executable="rws_server",
        name="ros_websocket_server",
        parameters=[{}],
        # Silence the per-message "Field 'z' is not in json, default: (nil)" INFO spam
        # from the JSON translator while keeping connect/disconnect logs visible.
        arguments=["--ros-args", "--log-level", "rws::translate:=WARN"],
        output="screen",
        respawn=True,
        respawn_delay=2.0,
        # Disable Zenoh SHM for rws_server to prevent __pthread_tpp_change_priority
        # crash from priority-inheritance mutex contention between websocketpp threads
        # and Zenoh SHM watchdog threads. rws_server is a JSON bridge — no SHM needed.
        additional_env={
            "ZENOH_SESSION_CONFIG_OVERRIDE": (
                "transport/shared_memory/enabled=false"
                ";transport/link/tx/queue/congestion_control/drop/wait_before_drop=900000"
                ";transport/link/tx/queue/congestion_control/drop/max_wait_before_drop_fragments=900000"
            ),
        },
    )

    app_node = Node(
        package="mars_control",
        executable="app.cpp",
        name="mars_app",
        output="screen",
        parameters=[
            {
                "data_directory": data_directory,
                "default_hardware_revision": default_hardware_revision,
            },
            *settings_params(),
        ],
    )

    return LaunchDescription(
        [
            rosbridge_node,
            app_node,
        ]
    )
