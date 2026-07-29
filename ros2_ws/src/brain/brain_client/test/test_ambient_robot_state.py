# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Robot-state declarations and their typed values.

One rule for everything: annotate what you consume — ``odom: Odometry``,
``image: MainImage``, ``mobility: Mobility``. Non-optional means the server
guarantees the value before execute() (bounded wait, run fails otherwise);
``| None`` means best effort. Legacy descriptor assignments keep working.
"""

import base64
import logging
import math
import threading
import time
import warnings
from types import SimpleNamespace

import numpy as np
import pytest

from brain_client.robot.head import HeadInterface as Head
from brain_client.robot.mobility import MobilityInterface as Mobility
from brain_client.skills.arm import Arm
from brain_client.skills.battery import Battery
from brain_client.skills.head import HeadState
from brain_client.skills.image import DepthMap, Image, MainImage, WristImage
from brain_client.skills.joint_states import JointStates
from brain_client.skills.lidar import Lidar
from brain_client.skills.map import Map
from brain_client.skills.odometry import Odometry
from brain_client.skills.pose import Pose
from brain_client.skills.types import (
    Camera,
    Interface,
    InterfaceType,
    RobotState,
    RobotStateType,
    Skill,
    SkillCancelled,
    SkillFailed,
    resource,
)

LOGGER = logging.getLogger(__name__)


class _BareSkill(Skill):
    """No declarations at all."""

    def execute(self):
        return None


def test_declared_sensors_inject_and_gate():
    class Sensors(Skill):
        """test"""

        odom: Odometry
        battery: Battery | None
        lidar: Lidar | None
        arm: Arm | None
        pose: Pose | None

        def execute(self):
            return None

    skill = Sensors(LOGGER)
    assert set(skill.declared_robot_state_types()) == {
        RobotStateType.LAST_ODOM,
        RobotStateType.LAST_BATTERY,
        RobotStateType.LAST_LIDAR,
        RobotStateType.LAST_ARM,
        RobotStateType.LAST_POSE,
    }
    # only the non-optional declaration gates the run
    assert skill.required_robot_state_types() == [RobotStateType.LAST_ODOM]
    assert skill.missing_required_robot_states() == ["odometry"]
    skill.update_robot_state(
        last_odom=Odometry(x=1.0, y=2.0, theta=0.5),
        last_battery=Battery(percentage=0.8, voltage=12.4, current=1.2, charging=False),
        last_lidar=Lidar(ranges=(1.0,), angle_min=0.0, angle_increment=0.0, range_min=0.1, range_max=10.0),
        last_arm=Arm(x=0.2, y=0.0, z=0.3, qx=0.0, qy=0.0, qz=0.0, qw=1.0, gripper=0.5),
        last_pose=Pose(x=3.0, y=4.0, theta=1.0),
    )
    assert skill.missing_required_robot_states() == []
    assert skill.odom.position == (1.0, 2.0)
    assert skill.battery.percentage == 0.8
    assert skill.lidar.min_range() == 1.0
    assert skill.arm.gripper == 0.5
    assert skill.pose.position == (3.0, 4.0)


def test_undeclared_reads_guide_to_annotations():
    skill = _BareSkill(LOGGER)
    with pytest.raises(AttributeError, match="battery: Battery"):
        _ = skill.battery
    with pytest.raises(AttributeError, match="mobility: Mobility"):
        _ = skill.mobility


def test_camera_declares_required_state_and_receives_frames():
    class TwoCameras(Skill):
        """test"""

        image = Camera(RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64)
        wrist = Camera(RobotStateType.LAST_WRIST_CAMERA_IMAGE_B64)

        def execute(self):
            return None

    skill = TwoCameras(LOGGER)
    assert set(skill.declared_robot_state_types()) == {
        RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64,
        RobotStateType.LAST_WRIST_CAMERA_IMAGE_B64,
    }
    skill.update_robot_state(last_main_camera_image_b64="MAIN", last_wrist_camera_image_b64="WRIST")
    assert skill.image == "MAIN"
    assert skill.wrist == "WRIST"


def test_camera_rejects_unknown_feed():
    # the old string form must fail loudly, not silently misdeclare
    with pytest.raises(ValueError, match="MainImage"):
        Camera("main")
    with pytest.raises(ValueError, match="MainImage"):
        Camera(RobotStateType.LAST_ODOM)


def test_depth_camera_declares_required_state():
    class DepthSkill(Skill):
        """test"""

        depth = Camera(RobotStateType.LAST_DEPTH_IMAGE)

        def execute(self):
            return None

    skill = DepthSkill(LOGGER)
    assert skill.declared_robot_state_types() == [RobotStateType.LAST_DEPTH_IMAGE]


def test_pose_wraps_theta_like_odometry():
    assert Pose(x=0.0, y=0.0, theta=3 * math.pi).theta == pytest.approx(math.pi)


def test_lidar_min_range_filters_and_sectors():
    # 8 beams every 45deg starting at -180: angles -180,-135,-90,-45,0,45,90,135
    sweep = Lidar(
        ranges=(0.3, 2.0, float("inf"), 4.0, 5.0, 0.05, 6.0, 1.5),
        angle_min=-math.pi,
        angle_increment=math.pi / 4,
        range_min=0.1,
        range_max=10.0,
    )
    # inf and the 0.05 (below range_min) beams are invalid
    assert sweep.min_range() == 0.3
    # ahead: -45..45 -> beams at -45 (4.0), 0 (5.0); 45 is invalid
    assert sweep.min_range(-45, 45) == 4.0
    # sector wrapping through +-180: beams at 135 (1.5) and -180 (0.3)
    assert sweep.min_range(130, -170) == 0.3
    # empty sector
    assert sweep.min_range(60, 80) is None


def test_legacy_descriptor_still_works_and_tolerates_missing():
    class LegacySkill(Skill):
        """test"""

        battery = RobotState(RobotStateType.LAST_BATTERY)

        def execute(self):
            return None

    skill = LegacySkill(LOGGER)
    # legacy declarations keep the old behavior: no fail-fast, None until injected
    assert skill.battery is None
    assert skill.missing_required_robot_states() == []
    skill.update_robot_state(last_battery=Battery(percentage=0.5, voltage=12.0, current=0.3, charging=False))
    assert skill.battery.percentage == 0.5


def test_interface_required_flag_gates_the_run():
    class HeadUser(Skill):
        """test"""

        manipulation = Interface(InterfaceType.MANIPULATION, required=True)
        head = Interface(InterfaceType.HEAD)  # optional

        def execute(self):
            return None

    skill = HeadUser(LOGGER)
    assert set(skill.declared_interface_types()) == {InterfaceType.MANIPULATION, InterfaceType.HEAD}
    # nothing injected: only the required interface blocks the run
    assert skill.missing_required_interfaces() == [InterfaceType.MANIPULATION.value]


def test_legacy_interface_construction_stays_tolerant():
    """Hand-built Interface(InterfaceType.X) keeps the pre-annotation
    tolerate-None behavior (same rule as legacy RobotState): out-of-tree
    skills guarding on None keep their degraded paths."""

    class OldStyle(Skill):
        """test"""

        head = Interface(InterfaceType.HEAD)

        def execute(self):
            return None

    skill = OldStyle(LOGGER)
    assert skill.declared_interface_types() == [InterfaceType.HEAD]  # still injected
    assert skill.missing_required_interfaces() == []  # but never blocks the run


def test_joint_states_typed_access():
    js = JointStates(
        name=("j1", "j2", "j3", "j4", "j5", "j6"),
        position=(0.0, 0.0, 0.0, 0.0, 0.0, 0.42),
        velocity=(),
        effort=(),
    )
    assert js.position[5] == 0.42
    assert js.of("j6") == (0.42, None, None)
    assert js.of("nope") is None


def test_map_grid_and_legacy_shape():
    msg = SimpleNamespace(
        data=[-1, 0, 100, 0, 0, 100],
        info=SimpleNamespace(
            map_load_time=SimpleNamespace(sec=1, nanosec=2),
            origin=SimpleNamespace(position=SimpleNamespace(z=0.0)),
        ),
    )
    grid_map = Map(resolution=0.05, width=3, height=2, origin_x=1.0, origin_y=2.0, raw_source=msg)
    assert grid_map.grid.shape == (2, 3)
    assert grid_map.grid[1, 2] == 100
    with pytest.warns(FutureWarning):
        legacy = dict(grid_map.items())
    assert legacy["info"]["width"] == 3
    assert base64.b64decode(legacy["data_b64"]) == grid_map.grid.tobytes()


def test_head_state_typed_and_legacy_preserves_unmodeled_keys():
    payload = {"current_position": -20.0, "min_angle": -45.0, "max_angle": 30.0, "default_angle": 0.0, "extra": 1}
    head = HeadState(pitch_degrees=-20.0, min_degrees=-45.0, max_degrees=30.0, default_degrees=0.0, raw_source=payload)
    assert head.pitch_degrees == -20.0
    with pytest.warns(FutureWarning):
        assert head["extra"] == 1
    # the documented `if self.head_position:` None-check must not warn
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert head


def test_image_is_b64_str_with_jpeg_bytes():
    image = Image.from_jpeg(b"\xff\xd8jpegdata")
    assert isinstance(image, str)
    assert base64.b64decode(image) == b"\xff\xd8jpegdata"
    assert image.jpeg == b"\xff\xd8jpegdata"
    # built from base64 text instead: decodes lazily to the same bytes
    assert Image(str(image)).jpeg == b"\xff\xd8jpegdata"


def test_undeclared_camera_attribute_says_how_to_declare():
    skill = _BareSkill(LOGGER)
    with pytest.raises(AttributeError, match="image: MainImage"):
        _ = skill.image
    with pytest.raises(AttributeError, match="no attribute 'nonsense'"):
        _ = skill.nonsense


def test_normalize_keeps_forwarded_child_output_payload():
    """`return some_child_skill(...)` must not strip the child's .data."""
    from brain_client.skills.types import SkillOutput, SkillResult, normalize_skill_result

    child = SkillOutput("done", data={"traveled_m": 1.0})
    message, status = normalize_skill_result(child, "parent")
    assert status is SkillResult.SUCCESS
    assert message == "done"
    assert message.data == {"traveled_m": 1.0}


def test_wait_for_value_timeout_and_cancel():
    from brain_client.skills.types import SkillCancelled

    skill = _BareSkill(LOGGER)
    assert skill.wait_for(lambda: 42, timeout=0.1) == 42
    assert skill.wait_for(lambda: None, timeout=0.05, poll=0.01) is None
    skill._cancelled = True
    with pytest.raises(SkillCancelled):
        skill.wait_for(lambda: None, timeout=1.0, poll=0.01)


def test_annotation_declarations_mint_descriptors():
    class Fetch(Skill):
        """test"""

        mobility: Mobility
        head: Head | None
        image: MainImage
        OBS_Z: float = 0.18  # typed constant: left alone

        def execute(self):
            return None

    skill = Fetch(LOGGER)
    assert set(skill.declared_interface_types()) == {InterfaceType.MOBILITY, InterfaceType.HEAD}
    # head is `| None` -> optional: only mobility blocks the run
    assert skill.missing_required_interfaces() == [InterfaceType.MOBILITY.value]
    assert skill.declared_robot_state_types() == [RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64]
    # required camera with no frame -> the server fails the run up front
    assert skill.missing_required_robot_states() == ["main camera"]
    skill.update_robot_state(last_main_camera_image_b64=MainImage.from_jpeg(b"\xff\xd8"))
    assert skill.missing_required_robot_states() == []
    assert skill.image.jpeg == b"\xff\xd8"
    assert Fetch.OBS_Z == 0.18


def test_string_annotations_resolve():
    class Later(Skill):
        """test"""

        wrist: "WristImage | None"

        def execute(self):
            return None

    skill = Later(LOGGER)
    assert skill.declared_robot_state_types() == [RobotStateType.LAST_WRIST_CAMERA_IMAGE_B64]
    # optional camera: prepared and warmed, but never fails the run
    assert skill.missing_required_robot_states() == []


def test_declarations_inherit_and_index_at_class_creation():
    """Declarations live in a per-class index built at class creation (the
    50 Hz injection path must not MRO-scan): a subclass sees inherited
    declarations plus its own, and the parent's index is untouched."""

    class Base(Skill):
        """test"""

        odom: Odometry

        def execute(self):
            return None

    class Child(Base):
        """test"""

        battery: Battery | None

        def execute(self):
            return None

    child = Child(LOGGER)
    assert set(child.declared_robot_state_types()) == {RobotStateType.LAST_ODOM, RobotStateType.LAST_BATTERY}
    assert child.required_robot_state_types() == [RobotStateType.LAST_ODOM]
    child.update_robot_state(last_odom=Odometry(x=1.0, y=0.0, theta=0.0))
    assert child.odom.x == 1.0
    # parent's own index is unchanged by the subclass
    assert Base(LOGGER).declared_robot_state_types() == [RobotStateType.LAST_ODOM]


def test_describe_feeds_summarizes_declarations():
    class Fetchy(Skill):
        """test"""

        mobility: Mobility
        head: Head | None
        odom: Odometry

        def execute(self):
            return None

    # interfaces first, then robot state; ``?`` marks optional
    assert Fetchy(LOGGER).describe_feeds() == "mobility, head?, odom"
    assert _BareSkill(LOGGER).describe_feeds() == ""


def test_suspicious_bare_annotations_are_recorded():
    """A typo'd feed declaration must be a load-time warning, not a silent
    no-injection: the loader logs whatever lands in _declaration_issues."""

    class NotAFeed:
        pass

    class Suspicious(Skill):
        """test"""

        battery: NotAFeed  # feed name, non-feed type -> "did you mean"
        wrist: "NoSuchType | None"  # unresolvable -> recorded  # noqa: F821
        _tracker: NotAFeed  # non-feed name: legitimately not a declaration

        def execute(self):
            return None

    issues = Suspicious._declaration_issues
    assert any("battery: Battery" in issue for issue in issues)
    assert any("NoSuchType" in issue for issue in issues)
    assert not any("_tracker" in issue for issue in issues)
    # a clean skill records none
    assert _BareSkill._declaration_issues == []


def _import_skill_module(tmp_path, name: str, body: str):
    """Load a module the way discovery does — a real import, not an exec —
    and return the Skill classes it registered."""
    import importlib
    import sys

    (tmp_path / f"{name}.py").write_text(body)
    sys.path.insert(0, str(tmp_path))
    try:
        module = importlib.import_module(name)
        return {
            cls.__name__: cls
            for (mod, _qual), cls in Skill._registry.items()
            if mod == name and getattr(module, cls.__name__, None) is cls
        }
    finally:
        sys.path.remove(str(tmp_path))
        for mod, qual in [k for k in Skill._registry if k[0] == name]:
            del Skill._registry[(mod, qual)]
        sys.modules.pop(name, None)


def test_import_resolves_future_annotations(tmp_path):
    """The REAL load path (importlib, as workspace discovery uses) must resolve
    string annotations — `from __future__ import annotations` makes every
    annotation a string, and resolution goes through sys.modules."""
    classes = _import_skill_module(
        tmp_path,
        "future_skill",
        "from __future__ import annotations\n\n"
        "from innate import Odometry, Skill\n\n\n"
        "class FutureSkill(Skill):\n"
        '    """test"""\n\n'
        "    odom: Odometry\n\n"
        "    def execute(self):\n"
        "        return None\n",
    )
    skill = classes["FutureSkill"](LOGGER)
    assert skill.declared_robot_state_types() == [RobotStateType.LAST_ODOM]
    assert skill.required_robot_state_types() == [RobotStateType.LAST_ODOM]


def test_future_annotations_with_one_typo_degrade_per_name(tmp_path):
    """Under PEP 563 the stdlib resolver is all-or-nothing, but
    ``_own_annotations`` retries per name: one typo is REPORTED (never a
    silent no-injection) and costs only its own declaration — the other
    feeds on the class still resolve and inject."""
    classes = _import_skill_module(
        tmp_path,
        "typo_skill",
        "from __future__ import annotations\n\n"
        "from innate import Odometry, Skill\n\n\n"
        "class TypoSkill(Skill):\n"
        '    """test"""\n\n'
        "    odom: Odometry\n"
        "    battery: Batery | None\n\n"  # noqa: F821 — the typo under test
        "    def execute(self):\n"
        "        return None\n",
    )
    cls = classes["TypoSkill"]
    assert any("Batery" in issue for issue in cls._declaration_issues)
    assert not any("Odometry" in issue for issue in cls._declaration_issues)
    assert cls(LOGGER).declared_robot_state_types() == [RobotStateType.LAST_ODOM]


def test_several_skills_in_one_file_all_register(tmp_path):
    """Filename identity is gone: a file may define as many skills as it likes
    and every one of them registers."""
    classes = _import_skill_module(
        tmp_path,
        "two_skills",
        "from innate import Skill\n\n\n"
        "class Extra(Skill):\n"
        '    """test"""\n\n'
        "    def execute(self):\n"
        "        return None\n\n\n"
        "class TwoSkills(Skill):\n"
        '    """test"""\n\n'
        "    def execute(self):\n"
        "        return None\n",
    )
    assert set(classes) == {"Extra", "TwoSkills"}


def test_depth_map_is_a_typed_ndarray_view():
    depth = np.zeros((2, 3), dtype=np.uint16).view(DepthMap)
    assert isinstance(depth, DepthMap)
    assert depth.shape == (2, 3)


class _Handle:
    def __init__(self):
        self.builds = 0
        self.destroyed = False

    def destroy(self):
        self.destroyed = True


def test_resource_builds_once_and_generator_teardown_runs_on_shutdown():
    class NavLike(Skill):
        """test"""

        @resource
        def handle(self):
            built = _Handle()
            built.builds = self._builds = getattr(self, "_builds", 0) + 1
            yield built
            built.destroy()  # teardown: the code after the yield

        def execute(self):
            return None

    skill = NavLike(LOGGER)
    first = skill.handle
    assert skill.handle is first  # cached across reads
    assert first.builds == 1
    skill.shutdown()
    assert first.destroyed  # the generator resumed past its yield
    second = skill.handle  # a disposed run instance is never reused in prod,
    assert second is not first  # but release re-arms the factory regardless
    assert second.builds == 2


def test_plain_return_resource_has_no_teardown_and_untouched_resources():
    class Plain(Skill):
        """test"""

        @resource
        def conn(self):
            return object()  # plain return: nothing runs at release

        @resource
        def never_built(self):
            raise AssertionError("factory must not run during shutdown")

        def execute(self):
            return None

    skill = Plain(LOGGER)
    built = skill.conn
    assert skill.conn is built
    skill.shutdown()  # never_built's factory must not fire; conn just drops


def test_none_class_default_still_declares_the_feed():
    """``image: MainImage | None = None`` is a natural authoring idiom; the
    None class default must count as an optional declaration, not silently
    suppress injection (the skill would read None forever, and the typo-help
    __getattr__ never fires because the class attribute exists)."""

    class Defaulted(Skill):
        """test"""

        image: MainImage | None = None
        battery: "Battery | None" = None

        def execute(self):
            return None

    skill = Defaulted(LOGGER)
    assert set(skill.declared_robot_state_types()) == {
        RobotStateType.LAST_MAIN_CAMERA_IMAGE_B64,
        RobotStateType.LAST_BATTERY,
    }
    # `= None` reads as "may be None": never gates the run
    assert skill.required_robot_state_types() == []
    assert skill.image is None
    skill.update_robot_state(last_main_camera_image_b64=MainImage.from_jpeg(b"\xff\xd8"))
    assert skill.image.jpeg == b"\xff\xd8"


def test_battery_and_joint_states_legacy_dict_access():
    """Un-migrated skills read these exactly as the dicts 0.3.0-0.6.x
    injected (every other legacy feed already had the shim)."""
    battery = Battery(percentage=0.8, voltage=12.4, current=1.2, charging=False)
    with pytest.warns(FutureWarning):
        assert battery["percentage"] == 0.8
    with pytest.warns(FutureWarning):
        assert battery.get("charging") is False

    js = JointStates(name=("j1", "j6"), position=(0.1, 0.42), velocity=(), effort=())
    with pytest.warns(FutureWarning):
        assert js["position"][1] == 0.42
    with pytest.warns(FutureWarning):
        assert dict(js.items())["name"] == ["j1", "j6"]
    # the documented `if self.<state>:` None-check must not warn
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert battery
        assert js


def test_on_cancel_hooks_fire_on_the_cancelling_thread():
    """on_cancel is how skills brake immediately (stop the base, halt Nav2)
    instead of waiting for the next cancelled poll — the hooks must run
    synchronously on the thread that delivers the cancel, and one failing
    hook must not starve the rest."""
    events = []

    def failing_hook():
        events.append("failing")
        raise RuntimeError("hook blew up")

    skill = _BareSkill(LOGGER)
    skill.on_cancel(lambda: events.append(threading.current_thread()))
    skill.on_cancel(failing_hook)
    skill.on_cancel(lambda: events.append("last"))
    assert not skill.cancelled

    canceller = threading.Thread(target=skill.cancel)
    canceller.start()
    canceller.join(timeout=2.0)

    assert skill.cancelled
    assert events == [canceller, "failing", "last"]


def test_on_cancel_hook_raising_skill_cancelled_is_contained():
    """SkillCancelled is a BaseException on purpose, so it slips every
    `except Exception` — the hook net must name it. A natural brake hook is
    a composed child (`self.on_cancel(self.arm_rest)`), and with the latch
    already set that call raises SkillCancelled; it must not abort the
    remaining hooks or unwind the server's cancel dispatch."""
    events = []

    def cancelled_hook():
        events.append("cancelled")
        raise SkillCancelled("child unwound")

    skill = _BareSkill(LOGGER)
    skill.on_cancel(cancelled_hook)
    skill.on_cancel(lambda: events.append("last"))

    skill.cancel()  # must not raise

    assert events == ["cancelled", "last"]


def test_resource_teardown_raising_skill_cancelled_is_contained():
    """Teardown runs after a cancelled run, so a cancel check tripping in it
    is ordinary — shutdown() must contain SkillCancelled (a BaseException) or
    it escapes the server's finally and the goal never finalizes."""

    class WithResource(_BareSkill):
        """test"""

        @resource
        def thing(self):
            yield object()
            self.check_cancelled()  # trips: the run was cancelled

    skill = WithResource(LOGGER)
    _ = skill.thing  # build it
    skill.cancel()
    skill.shutdown()  # must not raise


def test_on_cancel_hooks_fire_down_the_wired_subskill_tree():
    """A composed child registers its brake hook on ITSELF, and only the
    root's cancel() is ever invoked — the root must fire hooks down the
    wired tree or a child mid-`self.move(...)` coasts until its next poll."""
    events = []

    class Child(Skill):
        """test"""

        def execute(self):
            return None

    class Parent(Skill):
        """test"""

        child: Child

        def execute(self):
            return None

    parent = Parent(LOGGER)
    parent.wire_subskills(lambda cls: cls(LOGGER))
    parent.on_cancel(lambda: events.append("parent"))
    parent.child.on_cancel(lambda: events.append("child"))

    parent.cancel()

    assert events == ["parent", "child"]
    assert parent.child.cancelled  # shared latch


def test_on_cancel_hooks_registered_in_a_composed_call_expire_with_it():
    """A wired sub-skill instance is reused across calls, so a brake hook
    registered inside execute() must live only for its own call — otherwise
    N calls stack N hooks, all firing on one late cancel, including hooks
    over dead per-call state (a finished goal handle)."""
    events = []

    class Child(Skill):
        """test"""

        def execute(self, outcome="ok"):
            self.on_cancel(lambda: events.append("brake"))
            if outcome == "fail":
                self.fail("nope")
            return None

    class Parent(Skill):
        """test"""

        child: Child

        def execute(self):
            return None

    parent = Parent(LOGGER)
    parent.wire_subskills(lambda cls: cls(LOGGER))
    parent.child()
    parent.child()
    with pytest.raises(SkillFailed):
        parent.child(outcome="fail")  # a raising call must expire its hook too

    parent.cancel()
    assert events == []  # three finished calls, zero stale brake firings


def test_invoker_stop_racing_the_watchdog_still_cancels_the_routine():
    """The timeout watchdog cancels only the running child; a user Stop
    arriving mid-watchdog-cancel re-enters on another thread and must latch
    the whole routine — while the child's own cancel() delegating back on
    the same thread must NOT (that would turn every child timeout into a
    routine cancel)."""
    from brain_client.skills.invoker import SkillInvoker

    server = SimpleNamespace(get_logger=lambda: LOGGER)
    invoker = SkillInvoker(server, goal_handle=None, publish_feedback=lambda *a: None, run_node=None)

    class Child:
        def __init__(self, skills):
            self.skills = skills

        def cancel(self):
            # a child's default cancel() delegates right back to the invoker
            self.skills.cancel()

    invoker._active_code_skill = Child(invoker)

    # watchdog path: child cancelled, routine NOT latched (same-thread re-entry)
    invoker._cancel_active_child()
    assert invoker._cancelled is False

    # user Stop while another thread is mid-child-cancel: must latch
    invoker._cancelling = True
    invoker._cancelling_thread = object()  # "another thread"
    assert invoker.cancel() == "Routine cancelled"
    assert invoker._cancelled is True


def test_invoker_watchdog_for_physical_child_spares_the_dispatching_code_skill():
    """A → B (code) → pick_socks (physical, timeout=N): when the physical
    child's watchdog fires, _active_code_skill is B — the skill that
    DISPATCHED the child, an ancestor, not the child — and cancelling it
    would unwind the very routine the per-child timeout is documented not
    to cancel. Only the behavior goal may be touched."""
    from brain_client.skills.invoker import SkillInvoker

    behavior_cancels = []
    server = SimpleNamespace(
        get_logger=lambda: LOGGER,
        _request_behavior_goal_cancel=lambda _goal, skill_id: behavior_cancels.append(skill_id),
    )
    invoker = SkillInvoker(server, goal_handle=None, publish_feedback=lambda *a: None, run_node=None)

    cancelled = []
    invoker._active_code_skill = SimpleNamespace(cancel=lambda: cancelled.append("code"))
    invoker._active_physical_skill = "pick_socks"

    # the watchdog path for a physical child (run() passes include_code=False)
    invoker._cancel_active_child(include_code=False)

    assert behavior_cancels == ["pick_socks"]
    assert cancelled == []  # the dispatching code skill is untouched
    assert invoker._cancelled is False  # and the routine is not latched


# --- RobotStateProvider: injection loop, grace timing, 50 Hz thread ---
# The provider is exercised with fake per-state getters (the real ones need
# live ROS interfaces); the injection/wait/thread machinery under test is
# identical either way.


def _bare_provider():
    from brain_client.skills.robot_state import RobotStateProvider

    provider = RobotStateProvider.__new__(RobotStateProvider)
    provider._logger = SimpleNamespace(warn=lambda *_: None, error=lambda *_: None)
    provider._warned_missing = set()
    provider._current_skill = None
    provider._current_skill_lock = threading.Lock()
    provider._skill_stack = []
    provider._state_update_thread = None
    provider._state_update_stop_event = threading.Event()
    return provider


class _OdomReader(Skill):
    """test"""

    odom: Odometry

    def execute(self):
        return None


def test_update_never_reinjects_none():
    """THE invariant that let migrated skills drop their mid-loop None
    guards (move_straight/turn_in_place read self.odom while driving): once
    a feed has delivered, a getter souring to None must keep the last value
    on the skill, never overwrite it."""
    provider = _bare_provider()
    feed = {"odom": Odometry(x=1.0, y=0.0, theta=0.0)}
    provider._state_getters = lambda: {RobotStateType.LAST_ODOM: lambda: feed["odom"]}

    skill = _OdomReader(LOGGER)
    provider.update_skill_robot_state(skill)
    assert skill.odom.x == 1.0

    feed["odom"] = None  # feed goes quiet mid-run
    provider.update_skill_robot_state(skill)
    assert skill.odom is not None
    assert skill.odom.x == 1.0


def test_continuous_update_thread_injects_and_keeps_last_value():
    """The 50 Hz thread must deliver fresh values as they land and hold the
    never-re-inject-None invariant when the feed drops out."""
    provider = _bare_provider()
    feed = {"odom": Odometry(x=1.0, y=0.0, theta=0.0)}
    provider._state_getters = lambda: {RobotStateType.LAST_ODOM: lambda: feed["odom"]}

    skill = _OdomReader(LOGGER)
    provider.begin_continuous_updates(skill)
    try:
        assert skill.wait_for(lambda: skill.odom, timeout=2.0) is not None
        feed["odom"] = Odometry(x=2.0, y=0.0, theta=0.0)
        assert skill.wait_for(lambda: skill.odom.x == 2.0 or None, timeout=2.0)
        feed["odom"] = None
        time.sleep(0.1)  # several 50 Hz ticks with the feed dark
        assert skill.odom is not None
        assert skill.odom.x == 2.0
    finally:
        provider.end_continuous_updates()


def test_required_state_grace_bounds_the_wait(monkeypatch):
    """A required feed landing inside its grace releases the wait promptly;
    one that never lands bounds it (per-feed grace, spec-table override);
    a cancel abandons the wait instead of riding out the grace."""
    from brain_client.skills import robot_state as robot_state_module

    provider = _bare_provider()
    feed = {"odom": None}
    provider._state_getters = lambda: {RobotStateType.LAST_ODOM: lambda: feed["odom"]}

    # lands at 0.2 s, well inside the default grace: wait ends on arrival
    threading.Timer(0.2, lambda: feed.update(odom=Odometry(x=1.0, y=0.0, theta=0.0))).start()
    skill = _OdomReader(LOGGER)
    start = time.monotonic()
    provider.wait_for_required_states(skill)
    assert time.monotonic() - start < 1.5
    assert skill.odom.x == 1.0
    assert skill.missing_required_robot_states() == []

    # never lands: the wait is bounded by the (monkeypatched) grace
    monkeypatch.setattr(robot_state_module, "_DEFAULT_STATE_GRACE_S", 0.2)
    feed["odom"] = None
    late = _OdomReader(LOGGER)
    start = time.monotonic()
    provider.wait_for_required_states(late)
    elapsed = time.monotonic() - start
    assert 0.15 <= elapsed < 1.5
    assert late.missing_required_robot_states() == ["odometry"]

    # a cancel mid-wait abandons the grace
    monkeypatch.setattr(robot_state_module, "_DEFAULT_STATE_GRACE_S", 30.0)
    cancelled = _OdomReader(LOGGER)
    threading.Timer(0.1, cancelled.cancel).start()
    start = time.monotonic()
    provider.wait_for_required_states(cancelled)
    assert time.monotonic() - start < 2.0
