# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Unit tests for the turn_in_place skill: raw cmd_vel closed on odometry yaw.
Yaw updates are simulated from a background thread while execute() runs."""

import importlib.util
import logging
import threading

from brain_client.skills.types import InterfaceType, SkillResult

_SKILL_FILE = "/home/jetson1/innate-os/workspace/innate_skills/turn_in_place.py"


def _load_skill():
    spec = importlib.util.spec_from_file_location("turn_in_place", _SKILL_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.TurnInPlace(logging.getLogger("test"))


class _Mobility:
    def __init__(self):
        self.cmds = []

    def send_cmd_vel(self, linear_x=0.0, angular_z=0.0, duration=None):
        self.cmds.append((angular_z, duration))


def _rig(start_yaw=0.0):
    skill = _load_skill()
    mobility = _Mobility()
    assert skill.inject_interface(InterfaceType.MOBILITY, mobility)
    skill.odom = {"theta_degrees": start_yaw}
    return skill, mobility


def test_turns_left_until_yaw_covers_angle():
    skill, mobility = _rig(start_yaw=10.0)
    threading.Timer(0.2, lambda: setattr(skill, "odom", {"theta_degrees": 101.0})).start()

    message, status = skill.execute(angle_degrees=90.0, speed=0.5)

    assert status is SkillResult.SUCCESS, message
    assert "left" in message
    moving = [c for c in mobility.cmds if c[0] != 0.0]
    assert moving and all(v > 0 for v, _ in moving)  # CCW commands
    assert all(d == 0.5 for _, d in moving)  # deadman on every pulse
    assert mobility.cmds[-1][0] == 0.0  # explicit stop


def test_negative_angle_turns_right():
    skill, mobility = _rig(start_yaw=0.0)
    threading.Timer(0.2, lambda: setattr(skill, "odom", {"theta_degrees": -91.0})).start()

    message, status = skill.execute(angle_degrees=-90.0)

    assert status is SkillResult.SUCCESS
    assert "right" in message
    assert all(v < 0 for v, _ in mobility.cmds if v != 0.0)


def test_yaw_wraparound_at_180_seam():
    # 170° -> -170° across the seam is a 20° left turn, not 340°
    skill, mobility = _rig(start_yaw=170.0)

    def _cross_seam():
        skill.odom = {"theta_degrees": -169.0}

    threading.Timer(0.2, _cross_seam).start()

    message, status = skill.execute(angle_degrees=20.0)

    assert status is SkillResult.SUCCESS, message
    assert "21 degrees" in message or "20 degrees" in message


def test_cancel_stops_the_base():
    skill, mobility = _rig()
    threading.Timer(0.2, skill.cancel).start()

    _message, status = skill.execute(angle_degrees=720.0)

    assert status is SkillResult.CANCELLED
    assert mobility.cmds[-1][0] == 0.0


def test_no_odometry_fails_cleanly():
    skill, mobility = _rig()
    skill.odom = None

    _message, status = skill.execute(angle_degrees=90.0)

    assert status is SkillResult.FAILURE
    assert mobility.cmds == []  # never commanded motion blind
