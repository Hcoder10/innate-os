# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from brain_client.agent_types import Agent


class EmptyDirective(Agent):
    """Idle directive used when no behavior agent has been selected."""

    @property
    def id(self) -> str:
        return "empty_directive"

    @property
    def display_name(self) -> str:
        return "No Prompt"

    def get_skills(self) -> list[str]:
        return []

    def get_prompt(self) -> str:
        return ""
