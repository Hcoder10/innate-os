import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch_ros.actions import Node


def generate_launch_description():
    # Get package directory
    mars_arm_dir = get_package_share_directory("mars_arm")

    # Get the path to the config file
    config_file = os.path.join(mars_arm_dir, "config", "arm_config.yaml")

    # Path to planning launch file
    planning_launch_file = os.path.join(mars_arm_dir, "launch", "planning.launch.py")

    # Create the arm node (C++ - includes arm + head servo 7 + MoveIt planning client)
    mars_arm_node = Node(
        package="mars_arm", executable="arm", name="mars_arm", parameters=[config_file], output="screen"
    )

    # Include the planning launch file (MoveIt move_group)
    planning_launch = IncludeLaunchDescription(PythonLaunchDescriptionSource(planning_launch_file))

    return LaunchDescription([mars_arm_node, planning_launch])
