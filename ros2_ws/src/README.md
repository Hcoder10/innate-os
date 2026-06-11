# ROS 2 Workspace Source

This is the source tree for the Innate OS ROS 2 workspace. Packages are grouped
into three subsystems: **`brain/`** (cloud agent + manipulation), **`cloud/`**
(cloud connectivity, logging, training), and **`maurice_bot/`** (the robot's
drivers, control, navigation, and simulation).

> Build from the workspace root: `cd ros2_ws && colcon build`.
> External dependencies are pulled via [`dependencies.repos`](dependencies.repos)
> (`vcs import src < src/dependencies.repos`).

## brain/ — Agent bridge & manipulation
| Package | Description |
| --- | --- |
| [`brain_client`](brain/brain_client) | Bridges to a cloud agent via WebSocket. |
| [`brain_messages`](brain/brain_messages) | Message, service, and action definitions for the brain system. |
| [`manipulation`](brain/manipulation) | Manipulation, behavior recording, and policy execution. |

## cloud/ — Cloud connectivity, logging & training
| Package | Description |
| --- | --- |
| [`clients`](cloud/clients) | Cloud client libraries: auth, proxy, and training. |
| [`innate_cloud_msgs`](cloud/innate_cloud_msgs) | Message/service definitions for cloud training. |
| [`innate_logger`](cloud/innate_logger) | Logs robot vitals, directives, and chat to the cloud. |
| [`innate_training_node`](cloud/innate_training_node) | Training job management, status publishing, and file transfer. |
| [`innate_uninavid`](cloud/innate_uninavid) | Bridges compressed camera images to a cloud websocket and publishes `cmd_vel` in response. |

## maurice_bot/ — Robot drivers, control, nav & sim
| Package | Description |
| --- | --- |
| [`maurice_arm`](maurice_bot/maurice_arm) | Camera driver and arm control. |
| [`maurice_bringup`](maurice_bot/maurice_bringup) | Bringup scripts and configurations for the physical robot. |
| [`maurice_bt_provisioner`](maurice_bot/maurice_bt_provisioner) | Bluetooth provisioning service. |
| [`maurice_cam`](maurice_bot/maurice_cam) | Camera driver with GStreamer, OpenCV, and WebRTC streaming. |
| [`maurice_control`](maurice_bot/maurice_control) | Control interface (joystick, keyboard, leader arm). |
| [`maurice_msgs`](maurice_bot/maurice_msgs) | Message definitions for the robot. |
| [`maurice_nav`](maurice_bot/maurice_nav) | Python-based navigation package. |
| [`maurice_sim`](maurice_bot/maurice_sim) | Robot model assets (URDF, SRDF, meshes) shared with the arm/IK stack. |
| [`maurice_sim_bringup`](maurice_bot/maurice_sim_bringup) | Starts rosbridge for simulation. |
