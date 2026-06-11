cmake_minimum_required(VERSION 3.8)
project(mars_sim)

find_package(ament_cmake REQUIRED)

# Install robot model assets (URDF, SRDF, meshes)
install(
  DIRECTORY urdf meshes
  DESTINATION share/${PROJECT_NAME}
)

ament_package()
