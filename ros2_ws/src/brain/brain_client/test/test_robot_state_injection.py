# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Unit tests for robot-state injection of the tier-1 sensor states: joint
states and battery, declared via RobotState descriptors like odom."""

import logging
from types import SimpleNamespace

from sensor_msgs.msg import BatteryState

from brain_client.skills.robot_state import RobotStateProvider
from brain_client.skills.types import RobotState, RobotStateType, Skill, SkillResult


class _Logger:
    def info(self, *a, **k):
        pass

    error = warning = warn = debug = info


class Proprioceptive(Skill):
    joints = RobotState(RobotStateType.LAST_JOINT_STATES)
    battery = RobotState(RobotStateType.LAST_BATTERY)

    @property
    def name(self):
        return "proprioceptive"

    def execute(self):
        return "ok", SkillResult.SUCCESS


def _provider():
    provider = RobotStateProvider.__new__(RobotStateProvider)
    provider._logger = _Logger()
    provider.last_joint_states = None
    provider.last_battery = None
    return provider


def test_joint_states_and_battery_injected():
    provider = _provider()
    provider.last_joint_states = SimpleNamespace(
        name=["shoulder", "elbow"], position=[0.1, -0.5], velocity=[0.0, 0.0], effort=[1.2, 0.8]
    )
    provider.last_battery = SimpleNamespace(
        percentage=0.87, voltage=24.1, current=-1.4, power_supply_status=BatteryState.POWER_SUPPLY_STATUS_CHARGING
    )
    skill = Proprioceptive(logging.getLogger("test"))

    provider.update_skill_robot_state(skill)

    assert skill.joints["name"] == ["shoulder", "elbow"]
    assert skill.joints["position"] == [0.1, -0.5]
    assert skill.battery["percentage"] == 0.87
    assert skill.battery["charging"] is True


def test_missing_sensor_data_injects_nothing():
    provider = _provider()
    skill = Proprioceptive(logging.getLogger("test"))

    provider.update_skill_robot_state(skill)

    assert skill.joints is None and skill.battery is None
