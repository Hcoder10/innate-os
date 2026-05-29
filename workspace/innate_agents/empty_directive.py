from typing import List, Optional

from brain_client.agent_types import Agent


class EmptyDirective(Agent):
    """Idle directive used when no behavior agent has been selected."""

    @property
    def id(self) -> str:
        return "empty_directive"

    @property
    def display_name(self) -> str:
        return "No Prompt"

    def get_skills(self) -> List[str]:
        return []

    def get_prompt(self) -> Optional[str]:
        return None
