# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Unit tests for Agent.skill_ids(): get_skills() may list Skill classes
(typed) or id strings; the framework only ever consumes the normalized ids,
so the cloud registration wire format never sees a class."""

import logging

import pytest

from brain_client.agents.types import Agent
from brain_client.skills.types import Skill
from brain_client.skills.workspace_import import skill_id_for_class


class NavStub(Skill):
    """A stand-in code skill."""

    def execute(self):
        return "done"


class _AgentWith(Agent):
    def __init__(self, skills):
        self._skills = skills

    @property
    def id(self):
        return "test_agent"

    @property
    def display_name(self):
        return "Test Agent"

    def get_skills(self):
        return self._skills

    def get_prompt(self):
        return ""


def test_class_refs_resolve_to_catalog_ids():
    agent = _AgentWith([NavStub])
    assert agent.skill_ids() == [skill_id_for_class(NavStub)]
    # the derivation is <namespace>/<snake_case(ClassName)> — the same id the
    # catalog rosters the class under
    assert skill_id_for_class(NavStub).endswith("/nav_stub")


def test_strings_pass_through_and_mix_with_classes():
    agent = _AgentWith(["local/pick_socks", NavStub, "innate-os/wave"])
    assert agent.skill_ids() == ["local/pick_socks", skill_id_for_class(NavStub), "innate-os/wave"]


def test_non_skill_entry_raises():
    agent = _AgentWith([logging.Logger])  # a class, but not a Skill
    with pytest.raises(TypeError, match="Skill classes"):
        agent.skill_ids()
    agent = _AgentWith([42])
    with pytest.raises(TypeError, match="Skill classes"):
        agent.skill_ids()
