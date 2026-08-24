#!/usr/bin/env python3
"""Count messages actually arriving on the sensor topics, with a real subscriber.

`ros2 topic hz` reported "does not appear to be published yet" for both the
camera and odometry while `ros2 topic info` showed a publisher on each and
`ros2 node info /virtual_mars` listed them as its own. Those two cannot both be
right about data flow, and the CLI is the less trustworthy witness: it runs in
a separate process from a docker exec, so a FastDDS shared-memory discovery
miss looks exactly like silence.

This subscribes the same way the brain does and counts. If the counts are
non-zero the topics are fine and the CLI was the problem; if they are zero the
driver really is not publishing and the next place to look is its own stdout.

Run inside the container:
  source /opt/ros/humble/setup.bash
  source /root/innate-os/ros2_ws/install/setup.bash
  python3 /root/innate-os/workspace/probe_topics.py
"""

import time

import rclpy
from nav_msgs.msg import Odometry
from rclpy.node import Node
from sensor_msgs.msg import CompressedImage

SECONDS = 12
CAM = "/mars/main_camera/left/image_raw/compressed"
ODOM = "/odom"


def main() -> None:
    rclpy.init()
    node = Node("bench_probe_counter")
    counts = {"camera": 0, "odom": 0}

    def bump(key):
        def cb(_msg):
            counts[key] += 1
        return cb

    # Depth 10, default reliable QoS -- the same shape the brain's own camera
    # subscription uses. A QoS mismatch is its own failure mode and would show
    # up here as zero with a live publisher.
    node.create_subscription(CompressedImage, CAM, bump("camera"), 10)
    node.create_subscription(Odometry, ODOM, bump("odom"), 10)

    start = time.time()
    while time.time() - start < SECONDS:
        rclpy.spin_once(node, timeout_sec=0.2)

    elapsed = time.time() - start
    print(f"over {elapsed:.1f}s:")
    for key, n in counts.items():
        print(f"  {key:<8} {n:5d} messages   {n / elapsed:6.2f} Hz")
    rclpy.shutdown()


if __name__ == "__main__":
    main()
