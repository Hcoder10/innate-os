# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Class-based skill composition: declare a sub-skill, call the attribute.

The PyTorch shape — ``gripper_open: GripperOpen`` declares like a submodule,
``self.gripper_open(percent=50)`` calls through the framework's contract
(SkillOutput on success, SkillFailed/SkillCancelled raised otherwise). The
declared class is what runs; overriding is subclassing and re-declaring, not
file naming. Wiring gives every child the run's plumbing and the parent's
cancel latch, and the parent's feed views (declared/required/missing states)
union over the tree so the server's up-front checks and 50 Hz pushes cover
children too.

ROS-free. Part of the fast pytest bucket in ci/run_integration_tests.sh.
"""

import logging

import pytest

from brain_client.skills.types import (
    PhysicalSkill,
    RobotStateType,
    Skill,
    SkillCancelled,
    SkillFailed,
    SkillResult,
    SubSkill,
)

LOGGER = logging.getLogger("skill_composition_test")


class GripperOpen(Skill):
    """Open the gripper."""

    def execute(self, percent: float = 100.0):
        return f"opened {percent:.0f}%", SkillResult.SUCCESS

    def cancel(self):
        pass


class FlakyGripper(GripperOpen):
    def execute(self, percent: float = 100.0):
        return "servo tripped", SkillResult.FAILURE


class PickSocks(Skill):
    """Pick socks using a declared sub-skill."""

    gripper_open: GripperOpen

    def execute(self):
        out = self.gripper_open(percent=50)
        return f"picked after {out}", SkillResult.SUCCESS

    def cancel(self):
        pass


def _wire(cls):
    return cls(LOGGER)


def test_annotation_becomes_a_subskill_declaration():
    assert isinstance(vars(PickSocks)["gripper_open"], SubSkill)
    assert PickSocks._feed_subskills["gripper_open"].skill_class is GripperOpen


def test_optional_subskill_annotation_is_flagged_not_silently_required():
    """The feed rule (`| None` = best effort) does NOT extend to composition:
    a declared sub-skill is always wired and its requirements gate the run.
    That divergence must be a load-time warning, not a silent surprise when
    the parent fails up front on the child's feeds."""

    class Optimist(Skill):
        """test"""

        gripper_open: GripperOpen | None

        def execute(self):
            pass

    # still declared and required — behavior is unchanged, just no longer silent
    assert Optimist._feed_subskills["gripper_open"].skill_class is GripperOpen
    assert any("| None" in issue and "gripper_open" in issue for issue in Optimist._declaration_issues)


def test_wired_child_is_callable_and_result_flows_back():
    skill = PickSocks(LOGGER)
    skill.wire_subskills(_wire)
    out, status = skill.execute()
    assert status is SkillResult.SUCCESS
    assert out == "picked after opened 50%"


def test_child_failure_raises_skill_failed():
    skill = PickSocks(LOGGER)
    skill.wire_subskills(lambda cls: FlakyGripper(LOGGER) if cls is GripperOpen else cls(LOGGER))
    with pytest.raises(SkillFailed, match="servo tripped"):
        skill.execute()


def test_call_maps_cancelled_to_skill_cancelled():
    class Cancelled(Skill):
        def execute(self):
            return "stopped", SkillResult.CANCELLED

        def cancel(self):
            pass

    with pytest.raises(SkillCancelled, match="stopped"):
        Cancelled(LOGGER)()


def test_children_share_the_parents_cancel_latch():
    skill = PickSocks(LOGGER)
    skill.wire_subskills(_wire)
    skill._cancelled = True
    assert skill.gripper_open.cancelled


def test_wiring_recurses_and_guards_cycles():
    class Outer(Skill):
        inner: PickSocks

        def execute(self):
            pass

        def cancel(self):
            pass

    outer = Outer(LOGGER)
    outer.wire_subskills(_wire)
    assert isinstance(outer.inner.gripper_open, GripperOpen)  # two levels wired

    # a cycle can't be declared via annotations (circular import), so force one
    PickSocks._feed_subskills = {**PickSocks._feed_subskills, "oops": SubSkill(Outer)}
    try:
        with pytest.raises(RuntimeError, match="cycle"):
            Outer(LOGGER).wire_subskills(_wire)
    finally:
        PickSocks._feed_subskills = {k: v for k, v in PickSocks._feed_subskills.items() if k != "oops"}


def test_feed_views_union_over_the_tree():
    class NeedsOdom(Skill):
        from brain_client.skills.odometry import Odometry

        odom: Odometry

        def execute(self):
            pass

        def cancel(self):
            pass

    class Parent(Skill):
        child: NeedsOdom

        def execute(self):
            pass

        def cancel(self):
            pass

    parent = Parent(LOGGER)
    assert parent.declared_robot_state_types() == []  # unwired: children unknown
    parent.wire_subskills(_wire)
    assert RobotStateType.LAST_ODOM in parent.declared_robot_state_types()
    assert RobotStateType.LAST_ODOM in parent.required_robot_state_types()
    assert parent.missing_required_robot_states()  # child's odom still None

    parent.update_robot_state(last_odom={"pose": {}})  # pushed at the parent...
    assert parent.child.odom == {"pose": {}}  # ...lands on the child
    assert parent.missing_required_robot_states() == []


def test_subclass_overrides_by_redeclaring():
    class MyPick(PickSocks):
        gripper_open: FlakyGripper

    assert MyPick._feed_subskills["gripper_open"].skill_class is FlakyGripper
    skill = MyPick(LOGGER)
    skill.wire_subskills(_wire)
    with pytest.raises(SkillFailed):
        skill.execute()


# --- physical skills: declared by id, called the same way ---


class _FakeInvoker:
    """Minimal invoker: knows one policy, records what was run."""

    def __init__(self, known=("pick_socks",), status=SkillResult.SUCCESS):
        self.known = set(known)
        self.status = status
        self.calls = []

    def find(self, skill_id):
        return skill_id if skill_id in self.known else None

    def run(self, skill_id, *, timeout=None, **inputs):
        self.calls.append((skill_id, timeout, inputs))
        if self.status is SkillResult.SUCCESS:
            return "policy done", SkillResult.SUCCESS
        return "policy failed", self.status


class Routine(Skill):
    """Chains a code skill and a trained policy."""

    gripper_open: GripperOpen
    pick_socks = PhysicalSkill("pick_socks")

    def execute(self):
        self.gripper_open()
        return str(self.pick_socks(timeout=60)), SkillResult.SUCCESS

    def cancel(self):
        pass


def _wired_routine(invoker):
    skill = Routine(LOGGER)
    skill.skills = invoker
    skill.wire_subskills(_wire)
    return skill


def test_physical_declaration_is_indexed_separately_from_subskills():
    assert Routine._feed_physical_skills["pick_socks"].skill_id == "pick_socks"
    assert "pick_socks" not in Routine._feed_subskills
    assert "gripper_open" in Routine._feed_subskills


def test_declared_policy_calls_through_the_invoker():
    invoker = _FakeInvoker()
    out, status = _wired_routine(invoker).execute()
    assert status is SkillResult.SUCCESS
    assert out == "policy done"
    assert invoker.calls == [("pick_socks", 60, {})]


def test_policy_failure_raises_like_a_code_child():
    invoker = _FakeInvoker(status=SkillResult.FAILURE)
    with pytest.raises(SkillFailed, match="policy failed"):
        _wired_routine(invoker).execute()


def test_policy_cancellation_raises_skill_cancelled():
    invoker = _FakeInvoker(status=SkillResult.CANCELLED)
    with pytest.raises(SkillCancelled):
        _wired_routine(invoker).execute()


def test_unknown_policy_fails_at_wire_time_not_mid_routine():
    """The whole point of declaring: a typo'd id is caught before the arm moves."""

    class Typo(Skill):
        oops = PhysicalSkill("pick_soks")

        def execute(self):
            pass

        def cancel(self):
            pass

    skill = Typo(LOGGER)
    skill.skills = _FakeInvoker()
    with pytest.raises(RuntimeError, match="no skill with id 'pick_soks'"):
        skill.wire_subskills(_wire)
