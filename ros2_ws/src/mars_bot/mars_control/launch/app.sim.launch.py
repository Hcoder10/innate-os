import os

from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    # Use environment variable if set, otherwise construct from HOME
    mars_root = os.environ.get("INNATE_OS_ROOT", os.path.join(os.path.expanduser("~"), "innate-os"))
    data_directory = os.path.join(mars_root, "data")

    # Default hardware revision for new robots
    default_hardware_revision = "R6"

    app_node = Node(
        package="mars_control",
        executable="app.cpp",
        name="mars_app",
        output="screen",
        parameters=[
            {
                "data_directory": data_directory,
                "default_hardware_revision": default_hardware_revision,
            }
        ],
    )

    return LaunchDescription([app_node])
