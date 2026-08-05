# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Import shims so the no-ROS unit tests run on a bare Python (e.g. macOS dev
machines) as well as inside the CI image.

``install()`` registers minimal stand-ins for the modules ``brain_client.robot.
manipulation`` (and the ``innate`` namespace) import at module scope, skipping
each one the environment already provides. The stubs are structural only —
enough for classes to be defined, messages to be instantiated, and signatures
to be inspected. Tests never spin ROS: instances are built with
``Manipulation.__new__`` and hand-set attributes.

Each module is probed on its own rather than treating ``import rclpy`` as
proof of a whole ROS stack. A sourced ``/opt/ros`` with an unbuilt colcon
workspace has rclpy and the standard messages but no ``mars_msgs`` — which is
exactly what running these tests straight from a dev checkout looks like
(see docs/TESTING.md), and a partial stub is what makes that work.

Also prepends the package source root to ``sys.path`` so ``brain_client`` and
``innate`` resolve from the working tree without a colcon install.
"""

import importlib
import sys
import types
import typing
from pathlib import Path

# .../ros2_ws/src/brain/brain_client — the dir holding brain_client/ + innate/
_PKG_ROOT = Path(__file__).resolve().parents[1]


def _module(name: str, **attrs) -> types.ModuleType:
    """Register a stand-in module, creating any missing parent packages.

    ``from mars_msgs.msg import X`` resolves through both ``sys.modules`` and
    the parent's attribute, so a stubbed leaf has to be reachable by each.
    """
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    parent_name, _, leaf = name.rpartition(".")
    if parent_name:
        parent = sys.modules.get(parent_name) or _module(parent_name)
        setattr(parent, leaf, mod)
    return mod


def _stub(name: str, **attrs) -> types.ModuleType | None:
    """Register ``name``'s stand-in unless this environment already has it.

    Returns the stub, or None when the real module was used.
    """
    if name in sys.modules:  # real, or stubbed by an earlier install()
        return None
    try:
        importlib.import_module(name)
    except ImportError:
        return _module(name, **attrs)
    return None


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


class String:
    def __init__(self):
        self.data = ""


def _service(request_attrs: dict) -> type:
    """A service type with just the Request half — tests hand-roll responses."""

    class Request:
        def __init__(self):
            for key, value in request_attrs.items():
                setattr(self, key, value() if callable(value) else value)

    return type("Service", (), {"Request": Request})


class _FakeExecutor:
    """Name for `monkeypatch.setattr` to replace; the body never runs (tests
    build instances with __new__, so no executor is ever created here)."""


def _no_node(*_args, **_kwargs):
    raise RuntimeError("ros_stubs: rclpy is stubbed — build test instances with Manipulation.__new__, not __init__")


def install() -> None:
    """Stub whichever of the ROS modules these tests import are missing here."""
    if str(_PKG_ROOT) not in sys.path:
        sys.path.insert(0, str(_PKG_ROOT))

    _stub("rclpy", create_node=_no_node)
    _stub("rclpy.executors", SingleThreadedExecutor=_FakeExecutor)
    _stub("rclpy.node", Node=type("Node", (), {}))
    _stub("rclpy.subscription", Subscription=type("Subscription", (), {}))

    _stub("geometry_msgs.msg", PoseStamped=PoseStamped, Twist=Twist)
    _stub("sensor_msgs.msg", JointState=JointState)
    _stub("std_msgs.msg", Float64MultiArray=Float64MultiArray, String=String)
    _stub("std_srvs.srv", Trigger=_service({}))
    # The workspace's own messages: present only once colcon has built them,
    # so this is the stub a bare `pytest ros2_ws/...` actually leans on.
    _stub(
        "mars_msgs.srv",
        GotoJS=_service({"data": None, "time": 0.0}),
        GotoJSTrajectory=_service({"waypoints": None, "num_joints": 0, "segment_durations": list}),
    )
    _stub(
        "mars_msgs.msg",
        ArmStatus=type("ArmStatus", (), {"is_ok": True, "error": "", "is_torque_enabled": False}),
    )

    te = _stub("typing_extensions")
    if te is not None:
        te.__getattr__ = lambda name: getattr(typing, name)
