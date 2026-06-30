from brain_client.agents.types import Agent


class RoutineDemoAgent(Agent):
    """Demo directive showing how one skill can chain several skills in order."""

    @property
    def id(self) -> str:
        return "routine_demo_agent"

    @property
    def display_name(self) -> str:
        return "(Demo) Skill Routine"

    def get_skills(self) -> list[str]:
        return ["innate-os/run_routine_demo"]

    def get_inputs(self) -> list[str]:
        return ["micro"]

    def get_prompt(self) -> str:
        return (
            "You can run a fixed demo routine that chains several skills in a "
            "predictable order. When the user asks to run the routine, call "
            "run_routine_demo."
        )
