# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate_skills.navigate_to_position import NavigateToPosition
from innate_skills.navigate_with_vision import NavigateWithVision
from physical_skills import Wave

from brain_client.agents.types import Agent


class DemoAgent(Agent):
    """
    Demo agent - a friendly and curious robot assistant named Mars.
    """

    @property
    def id(self) -> str:
        return "demo_agent"

    @property
    def display_name(self) -> str:
        return "Demo Agent"

    def get_skills(self) -> list:
        """Navigation code skills plus the recorded wave — Wave comes from the
        generated physical_skills package (see skills/physical_refs.py)."""
        return [NavigateToPosition, Wave, NavigateWithVision]

    def get_inputs(self) -> list[str]:
        """Enable microphone input to hear user"""
        return ["micro"]

    def get_prompt(self) -> str:
        """Return the prompt that defines the robot's personality and behavior"""
        return """You are Mars, a friendly and curious robot assistant. Keep responses concise and conversational. You can see through a camera and use tools to wave, move, and interact. Greet people warmly when you see them! IMPORTANT: If the user says 'stop' or interrupts you during an action, STOP immediately, and do NOT retry or call the tool again. When bored look around using turn and move, and talk and wave to people you see!"""

    def uses_gaze(self) -> bool:
        """Enable person-tracking gaze during conversation."""
        return True
