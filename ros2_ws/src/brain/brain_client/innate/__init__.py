# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Public authoring namespace for Innate skills.

Everything a skill file needs, under one import:

    from innate import MainImage, Mobility, Skill
    from innate_skills.gripper_open import GripperOpen

    class WaveAtCamera(Skill):
        \"\"\"Wave the arm at whoever the camera sees.\"\"\"

        mobility: Mobility
        image: MainImage

        def execute(self):
            ...

The class docstring is the agent-facing guidelines, the class name is the
skill name (snake_cased), and everything the skill consumes is declared with
a bare type annotation — the type identifies the feed.

One rule covers interfaces, cameras and robot state: annotate what you read.
``battery: Battery``, ``odom: Odometry``, ``pose: Pose``, ``lidar: Lidar``,
``arm: Arm``, ``map: Map``, ``joint_states: JointStates``,
``head_position: HeadState``, ``image: MainImage`` / ``WristImage`` /
``DepthMap``, ``mobility: Mobility``, ``head: Head``. A plain annotation is
guaranteed inside execute() — the server waits for the first value and fails
the run up front if none arrives — so no None guards are needed; ``| None``
(``head: Head | None``) makes it best effort instead, injected when available
and None otherwise. Reading an undeclared feed raises, and your editor flags
it before you ship.
"""

from typing import TYPE_CHECKING

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
    PhysicalSkill,
    Skill,
    SkillCancelled,
    SkillFailed,
    SkillOutput,
    SkillResult,
    SkillReturn,
    resource,
)

__all__ = [
    "Arm",
    "PhysicalSkill",
    "Battery",
    "DepthMap",
    "Head",
    "HeadState",
    "Image",
    "JointStates",
    "Lidar",
    "MainImage",
    "Manipulation",
    "Map",
    "Mobility",
    "Odometry",
    "Pose",
    "Skill",
    "SkillCancelled",
    "SkillFailed",
    "SkillOutput",
    "SkillResult",
    "SkillReturn",
    "WristImage",
    "resource",
]

# The interface classes pull ROS/Nav2 modules, so they resolve lazily
# (PEP 562): `from innate import Mobility` imports them on first use only.
# Type checkers can't follow __getattr__, so they read the aliases below —
# the names, and the classes behind them, are identical either way.
if TYPE_CHECKING:
    from brain_client.robot.head import HeadInterface as Head
    from brain_client.robot.manipulation import ManipulationInterface as Manipulation
    from brain_client.robot.mobility import MobilityInterface as Mobility

_LAZY_INTERFACES = {
    "Mobility": ("brain_client.robot.mobility", "MobilityInterface"),
    "Manipulation": ("brain_client.robot.manipulation", "ManipulationInterface"),
    "Head": ("brain_client.robot.head", "HeadInterface"),
}


def __getattr__(name: str):
    target = _LAZY_INTERFACES.get(name)
    if target is None:
        raise AttributeError(f"module 'innate' has no attribute {name!r}")
    module_name, class_name = target
    import importlib

    return getattr(importlib.import_module(module_name), class_name)
