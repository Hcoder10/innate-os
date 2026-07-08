# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from brain_client.agents.types import Agent


class BasicAgent(Agent):
    """
    Default directive for the robot.
    Provides a basic professional personality and enables navigation primitives.
    """

    @property
    def id(self) -> str:
        return "basic_agent"

    @property
    def display_name(self) -> str:
        return "Basic Navigation"

    def get_skills(self) -> list[str]:
        """Return the list of skill IDs this directive can use"""
        return ["innate-os/navigate_to_position", "innate-os/navigate_with_vision", "innate-os/go_to_sleep"]

    def get_inputs(self) -> list[str]:
        """Enable microphone input to hear user"""
        return ["micro"]

    def get_prompt(self) -> str:
        return ""
