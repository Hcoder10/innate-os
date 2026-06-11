"""
Integration test: brain_client emits well-formed pose_image messages.

Pose images are how the robot streams its localized camera view to the cloud. This
test runs the real stack in sim mode against a scripted FakeCloud and asserts on
the pose_image messages the brain actually sends up:

  - that a pose_image is sent at all once the handshake completes,
  - that its payload is well-formed (image + x/y/theta + camera_info) and matches
    the static TF we publish (x=1, y=2, yaw=90deg -> theta~pi/2),
  - that pose_image is gated behind registration (not blasted before the brain is
    ready).

The brain runs the WebSocket transport in-process (no separate ws_client node), so
the only observation point is the FakeCloud transcript — which is exactly what the
real cloud would see.

NOTE: requires the ROS2 environment (run inside ci/Dockerfile.test image) and a
Zenoh router; it launches real ROS nodes.
"""

import base64
import os
import sys
import time
import unittest

import cv2
import launch
import launch_ros.actions
import launch_testing
import launch_testing.actions
import launch_testing.asserts
import numpy as np
import pytest
import rclpy
from geometry_msgs.msg import TransformStamped
from rclpy.qos import QoSHistoryPolicy, QoSProfile, QoSReliabilityPolicy
from sensor_msgs.msg import CompressedImage
from std_srvs.srv import SetBool
from tf2_ros.static_transform_broadcaster import StaticTransformBroadcaster

# fake_cloud.py is a sibling module in this test directory.
sys.path.insert(0, os.path.dirname(__file__))
from fake_cloud import FakeCloud  # noqa: E402

IMAGE_TOPIC = "/test/camera/compressed"

# Started in generate_test_description, referenced from the test in the same process.
FAKE = None


@pytest.mark.launch_test
def generate_test_description():
    global FAKE
    FAKE = FakeCloud().start()

    common = {"simulator_mode": True, "image_topic": IMAGE_TOPIC, "map_topic": "/test/map"}

    brain_client = launch_ros.actions.Node(
        package="brain_client",
        executable="brain_client_node.py",
        name="brain_client",
        output="screen",
        parameters=[
            {
                **common,
                "websocket_uri": FAKE.uri,
                "token": "test-token",
                "odom_topic": "/test/odom",
                "amcl_pose_topic": "/test/amcl_pose",
                "depth_image_topic": "/test/depth",
                "current_nav_mode_topic": "/test/nav_mode",
                "send_depth": False,
                "send_arm_camera_image": False,
                "use_odom_as_amcl_pose": True,
                "pose_image_interval": 0.5,
            }
        ],
    )

    skills_action_server = launch_ros.actions.Node(
        package="brain_client",
        executable="skills_server.py",
        name="skills_action_server",
        output="screen",
        parameters=[common],
    )

    return (
        launch.LaunchDescription(
            [
                skills_action_server,
                brain_client,
                launch.actions.TimerAction(period=5.0, actions=[launch_testing.actions.ReadyToTest()]),
            ]
        ),
        {"skills_action_server": skills_action_server, "brain_client": brain_client},
    )


def _make_compressed_image():
    img = np.zeros((100, 100, 3), dtype=np.uint8)
    img[:, :, 2] = 255  # red
    _, buf = cv2.imencode(".jpg", img)
    msg = CompressedImage()
    msg.format = "jpeg"
    msg.data = buf.tobytes()
    return msg


def _make_static_tf(x, y, yaw_z, yaw_w):
    t = TransformStamped()
    t.header.frame_id = "map"
    t.child_frame_id = "base_link"
    t.transform.translation.x = float(x)
    t.transform.translation.y = float(y)
    t.transform.rotation.z = float(yaw_z)
    t.transform.rotation.w = float(yaw_w)
    return t


class TestPoseImage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rclpy.init()

    @classmethod
    def tearDownClass(cls):
        rclpy.shutdown()

    def setUp(self):
        self.node = rclpy.create_node("test_pose_image")
        image_qos = QoSProfile(
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=10,
        )
        self.image_pub = self.node.create_publisher(CompressedImage, IMAGE_TOPIC, image_qos)
        self.tf_broadcaster = StaticTransformBroadcaster(self.node)

    def tearDown(self):
        self.node.destroy_node()

    def _spin_for(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            rclpy.spin_once(self.node, timeout_sec=0.1)

    def _wait_for_brain_client(self, timeout_sec=60.0):
        client = self.node.create_client(SetBool, "/brain/set_brain_active")
        try:
            self.assertTrue(
                client.wait_for_service(timeout_sec=timeout_sec),
                f"brain_client did not become ready within {timeout_sec}s",
            )
        finally:
            self.node.destroy_client(client)

    def _pose_images(self):
        return [m for m in FAKE.transcript if m.get("type") == "pose_image"]

    def _feed_until(self, predicate, timeout=20.0, tf_x=1.0, tf_y=2.0):
        """Publish camera + a static map->base_link TF and spin until predicate()."""
        self.tf_broadcaster.sendTransform(_make_static_tf(tf_x, tf_y, 0.7071, 0.7071))
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.image_pub.publish(_make_compressed_image())
            self._spin_for(0.2)
            if predicate():
                return True
        return predicate()

    def test_pose_image_is_published(self):
        """A pose_image reaches the cloud once the handshake completes."""
        self._wait_for_brain_client()
        sent = self._feed_until(lambda: len(self._pose_images()) > 0)
        self.assertTrue(sent, f"No pose_image received. Types: {FAKE.received_types()}")

    def test_pose_image_payload_is_correct(self):
        """The pose_image payload has image + x/y/theta matching the published TF."""
        self._wait_for_brain_client()
        self.assertTrue(self._feed_until(lambda: len(self._pose_images()) > 0), "No pose_image received")

        payload = self._pose_images()[0].get("payload", {})
        for key in ("image", "x", "y", "theta", "camera_info"):
            self.assertIn(key, payload)
        self.assertAlmostEqual(payload["x"], 1.0, places=1)
        self.assertAlmostEqual(payload["y"], 2.0, places=1)
        self.assertAlmostEqual(payload["theta"], 1.5708, delta=0.1)

        img_bytes = base64.b64decode(payload["image"])
        self.assertGreater(len(img_bytes), 0, "Image data is empty")
        decoded = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_COLOR)
        self.assertIsNotNone(decoded, "Could not decode JPEG from pose_image payload")

    def test_pose_image_gated_until_registered(self):
        """pose_image is not emitted before registration (the readiness trigger)."""
        self._wait_for_brain_client()
        self.assertTrue(self._feed_until(lambda: len(self._pose_images()) > 0), "No pose_image received")
        types = FAKE.received_types()
        self.assertIn("register_primitives_and_directive", types)
        self.assertLess(
            types.index("register_primitives_and_directive"),
            types.index("pose_image"),
            "pose_image was sent before registration",
        )


@launch_testing.post_shutdown_test()
class TestShutdown(unittest.TestCase):
    def test_exit_codes(self, proc_info):
        # 0 (clean) and -2 (SIGINT on teardown) only.
        launch_testing.asserts.assertExitCodes(proc_info, allowable_exit_codes=[0, -2])


def teardown_module(module):
    if FAKE is not None:
        FAKE.stop()
