#!/usr/bin/env python3

from action_msgs.msg import GoalStatus
import rclpy
from rclpy.action import ActionClient
from rclpy.node import Node

from geometry_msgs.msg import PoseStamped
from nav2_msgs.action import ComputePathToPose
from nav_msgs.msg import Path
from std_msgs.msg import String


class SimGoalPlanner(Node):
    """Plan simulator UI goals with Nav2 and publish paths for the sim follower."""

    def __init__(self):
        super().__init__("sim_goal_planner")

        self.declare_parameter("goal_topic", "/sim_navigation/goal_pose")
        self.declare_parameter("plan_topic", "/sim_navigation/global_plan")
        self.declare_parameter("status_topic", "/sim_navigation/status")
        self.declare_parameter("planner_action", "/compute_path_to_pose")
        self.declare_parameter("planner_id", "GridBased")

        self.goal_topic = self.get_parameter("goal_topic").value
        self.plan_topic = self.get_parameter("plan_topic").value
        self.status_topic = self.get_parameter("status_topic").value
        self.planner_action = self.get_parameter("planner_action").value
        self.planner_id = self.get_parameter("planner_id").value

        self._goal_sequence = 0
        self._planner = ActionClient(
            self,
            ComputePathToPose,
            self.planner_action,
        )
        self._path_pub = self.create_publisher(Path, self.plan_topic, 10)
        self._status_pub = self.create_publisher(String, self.status_topic, 10)
        self._goal_sub = self.create_subscription(
            PoseStamped,
            self.goal_topic,
            self._goal_callback,
            10,
        )

        self.get_logger().info(
            "Simulator Nav2 goal planner ready: "
            f"{self.goal_topic} -> {self.planner_action} -> {self.plan_topic}"
        )

    def _publish_status(self, status: str) -> None:
        msg = String()
        msg.data = status
        self._status_pub.publish(msg)

    def _goal_callback(self, pose: PoseStamped) -> None:
        self._goal_sequence += 1
        sequence = self._goal_sequence

        if not pose.header.frame_id:
            pose.header.frame_id = "map"
        pose.header.stamp = self.get_clock().now().to_msg()

        self.get_logger().info(
            "Planning simulator goal "
            f"({pose.pose.position.x:.2f}, {pose.pose.position.y:.2f}) "
            f"in frame {pose.header.frame_id}"
        )
        self._publish_status("PLANNING")

        if not self._planner.wait_for_server(timeout_sec=1.0):
            self.get_logger().error(
                f"Nav2 planner action is not available: {self.planner_action}"
            )
            self._publish_status("FAILED")
            return

        goal = ComputePathToPose.Goal()
        goal.goal = pose
        goal.planner_id = self.planner_id
        goal.use_start = False

        future = self._planner.send_goal_async(goal)
        future.add_done_callback(
            lambda done_future: self._on_goal_response(done_future, sequence)
        )

    def _on_goal_response(self, future, sequence: int) -> None:
        if sequence != self._goal_sequence:
            return

        try:
            goal_handle = future.result()
        except Exception as exc:
            self.get_logger().error(f"Failed to send Nav2 planning goal: {exc}")
            self._publish_status("FAILED")
            return

        if not goal_handle.accepted:
            self.get_logger().warning("Nav2 planning goal was rejected")
            self._publish_status("FAILED")
            return

        result_future = goal_handle.get_result_async()
        result_future.add_done_callback(
            lambda done_future: self._on_plan_result(done_future, sequence)
        )

    def _on_plan_result(self, future, sequence: int) -> None:
        if sequence != self._goal_sequence:
            return

        try:
            result_response = future.result()
        except Exception as exc:
            self.get_logger().error(f"Failed while waiting for Nav2 plan: {exc}")
            self._publish_status("FAILED")
            return

        if result_response.status != GoalStatus.STATUS_SUCCEEDED:
            self.get_logger().warning(
                f"Nav2 planning failed with status {result_response.status}"
            )
            self._publish_status("FAILED")
            return

        path = result_response.result.path
        if not path.poses:
            self.get_logger().warning("Nav2 returned an empty plan")
            self._publish_status("FAILED")
            return

        if not path.header.frame_id:
            path.header.frame_id = "map"
        path.header.stamp = self.get_clock().now().to_msg()

        self._path_pub.publish(path)
        self._publish_status("ACTIVE")
        self.get_logger().info(f"Published Nav2 simulator plan with {len(path.poses)} poses")


def main(args=None):
    rclpy.init(args=args)
    node = SimGoalPlanner()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
