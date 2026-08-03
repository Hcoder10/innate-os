# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Import shims so the no-ROS unit tests run on a bare Python (e.g. macOS dev
machines) as well as inside the CI image.

``install()`` is a no-op when the real ROS packages import cleanly; otherwise
it registers minimal stand-ins for the modules ``brain_client.robot.
manipulation`` (and the ``innate`` namespace) import at module scope. The
stubs are structural only — enough for classes to be defined, messages to be
instantiated, and signatures to be inspected. Tests never spin ROS: instances
are built with ``Manipulation.__new__`` and hand-set attributes.

Also prepends the package source root to ``sys.path`` so ``brain_client`` and
``innate`` resolve from the working tree without a colcon install.
"""

import sys
import types
import typing
from pathlib import Path

# .../ros2_ws/src/brain/brain_client — the dir holding brain_client/ + innate/
_PKG_ROOT = Path(__file__).resolve().parents[1]


def _module(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


class _Vector3:
    def __init__(self):
        self.x = 0.0
        self.y = 0.0
        self.z = 0.0


class _Quaternion:
    def __init__(self):
        self.x = 0.0
        self.y = 0.0
        self.z = 0.0
        self.w = 1.0


class _Header:
    def __init__(self):
        self.frame_id = ""
        self.stamp = None


class _Pose:
    def __init__(self):
        self.position = _Vector3()
        self.orientation = _Quaternion()


class Twist:
    def __init__(self):
        self.linear = _Vector3()
        self.angular = _Vector3()


class PoseStamped:
    def __init__(self):
        self.header = _Header()
        self.pose = _Pose()


class JointState:
    def __init__(self):
        self.name = []
        self.position = []
        self.velocity = []
        self.effort = []


class Float64MultiArray:
    def __init__(self):
        self.data = []


class Int32:
    def __init__(self):
        self.data = 0


class String:
    def __init__(self):
        self.data = ""


def _service(request_attrs: dict, response_attrs: dict) -> type:
    class Request:
        def __init__(self):
            for key, value in request_attrs.items():
                setattr(self, key, value() if callable(value) else value)

    class Response:
        def __init__(self):
            for key, value in response_attrs.items():
                setattr(self, key, value() if callable(value) else value)

    return type("Service", (), {"Request": Request, "Response": Response})


class _FakeExecutor:
    """Parked stand-in for SingleThreadedExecutor: spin() returns at once."""

    def __init__(self):
        self.nodes = []

    def add_node(self, node):
        self.nodes.append(node)

    def remove_node(self, node):
        self.nodes.remove(node)

    def spin(self):
        pass

    def shutdown(self):
        pass


def _no_node(*_args, **_kwargs):
    raise RuntimeError("ros_stubs: rclpy is stubbed — build test instances with Manipulation.__new__, not __init__")


def install() -> None:
    """Register stub modules unless the real ROS stack is importable."""
    if str(_PKG_ROOT) not in sys.path:
        sys.path.insert(0, str(_PKG_ROOT))

    try:
        import rclpy  # noqa: F401  (real environment — nothing to stub)

        return
    except ImportError:
        pass

    if "rclpy" in sys.modules:  # already stubbed by an earlier install()
        return

    rclpy = _module("rclpy", create_node=_no_node)
    rclpy.executors = _module("rclpy.executors", SingleThreadedExecutor=_FakeExecutor)
    rclpy.node = _module("rclpy.node", Node=type("Node", (), {}))
    rclpy.subscription = _module("rclpy.subscription", Subscription=type("Subscription", (), {}))
    rclpy.timer = _module("rclpy.timer", Timer=type("Timer", (), {}))

    geometry = _module("geometry_msgs")
    geometry.msg = _module("geometry_msgs.msg", PoseStamped=PoseStamped, Twist=Twist)
    sensor = _module("sensor_msgs")
    sensor.msg = _module("sensor_msgs.msg", JointState=JointState)
    std_msgs = _module("std_msgs")
    std_msgs.msg = _module("std_msgs.msg", Float64MultiArray=Float64MultiArray, Int32=Int32, String=String)
    std_srvs = _module("std_srvs")
    std_srvs.srv = _module(
        "std_srvs.srv",
        Trigger=_service({}, {"success": False, "message": ""}),
        SetBool=_service({"data": False}, {"success": False, "message": ""}),
    )
    mars_msgs = _module("mars_msgs")
    mars_msgs.srv = _module(
        "mars_msgs.srv",
        GotoJS=_service({"data": None, "time": 0.0}, {"success": False}),
        GotoJSTrajectory=_service({"waypoints": None, "num_joints": 0, "segment_durations": list}, {"success": False}),
    )
    mars_msgs.msg = _module(
        "mars_msgs.msg",
        ArmStatus=type("ArmStatus", (), {"is_ok": True, "error": "", "is_torque_enabled": False}),
    )

    try:
        import typing_extensions  # noqa: F401
    except ImportError:
        te = _module("typing_extensions")
        te.__getattr__ = lambda name: getattr(typing, name)
