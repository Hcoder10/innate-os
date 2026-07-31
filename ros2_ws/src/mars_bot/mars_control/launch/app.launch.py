# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch_ros.actions import Node
from mars_bringup.config_loader import innate_os_root, settings_params


def generate_launch_description():
    data_directory = str(innate_os_root() / "data")
    motor_sound_config = os.path.join(get_package_share_directory("mars_control"), "config", "motor_sound.yaml")

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

    # The Mad-mode engine sound. Runs alongside mars_app because mars_app is
    # what publishes the speed mode it gates on; silent in every other mode
    # and on machines with no audio device (the node degrades gracefully).
    motor_sound_node = Node(
        package="mars_control",
        executable="motor_sound.py",
        name="motor_sound",
        parameters=[motor_sound_config, *settings_params()],
        output="screen",
    )

    return LaunchDescription(
        [
            rosbridge_node,
            app_node,
            motor_sound_node,
        ]
    )
