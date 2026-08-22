# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
import threading

from innate_skills.come_here import ComeHere
from innate_skills.person_tracking import _PersonTrackingSkill
from innate_skills.short_clap import ShortClap

from innate import SkillCancelled, SkillReturn

HAPPY_NOD = ((3, 0.12), (-3, 0.12), (5, 0.15), (-3, 0.12), (5, 0.15), (0, 0.18))
HAPPY_NOD_SECONDS = sum(duration for _offset, duration in HAPPY_NOD)
SPEECH_WORDS_PER_SECOND = 2.5
CLAP_SPEECH_FRACTION = 0.7


class ComplimentPerson(_PersonTrackingSkill):
    """Face a visible person, point and beckon with a happy nod, then speak a
    supplied visual compliment."""

    come_here: ComeHere
    short_clap: ShortClap

    def guidelines(self) -> str:
        return (
            "Use for a genuine compliment about a clearly visible detail on a person. "
            "Pass the complete spoken compliment, beginning with 'Hey you' and naming only "
            "details that are clearly visible. The skill locks onto a face, points, beckons, "
            "speaks, and claps near the end of the compliment."
        )

    def execute(self, compliment: str) -> SkillReturn:
        compliment = compliment.strip()
        if not compliment:
            self.fail("A spoken compliment is required")

        locked = self._center_face()
        if locked is None:
            self.say("I cannot find a face to point toward.")
            self.fail("Could not center a face before complimenting")
        gaze_angle, _face = locked
        self.say("Hey, you! Come a little closer.")
        nod = threading.Thread(target=self._happy_nod, args=(gaze_angle,), daemon=True)
        nod.start()
        try:
            self.come_here(timeout=30.0)
        finally:
            nod.join(timeout=2.0)

        self.say(compliment)
        self._happy_nod(gaze_angle)
        self.sleep(self._clap_delay(compliment))
        self.short_clap(timeout=30.0)
        return "Pointed, beckoned, and delivered the compliment"

    @staticmethod
    def _clap_delay(compliment: str) -> float:
        speech_seconds = len(compliment.split()) / SPEECH_WORDS_PER_SECOND
        clap_at = speech_seconds * CLAP_SPEECH_FRACTION
        return max(0.0, clap_at - HAPPY_NOD_SECONDS)

    def _happy_nod(self, gaze_angle: int) -> None:
        try:
            for offset, duration in HAPPY_NOD:
                self.head.set_position(max(-25, min(15, gaze_angle + offset)))
                self.sleep(duration)
        except SkillCancelled:
            pass
        finally:
            self.head.set_position(gaze_angle)
