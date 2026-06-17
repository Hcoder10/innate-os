import os

from ament_index_python.packages import get_package_share_directory
from innate_config.launch import apply_overrides
from launch import LaunchDescription
from launch_ros.actions import Node


def generate_launch_description():
    # Get the package share directory
    pkg_dir = get_package_share_directory("mars_control")

    # Path to the config file
    config_file = os.path.join(pkg_dir, "config", "motion_control.yaml")

    # Create the joystick node
    joystick_node = Node(
        package="mars_control",
        executable="joystick.py",
        name="joystick_controller",
        parameters=apply_overrides([config_file]),
        output="screen",
    )

    return LaunchDescription([joystick_node])
