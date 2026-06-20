from launch import LaunchDescription
from launch_ros.actions import Node
from mars_bringup.config_loader import innate_os_root, settings_params


def generate_launch_description():
    data_directory = str(innate_os_root() / "data")

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
            },
            *settings_params(),
        ],
    )

    return LaunchDescription([app_node])
