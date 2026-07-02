# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Unit tests for the move_straight skill: raw cmd_vel closed on odometry,
no Nav2. Odometry is simulated by updating the injected robot state from a
background thread while execute() runs."""

import importlib.util
import logging
import math
import threading
import time

from brain_client.skills.types import InterfaceType, SkillResult

_SKILL_FILE = "/home/jetson1/innate-os/workspace/innate_skills/move_straight.py"


def _load_skill():
    spec = importlib.util.spec_from_file_location("move_straight", _SKILL_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.MoveStraight(logging.getLogger("test"))


class _Mobility:
    def __init__(self):
        self.cmds = []

    def send_cmd_vel(self, linear_x=0.0, angular_z=0.0, duration=None):
        self.cmds.append((linear_x, duration))


def _odom(x, y=0.0):
    return {"pose": {"pose": {"position": {"x": x, "y": y, "z": 0.0}}}}


def _rig(start_x=0.0):
    skill = _load_skill()
    mobility = _Mobility()
    assert skill.inject_interface(InterfaceType.MOBILITY, mobility)
    skill.odom = _odom(start_x)
    return skill, mobility


def test_drives_until_odometry_covers_distance():
    skill, mobility = _rig()
    # simulate the robot arriving after 0.25s of driving
    threading.Timer(0.25, lambda: setattr(skill, "odom", _odom(0.21))).start()

    message, status = skill.execute(distance=0.2, speed=0.2)

    assert status is SkillResult.SUCCESS, message
    assert "forward" in message
    moving = [c for c in mobility.cmds if c[0] != 0.0]
    assert moving and all(v > 0 for v, _ in moving)  # forward commands
    assert all(d == 0.5 for _, d in moving)  # deadman on every pulse
    assert mobility.cmds[-1][0] == 0.0  # explicit stop at the end


def test_negative_distance_drives_backward():
    skill, mobility = _rig()
    threading.Timer(0.25, lambda: setattr(skill, "odom", _odom(-0.21))).start()

    message, status = skill.execute(distance=-0.2, speed=0.2)

    assert status is SkillResult.SUCCESS
    assert "backward" in message
    assert all(v < 0 for v, _ in mobility.cmds if v != 0.0)


def test_cancel_stops_the_base():
    skill, mobility = _rig()
    threading.Timer(0.2, skill.cancel).start()

    message, status = skill.execute(distance=5.0, speed=0.2)

    assert status is SkillResult.CANCELLED
    assert mobility.cmds[-1][0] == 0.0


def test_no_odometry_fails_cleanly():
    skill, mobility = _rig()
    skill.odom = None

    _message, status = skill.execute(distance=0.3)

    assert status is SkillResult.FAILURE
    assert mobility.cmds == []  # never commanded motion blind


def test_speed_is_clamped():
    skill, mobility = _rig()
    threading.Timer(0.2, lambda: setattr(skill, "odom", _odom(0.11))).start()

    _message, status = skill.execute(distance=0.1, speed=9.0)

    assert status is SkillResult.SUCCESS
    fastest = max(abs(v) for v, _ in mobility.cmds)
    assert math.isclose(fastest, 0.3)  # MAX_SPEED
