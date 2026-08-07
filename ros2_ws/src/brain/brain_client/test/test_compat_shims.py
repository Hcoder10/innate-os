# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The pre-#542 import paths are load-bearing: custom skills and agents on
robots in the field import ``brain_client.skill_types`` and
``brain_client.agent_types``, so the shims must keep resolving and re-export
the *same* class objects — registration via ``__init_subclass__`` and every
isinstance check depend on identity, not just equal names."""

from brain_client import agent_types, skill_types
from brain_client.agents import types as agents_types
from brain_client.skills import types as skills_types


def test_agent_types_shim_reexports_same_objects():
    for name in ("Agent", "SkillRef", "InputRef"):
        assert getattr(agent_types, name) is getattr(agents_types, name)


def test_skill_types_shim_reexports_same_objects():
    for name in (
        "Skill",
        "SkillResult",
        "SkillOutput",
        "SkillCancelled",
        "RobotState",
        "RobotStateType",
        "Interface",
        "InterfaceType",
    ):
        assert getattr(skill_types, name) is getattr(skills_types, name)
