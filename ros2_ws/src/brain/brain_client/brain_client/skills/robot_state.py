"""Robot-state provision for skill execution.

Owns the on-demand state subscriptions (odom / map / head) and the camera + robot
interface references, and injects required interfaces and live robot state into the
running skill — including the 50 Hz continuous-update thread. Pulled out of the
skills action server so the node is left with just the action/execution flow.
"""

from __future__ import annotations

import base64
import json
import math
import threading

import numpy as np
from nav_msgs.msg import OccupancyGrid, Odometry
from std_msgs.msg import String

from brain_client.common.geometry import quaternion_to_yaw
from brain_client.skills.types import InterfaceType, RobotStateType


class RobotStateProvider:
    def __init__(self, node, camera_node, *, manipulation, mobility, head, head_current_position_topic: str):
        self._node = node
        self._logger = node.get_logger()
        self._camera = camera_node
        self._manipulation = manipulation
        self._mobility = mobility
        self._head = head
        self._head_current_position_topic = head_current_position_topic

        self.last_odom = None
        self.last_map = None
        self.last_head_position = None

        self._odom_sub = None
        self._map_sub = None
        self._head_position_sub = None

        self._current_skill = None
        self._current_skill_lock = threading.Lock()
        self._state_update_thread = None
        self._state_update_stop_event = threading.Event()

    # --- interface injection (at skill load + before execution) ---
    def inject_required_interfaces(self, skill) -> None:
        """Inject only the interfaces declared by the skill via Interface descriptors."""
        for interface_type in skill.get_required_interfaces():
            if interface_type == InterfaceType.MANIPULATION:
                skill.inject_interface(interface_type, self._manipulation)
            elif interface_type == InterfaceType.MOBILITY:
                skill.inject_interface(interface_type, self._mobility)
            elif interface_type == InterfaceType.HEAD:
                skill.inject_interface(interface_type, self._head)

    # --- subscriptions ---
    def start_subscriptions(self) -> None:
        """Create robot-state subscriptions needed during skill execution."""
        if self._odom_sub is not None:
            return
        self._odom_sub = self._node.create_subscription(Odometry, "/odom", self._on_odom, 10)
        self._map_sub = self._node.create_subscription(OccupancyGrid, "/map", self._on_map, 1)
        self._head_position_sub = self._node.create_subscription(
            String, self._head_current_position_topic, self._on_head_position, 10
        )
        self._manipulation.start()

    def stop_subscriptions(self) -> None:
        """Destroy robot-state subscriptions when no skill is running."""
        for sub in (self._odom_sub, self._map_sub, self._head_position_sub):
            if sub is not None:
                self._node.destroy_subscription(sub)
        self._odom_sub = None
        self._map_sub = None
        self._head_position_sub = None
        self._manipulation.stop()
        self.last_odom = None
        self.last_map = None
        self.last_head_position = None

    def _on_odom(self, msg: Odometry) -> None:
        self.last_odom = msg

    def _on_map(self, msg: OccupancyGrid) -> None:
        self.last_map = msg

    def _on_head_position(self, msg: String) -> None:
        try:
            self.last_head_position = json.loads(msg.data)
        except Exception as e:
            self._logger.error(f"Failed to parse head position JSON: {e}")

    # --- continuous updates ---
    def begin_continuous_updates(self, skill) -> None:
        with self._current_skill_lock:
            self._current_skill = skill
        self._state_update_stop_event.clear()
        self._state_update_thread = threading.Thread(target=self._state_update_thread_func, daemon=True)
        self._state_update_thread.start()

    def end_continuous_updates(self) -> None:
        if self._state_update_thread is not None:
            self._state_update_stop_event.set()
            self._state_update_thread.join(timeout=1.0)
            self._state_update_thread = None
        with self._current_skill_lock:
            self._current_skill = None

    def _state_update_thread_func(self) -> None:
        """Continuously refresh robot state for the running skill (~50 Hz)."""
        while not self._state_update_stop_event.is_set():
            with self._current_skill_lock:
                if self._current_skill is not None:
                    try:
                        self.update_skill_robot_state(self._current_skill)
                    except Exception as e:
                        self._logger.error(f"Error in continuous state update: {e}")
            self._state_update_stop_event.wait(0.02)

    # --- state injection ---
    def update_skill_robot_state(self, skill) -> None:
        """Update a skill's robot state from current sensor data."""
        required_states = skill.get_required_robot_states()
        if not required_states:
            return

        robot_state_to_inject = {}

        if RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64 in required_states:
            b64 = self._camera.last_main_camera_b64
            if b64 is not None:
                robot_state_to_inject[RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64.value] = b64
            else:
                self._logger.warn("Skill requires LAST_MAIN_CAMERA_IMAGE_B64 but none available.")

        if RobotStateType.LAST_WRIST_CAMERA_IMAGE_B64 in required_states:
            b64 = self._camera.last_wrist_camera_b64
            if b64 is not None:
                robot_state_to_inject[RobotStateType.LAST_WRIST_CAMERA_IMAGE_B64.value] = b64
            else:
                self._logger.warn("Skill requires LAST_WRIST_CAMERA_IMAGE_B64 but none available.")

        if RobotStateType.LAST_ODOM in required_states:
            if self.last_odom is not None:
                pos = self.last_odom.pose.pose.position
                ori = self.last_odom.pose.pose.orientation
                theta = quaternion_to_yaw(ori)
                robot_state_to_inject[RobotStateType.LAST_ODOM.value] = {
                    "header": {
                        "stamp": {
                            "sec": self.last_odom.header.stamp.sec,
                            "nanosec": self.last_odom.header.stamp.nanosec,
                        },
                        "frame_id": self.last_odom.header.frame_id,
                    },
                    "child_frame_id": self.last_odom.child_frame_id,
                    "pose": {
                        "pose": {
                            "position": {"x": pos.x, "y": pos.y, "z": pos.z},
                            "orientation": {"x": ori.x, "y": ori.y, "z": ori.z, "w": ori.w},
                        }
                    },
                    "theta_degrees": math.degrees(theta),
                }
            else:
                self._logger.warn("Skill requires LAST_ODOM but none available.")

        if RobotStateType.LAST_MAP in required_states:
            if self.last_map is not None:
                map_data_bytes = np.array(self.last_map.data, dtype=np.int8).tobytes()
                ori_map = self.last_map.info.origin.orientation
                yaw_map = quaternion_to_yaw(ori_map)
                robot_state_to_inject[RobotStateType.LAST_MAP.value] = {
                    "header": {
                        "stamp": {
                            "sec": self.last_map.header.stamp.sec,
                            "nanosec": self.last_map.header.stamp.nanosec,
                        },
                        "frame_id": self.last_map.header.frame_id,
                    },
                    "info": {
                        "map_load_time": {
                            "sec": self.last_map.info.map_load_time.sec,
                            "nanosec": self.last_map.info.map_load_time.nanosec,
                        },
                        "resolution": self.last_map.info.resolution,
                        "width": self.last_map.info.width,
                        "height": self.last_map.info.height,
                        "origin": {
                            "position": {
                                "x": self.last_map.info.origin.position.x,
                                "y": self.last_map.info.origin.position.y,
                                "z": self.last_map.info.origin.position.z,
                            },
                            "orientation": {"x": ori_map.x, "y": ori_map.y, "z": ori_map.z, "w": ori_map.w},
                            "yaw_degrees": math.degrees(yaw_map),
                        },
                    },
                    "data_b64": base64.b64encode(map_data_bytes).decode("utf-8"),
                }
            else:
                self._logger.warn("Skill requires LAST_MAP but none available.")

        if RobotStateType.LAST_HEAD_POSITION in required_states:
            if self.last_head_position is not None:
                robot_state_to_inject[RobotStateType.LAST_HEAD_POSITION.value] = self.last_head_position
            else:
                self._logger.warn("Skill requires LAST_HEAD_POSITION but none available.")

        if robot_state_to_inject:
            skill.update_robot_state(**robot_state_to_inject)
