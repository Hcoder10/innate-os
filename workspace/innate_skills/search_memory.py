# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from innate import Skill, SkillOutput, SpatialMemory


class SearchMemory(Skill):
    """Search the robot's long-term memory of places it has seen on this map. Describe the place
    or thing ('the kitchen', 'a banana', 'the door out of this room', 'where you saw my airpods');
    a vision model reviews every remembered view and returns the best match — its image, map
    coordinates, and when it was seen. When asked to find or go to something not in the current
    camera view, or whether you've seen something, search first and drive to the match with
    navigate_to_position (local_frame=false) — don't wander. Never claim you haven't seen or
    don't remember something without searching first. If the search finds nothing, say so and
    only then explore."""

    memory: SpatialMemory

    def execute(self, query: str):
        recall = self.memory.begin(query)
        verdict = self.wait_for(recall, timeout=90.0)
        if verdict is None:
            self.fail("memory search timed out")
        if verdict.error:
            self.fail(verdict.message)
        # A clean no-match is still a successful search — the answer is "nowhere".
        return SkillOutput(verdict.message, image=verdict.image)
