#!/usr/bin/env python3
"""
Patrol Waypoints Skill - Save named waypoints and patrol through them.

Custom skill: a customer's "guard tour" building block. Waypoints are stored in
a JSON file so patrol routes survive restarts. The patrol visits each waypoint
with Nav2 and sends a camera snapshot from every stop.
"""

import json
import math
import threading
import time
from pathlib import Path
from typing import Literal

from geometry_msgs.msg import PoseStamped
from nav2_simple_commander.robot_navigator import BasicNavigator, TaskResult

from brain_client.skills.types import RobotState, RobotStateType, Skill, SkillResult

WAYPOINTS_FILE = Path.home() / "patrol_waypoints.json"
Action = Literal["save", "list", "remove", "patrol"]


class PatrolWaypoints(Skill):
    """Manage named map waypoints and patrol through them in order."""

    image = RobotState(RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64)

    def __init__(self, logger):
        super().__init__(logger)
        self._cancel_requested = threading.Event()
        self._navigator = None
        self._navigator_navigation = None

    @property
    def name(self):
        return "patrol_waypoints"

    def guidelines(self):
        return (
            "Manage and patrol named map waypoints. 'action' parameter is one of: "
            "'save' (store a waypoint; needs 'waypoint_name', 'x', 'y', 'theta' in map frame), "
            "'list' (show saved waypoints), "
            "'remove' (delete one; needs 'waypoint_name'), "
            "'patrol' (visit every saved waypoint in order, sending a camera view from each). "
            "Only works when a map is loaded and the robot is in navigation mode."
        )

    def _load_waypoints(self) -> dict:
        if WAYPOINTS_FILE.exists():
            try:
                return json.loads(WAYPOINTS_FILE.read_text())
            except Exception:
                return {}
        return {}

    def _get_navigators(self):
        if self._navigator is None:
            self._navigator = BasicNavigator(namespace="")
            self._navigator_navigation = BasicNavigator(namespace="navigation")
        return self._navigator, self._navigator_navigation

    def execute(self, action: Action, waypoint_name: str = "", x: float = 0.0, y: float = 0.0, theta: float = 0.0):
        """
        Args:
            action: 'save', 'list', 'remove', or 'patrol'.
            waypoint_name: Name of the waypoint (for save/remove).
            x, y, theta: Map-frame pose (for save).
        """
        self._cancel_requested.clear()
        action = action.strip().lower()

        if action == "save":
            return self._save(waypoint_name, x, y, theta)
        if action == "list":
            return self._list()
        if action == "remove":
            return self._remove(waypoint_name)
        if action == "patrol":
            return self._patrol()
        return f"Unknown action '{action}'. Use save, list, remove, or patrol.", SkillResult.FAILURE

    def _save(self, name: str, x: float, y: float, theta: float):
        if not name:
            return "waypoint_name is required for save", SkillResult.FAILURE
        waypoints = self._load_waypoints()
        waypoints[name] = {"x": float(x), "y": float(y), "theta": float(theta)}
        WAYPOINTS_FILE.write_text(json.dumps(waypoints, indent=2))
        return f"Saved waypoint '{name}' at ({x:.2f}, {y:.2f}, {theta:.2f})", SkillResult.SUCCESS

    def _list(self):
        waypoints = self._load_waypoints()
        if not waypoints:
            return "No waypoints saved yet", SkillResult.SUCCESS
        lines = [f"{name}: ({wp['x']:.2f}, {wp['y']:.2f}, {wp['theta']:.2f})" for name, wp in waypoints.items()]
        return "Saved waypoints: " + "; ".join(lines), SkillResult.SUCCESS

    def _remove(self, name: str):
        waypoints = self._load_waypoints()
        if name not in waypoints:
            return f"No waypoint named '{name}'", SkillResult.FAILURE
        del waypoints[name]
        WAYPOINTS_FILE.write_text(json.dumps(waypoints, indent=2))
        return f"Removed waypoint '{name}'", SkillResult.SUCCESS

    def _patrol(self):
        waypoints = self._load_waypoints()
        if not waypoints:
            return "No waypoints saved - save some before patrolling", SkillResult.FAILURE

        navigator, path_navigator = self._get_navigators()
        visited = []

        for name, wp in waypoints.items():
            if self._cancel_requested.is_set():
                return f"Patrol cancelled after visiting: {', '.join(visited) or 'none'}", SkillResult.CANCELLED

            self._send_feedback(f"Heading to waypoint '{name}'")

            goal = PoseStamped()
            goal.header.frame_id = "map"
            goal.header.stamp = navigator.get_clock().now().to_msg()
            goal.pose.position.x = wp["x"]
            goal.pose.position.y = wp["y"]
            goal.pose.orientation.z = math.sin(wp["theta"] / 2.0)
            goal.pose.orientation.w = math.cos(wp["theta"] / 2.0)

            path = path_navigator.getPath(goal, goal, use_start=False)
            if path is None:
                self._send_feedback(f"No path to waypoint '{name}', skipping it")
                continue

            navigator.goToPose(goal, behavior_tree="navigation")
            while not navigator.isTaskComplete():
                if self._cancel_requested.is_set():
                    navigator.cancelTask()
                    return f"Patrol cancelled after visiting: {', '.join(visited) or 'none'}", SkillResult.CANCELLED
                time.sleep(0.1)

            if navigator.getResult() == TaskResult.SUCCEEDED:
                visited.append(name)
                time.sleep(0.5)  # let the camera settle
                if self.image:
                    self._send_feedback(f"Arrived at '{name}' - here is the view", self.image)
                else:
                    self._send_feedback(f"Arrived at '{name}'")
            else:
                self._send_feedback(f"Could not reach waypoint '{name}', continuing patrol")

        if not visited:
            return "Patrol finished but no waypoint could be reached", SkillResult.FAILURE
        return f"Patrol complete. Visited: {', '.join(visited)}", SkillResult.SUCCESS

    def cancel(self):
        self._cancel_requested.set()
        return "Patrol cancelled"
