cmake_minimum_required(VERSION 3.10)
project(brain_client)

find_package(ament_cmake REQUIRED)
find_package(ament_cmake_python REQUIRED)
find_package(rclpy REQUIRED)
find_package(std_msgs REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(brain_messages REQUIRED)


# Install Python modules
ament_python_install_package(${PROJECT_NAME}
  PACKAGE_DIR brain_client
)

# Public authoring namespace for skill files (`from innate.skills import ...`)
ament_python_install_package(innate
  PACKAGE_DIR innate
)

# Install Python executables
install(PROGRAMS
  brain_client/nodes/brain_client_node.py
  brain_client/nodes/skills_server.py
  brain_client/nodes/input_manager.py
  DESTINATION lib/${PROJECT_NAME}
)

# Install the launch folder
install(DIRECTORY launch
  DESTINATION share/${PROJECT_NAME}
)

if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  ament_lint_auto_find_test_dependencies()
endif()

ament_package()
