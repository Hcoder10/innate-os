# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from brain_client.skills.types import Skill, SkillResult


class RunRoutineDemo(Skill):
    """Example: run several skills in a fixed order from a single skill.

    This is how you chain skills predictably -- a learned policy, then a classic
    move, then a replay/mimic, whatever. Just call self.skills.run(id, **inputs)
    per step; each child runs to completion before the next and shows up as its
    own step in the app. Swap in your own skill ids below.

    Note: an orchestrator like this shouldn't declare required robot states of its
    own -- let each child ask for what it needs.
    """

    @property
    def name(self):
        return "run_routine_demo"

    def guidelines(self):
        return (
            "Run a short fixed routine: look excited, nudge forward 30cm, look proud. "
            "Use when the user asks to run the demo routine."
        )

    def execute(self):
        # a routine is just a list of (skill, inputs) we walk in order, bailing the
        # moment one doesn't succeed.
        routine = [
            ("innate-os/head_emotion", {"emotion": "excited"}),
            ("innate-os/navigate_to_position", {"x": 0.3, "y": 0.0, "theta": 0.0, "local_frame": True}),
            ("innate-os/head_emotion", {"emotion": "proud"}),
        ]
        for skill_id, inputs in routine:
            message, status = self.skills.run(skill_id, **inputs)
            if status is not SkillResult.SUCCESS:
                return f"Stopped at {skill_id}: {message}", status
        return "Routine complete", SkillResult.SUCCESS

    def cancel(self):
        # let the invoker stop whichever child is mid-run
        return self.skills.cancel() if self.skills else "Nothing to cancel"
