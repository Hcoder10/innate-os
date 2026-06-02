"""
Integration test: full robot brain loop against a scripted FakeCloud.

This is the behavioral counterpart to the "launch the nodes, see if any crash"
liveness smoke test. It launches the REAL robot stack and drives a complete
directive->primitive->completion cycle with a deterministic, scripted cloud, then
asserts on what actually crossed the wire. Because the same ROS2 nodes run in sim
and on hardware, green here is high-confidence evidence about hardware behavior.

What runs (all real, sim mode):
  - brain_client_node      - orchestration: registration, vision-output handling,
                             primitive lifecycle, brain activation
  - ws_client_node         - the WebSocket transport (auth, connect, forward)
  - skills_action_server   - executes the dispatched primitive (ExecuteSkill)
  - FakeCloud (this test)   - scriptable stand-in for innate-cloud-agent on ws 8765

Why it is self-driving: brain_client_node broadcasts READY_FOR_CONNECTION on
startup, so ws_client_node connects to FakeCloud on its own; in simulator_mode the
brain auto-activates after registration (no /brain/set_brain_active needed). The
test only has to feed a synthetic camera image + TF (no hardware) so the brain has
perception to send - the same trick test_pose_image.launch.py uses.

The loop (verified against brain_client_node.py):
  ws_client connect -> auth -> FakeCloud: ready_for_image
  brain: register_primitives_and_directive -> FakeCloud: ...registered (ack)
  sim auto-activates brain -> brain: image -> FakeCloud: vision_agent_output{next_task}
  brain: ExecuteSkill -> skills_action_server runs primitive -> SUCCEEDED
  brain: primitive_completed -> FakeCloud records it

Assertions are on FakeCloud.transcript (what the robot sent up) - the protocol
behavior that silently breaks between releases.

Run:
  colcon test --packages-select brain_client --ctest-args -R test_fake_cloud_loop
  colcon test-result --verbose

NOTE: requires the ROS2 environment (run inside ci/Dockerfile.test image). It
cannot run on a bare host - it launches real ROS nodes.
"""

import json
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
from std_msgs.msg import String
from std_srvs.srv import SetBool
from tf2_ros.static_transform_broadcaster import StaticTransformBroadcaster

# fake_cloud.py is a sibling module in this test directory.
sys.path.insert(0, os.path.dirname(__file__))
from fake_cloud import FakeCloud  # noqa: E402

# A default skill that loads and succeeds headlessly (send_email needs no external
# creds in sim). The scripted next_task dispatches it end to end.
SCRIPTED_SKILL = "innate-os/send_email"
SCRIPTED_INPUTS = {
    "subject": "Integration Test",
    "message": "Dispatched by test_fake_cloud_loop.",
    "recipients": ["test@example.com"],
}

IMAGE_TOPIC = "/test/camera/compressed"

# FakeCloud is started in generate_test_description (before the nodes connect) and
# referenced from the active test in the same process. Module-global by necessity:
# launch_testing runs the description and the TestCase in one interpreter.
# Each test scripts its OWN tasks at runtime and asserts only on the slice of the
# transcript it produced (see _baseline), so tests don't depend on run order.
FAKE = None


@pytest.mark.launch_test
def generate_test_description():
    global FAKE
    FAKE = FakeCloud().start()

    common = {
        "simulator_mode": True,
        "image_topic": IMAGE_TOPIC,
        "map_topic": "/test/map",
    }

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

    ws_client = launch_ros.actions.Node(
        package="brain_client",
        executable="ws_client_node.py",
        name="ws_client_node",
        output="screen",
        parameters=[{"websocket_uri": FAKE.uri, "token": "test-token"}],
    )

    skills_action_server = launch_ros.actions.Node(
        package="brain_client",
        executable="skills_action_server.py",
        name="skills_action_server",
        output="screen",
        parameters=[common],
    )

    return (
        launch.LaunchDescription(
            [
                brain_client,
                ws_client,
                skills_action_server,
                launch.actions.TimerAction(
                    period=5.0,
                    actions=[launch_testing.actions.ReadyToTest()],
                ),
            ]
        ),
        {
            "brain_client": brain_client,
            "ws_client_node": ws_client,
            "skills_action_server": skills_action_server,
        },
    )


def _make_compressed_image():
    img = np.zeros((100, 100, 3), dtype=np.uint8)
    img[:, :, 2] = 255
    _, buf = cv2.imencode(".jpg", img)
    msg = CompressedImage()
    msg.format = "jpeg"
    msg.data = buf.tobytes()
    return msg


def _make_static_tf():
    t = TransformStamped()
    t.header.frame_id = "map"
    t.child_frame_id = "base_link"
    t.transform.translation.x = 1.0
    t.transform.translation.y = 2.0
    t.transform.rotation.z = 0.7071
    t.transform.rotation.w = 0.7071
    return t


class TestFakeCloudLoop(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rclpy.init()

    @classmethod
    def tearDownClass(cls):
        rclpy.shutdown()

    def setUp(self):
        self.node = rclpy.create_node("test_fake_cloud_loop")
        image_qos = QoSProfile(
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
            history=QoSHistoryPolicy.KEEP_LAST,
            depth=10,
        )
        self.image_pub = self.node.create_publisher(CompressedImage, IMAGE_TOPIC, image_qos)
        self.chat_pub = self.node.create_publisher(String, "/brain/chat_in", 10)
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
                f"brain_client_node not ready within {timeout_sec}s",
            )
        finally:
            self.node.destroy_client(client)

    def _feed_until(self, predicate, timeout=40.0):
        """Publish image + TF and spin until predicate() is true (return True) or
        the timeout elapses (return False). Returns as soon as the work is done -
        fast on fast machines, tolerant on slow CI, no fixed-duration sleeps."""
        self.tf_broadcaster.sendTransform(_make_static_tf())
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.image_pub.publish(_make_compressed_image())
            self._spin_for(0.2)
            if predicate():
                return True
        return predicate()

    def _since(self, before, *types):
        """Transcript messages of the given types recorded since index `before`.

        Slicing from a per-test baseline keeps each test independent of work done
        by earlier-running tests (methods share the one FAKE transcript)."""
        return [m for m in FAKE.transcript[before:] if m.get("type") in types]

    def test_connection_and_registration(self):
        """The stack connects and registers without manual intervention:
        auth -> ready_for_image -> register_primitives_and_directive."""
        self._wait_for_brain_client()
        FAKE.wait_for("auth", timeout=30.0)
        FAKE.wait_for("register_primitives_and_directive", timeout=30.0)

    def test_brain_sends_perception(self):
        """Once registered + auto-activated (sim mode), the brain sends imagery."""
        self._wait_for_brain_client()
        FAKE.wait_for("register_primitives_and_directive", timeout=30.0)
        before = len(FAKE.transcript)
        sent = self._feed_until(lambda: bool(self._since(before, "image", "pose_image")), timeout=20.0)
        self.assertTrue(sent, f"Brain never sent perception. Types: {FAKE.received_types()}")

    def test_scripted_primitive_runs_to_completion(self):
        """A scripted next_task is dispatched, executed by the skills server, and
        reported complete - a full directive->primitive->completion cycle. Scripts
        its own task and asserts only on its own slice, so it is order-independent."""
        self._wait_for_brain_client()
        before = len(FAKE.transcript)
        pid = FAKE.script_task(SCRIPTED_SKILL, SCRIPTED_INPUTS)

        # primitive_activated carries next_task.primitive_id verbatim
        # (brain_client_node.py:1802), so this correlation is exact.
        activated = self._feed_until(
            lambda: any(
                m.get("payload", {}).get("primitive_id") == pid for m in self._since(before, "primitive_activated")
            ),
            timeout=40.0,
        )
        self.assertTrue(
            activated,
            f"Scripted primitive {pid} was never activated. Types: {FAKE.received_types()}",
        )

        done = self._feed_until(
            lambda: bool(self._since(before, "primitive_completed", "primitive_failed")),
            timeout=20.0,
        )
        self.assertTrue(
            done,
            f"Primitive activated but never reported a terminal result. Types: {FAKE.received_types()}",
        )
        # send_email succeeds headlessly (no external creds) -> completion, not failure.
        terminal = self._since(before, "primitive_completed", "primitive_failed")
        self.assertEqual(
            terminal[0].get("type"),
            "primitive_completed",
            f"Expected primitive_completed, got {terminal[0].get('type')}: {terminal[0].get('payload')}",
        )

    def test_loop_continues_after_completion(self):
        """After one primitive completes the brain requests and runs the next -
        the post-completion continuation path. Scripts two of its own tasks."""
        self._wait_for_brain_client()
        before = len(FAKE.transcript)
        FAKE.script_task(SCRIPTED_SKILL, SCRIPTED_INPUTS)
        FAKE.script_task(SCRIPTED_SKILL, SCRIPTED_INPUTS)
        done = self._feed_until(lambda: len(self._since(before, "primitive_completed")) >= 2, timeout=45.0)
        self.assertTrue(
            done,
            f"Loop did not complete two primitives. Types: {FAKE.received_types()}",
        )

    def test_chat_in_is_forwarded_to_cloud(self):
        """A chat message on /brain/chat_in is forwarded up as chat_in - the fast
        path that bypasses the image queue (brain_client_node.py:1155)."""
        self._wait_for_brain_client()
        # Registration -> brain auto-activates in sim mode; chat_in needs that.
        FAKE.wait_for("register_primitives_and_directive", timeout=30.0)
        before = len(FAKE.transcript)
        end = time.monotonic() + 15.0
        forwarded = False
        while time.monotonic() < end and not forwarded:
            self.chat_pub.publish(String(data=json.dumps({"text": "hello robot"})))
            self._spin_for(0.5)
            forwarded = bool(self._since(before, "chat_in"))
        self.assertTrue(forwarded, f"chat_in was not forwarded. Types: {FAKE.received_types()}")


@launch_testing.post_shutdown_test()
class TestShutdown(unittest.TestCase):
    def test_exit_codes(self, proc_info):
        # Teardown is noisy for these nodes (a known wart also tolerated by
        # test_pose_image): launch_testing's SIGINT races the node's own
        # rclpy.shutdown() -> RCLError "rcl_shutdown already called" (exit 1), and
        # a slow websocket-asyncio teardown can hit the SIGKILL escalation (-9).
        # The active tests above are the real correctness signal.
        launch_testing.asserts.assertExitCodes(
            proc_info,
            allowable_exit_codes=[0, -2, 1, -9],
        )


def teardown_module(module):
    if FAKE is not None:
        FAKE.stop()
