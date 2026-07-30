# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Unit tests for Agent.input_names(): get_inputs() may list InputDevice
classes (typed) or device-name strings; the input manager only ever consumes
the normalized names, resolved by the same derivation the loader uses."""

import logging

import pytest

from brain_client.agents.types import Agent
from brain_client.inputs.types import InputDevice, input_name_for_class


class OddNameInput(InputDevice):
    """Instance name deliberately differs from the class-derived one."""

    @property
    def name(self):
        return "weird_mic"

    def on_open(self):
        pass

    def on_close(self):
        pass


class ExplodingInput(InputDevice):
    """Instantiation fails — resolution must fall back to the class name."""

    def __init__(self):
        raise RuntimeError("no hardware here")

    @property
    def name(self):
        return "never_reached"

    def on_open(self):
        pass

    def on_close(self):
        pass


class _AgentWith(Agent):
    def __init__(self, inputs):
        self._inputs = inputs

    @property
    def id(self):
        return "test_agent"

    @property
    def display_name(self):
        return "Test Agent"

    def get_skills(self):
        return []

    def get_inputs(self):
        return self._inputs

    def get_prompt(self):
        return ""


def test_class_refs_resolve_to_instance_name():
    # the instance `name` property is the registered identity, not the class name
    agent = _AgentWith([OddNameInput])
    assert agent.input_names() == ["weird_mic"]
    assert input_name_for_class(OddNameInput) == "weird_mic"


def test_uninstantiable_class_falls_back_to_derived_name():
    # same fallback the loader uses: snake_case minus the Input suffix
    agent = _AgentWith([ExplodingInput])
    assert agent.input_names() == ["exploding"]


def test_strings_pass_through_and_mix_with_classes():
    agent = _AgentWith(["micro", OddNameInput])
    assert agent.input_names() == ["micro", "weird_mic"]


def test_non_input_entry_raises():
    agent = _AgentWith([logging.Logger])  # a class, but not an InputDevice
    with pytest.raises(TypeError, match="InputDevice classes"):
        agent.input_names()
    agent = _AgentWith([42])
    with pytest.raises(TypeError, match="InputDevice classes"):
        agent.input_names()
