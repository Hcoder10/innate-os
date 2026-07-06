"""Sim stand-in for mars_nav's grid_localizer (which is CUDA-only and can't
run in the container). Same contract: lifecycle node named
navigation_grid_localizer, latched /initialpose, a `localize` Trigger -- but
"localization" is just the driver's ground-truth odom pose (map == odom in
sim until AMCL refines it)."""

import rclpy
from geometry_msgs.msg import PoseWithCovarianceStamped
from nav_msgs.msg import Odometry
from rclpy.lifecycle import LifecycleNode, TransitionCallbackReturn
from rclpy.qos import DurabilityPolicy, QoSProfile
from std_srvs.srv import Trigger


class GridLocalizerSim(LifecycleNode):
    def __init__(self):
        super().__init__("navigation_grid_localizer")
        self._last_odom = None
        self._pose_pub = None
        self._retry_timer = None
        self.create_subscription(Odometry, "/odom", self._on_odom, 10)
        # AMCL's /initialpose sub is VOLATILE, so latching can't cover the
        # activation race -- retry until AMCL answers with /amcl_pose.
        self.create_subscription(PoseWithCovarianceStamped, "/amcl_pose", self._on_amcl_pose, 10)
        self.create_service(Trigger, "localize", self._on_localize)

    def _on_odom(self, msg):
        self._last_odom = msg

    def _on_amcl_pose(self, _msg):
        if self._retry_timer is not None:
            self._retry_timer.cancel()
            self._retry_timer = None
            self.get_logger().info("AMCL localized; stopping /initialpose retries")

    def on_configure(self, state) -> TransitionCallbackReturn:
        latched = QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL)
        self._pose_pub = self.create_lifecycle_publisher(PoseWithCovarianceStamped, "/initialpose", latched)
        return TransitionCallbackReturn.SUCCESS

    def on_activate(self, state) -> TransitionCallbackReturn:
        self._publish_pose()
        if self._retry_timer is None:
            self._retry_timer = self.create_timer(2.0, self._publish_pose)
        return super().on_activate(state)

    def on_deactivate(self, state) -> TransitionCallbackReturn:
        if self._retry_timer is not None:
            self._retry_timer.cancel()
            self._retry_timer = None
        return super().on_deactivate(state)

    def _on_localize(self, _request, response):
        response.success = self._publish_pose()
        response.message = "ground-truth pose published" if response.success else "no odom yet"
        return response

    def _publish_pose(self) -> bool:
        if self._last_odom is None or self._pose_pub is None:
            return False
        msg = PoseWithCovarianceStamped()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = "map"
        msg.pose.pose = self._last_odom.pose.pose
        msg.pose.covariance[0] = msg.pose.covariance[7] = 0.01
        msg.pose.covariance[35] = 0.01
        self._pose_pub.publish(msg)
        self.get_logger().info("published ground-truth /initialpose")
        return True


def main():
    rclpy.init()
    node = GridLocalizerSim()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
