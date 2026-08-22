# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
from typing import Literal, cast

from innate import Head, HeadState, Skill, SkillReturn

# Each pose is (angle_degrees, duration_seconds). Duration is the time to
# interpolate from the previous pose to this one.
EMOTIONS = {
    "happy": {
        "description": "Quick upward nods",
        "sequence": [(5, 0.12), (-5, 0.12), (10, 0.15), (-5, 0.12), (10, 0.15), (0, 0.18)],
    },
    "very_happy": {
        "description": "A long burst of nods that starts eager and winds down",
        # ~5s: eight fast nods, then the same nod stretched longer and longer
        # until it settles level — delight running out of steam, not stopping.
        "sequence": [
            (15, 0.09),
            (-10, 0.09),
            (15, 0.09),
            (-10, 0.09),
            (15, 0.09),
            (-10, 0.09),
            (15, 0.10),
            (-10, 0.10),
            (15, 0.13),
            (-10, 0.13),
            (15, 0.15),
            (-10, 0.15),
            (15, 0.17),
            (-10, 0.17),
            (15, 0.20),
            (-10, 0.20),
            (15, 0.24),
            (-10, 0.24),
            (15, 0.28),
            (-10, 0.28),
            (15, 0.32),
            (-10, 0.32),
            (12, 0.36),
            (-8, 0.40),
            (0, 0.45),
        ],
    },
    "sad": {
        "description": "Slow droop downward",
        "sequence": [(0, 0.3), (-5, 0.35), (-10, 0.4), (-15, 0.4), (-20, 0.45), (-25, 0.5), (-25, 0.3)],
    },
    "excited": {
        "description": "Rapid enthusiastic bouncing",
        "sequence": [(10, 0.08), (-10, 0.08), (15, 0.1), (-15, 0.1), (10, 0.08), (-10, 0.08), (15, 0.1), (0, 0.12)],
    },
    "thinking": {
        "description": "Slow tilt up, pause, slight nod down",
        "sequence": [(5, 0.3), (10, 0.35), (15, 0.4), (15, 0.6), (15, 0.6), (10, 0.3), (5, 0.25), (-5, 0.2), (0, 0.3)],
    },
    "disappointed": {
        "description": "Slow shake-like droop then hold",
        "sequence": [(0, 0.25), (-5, 0.3), (-3, 0.2), (-10, 0.3), (-8, 0.2), (-15, 0.35), (-20, 0.4), (-20, 0.3)],
    },
    "surprised": {
        "description": "Quick jolt up then settle",
        "sequence": [(15, 0.08), (15, 0.15), (10, 0.15), (5, 0.15), (0, 0.2)],
    },
    "confused": {
        "description": "Tilt up, down, up hesitantly",
        "sequence": [(5, 0.2), (-5, 0.25), (8, 0.25), (-8, 0.25), (3, 0.2), (-3, 0.2), (0, 0.25)],
    },
    "angry": {
        "description": "Sharp downward jab, hold low, return",
        "sequence": [(-10, 0.1), (-25, 0.1), (-25, 0.2), (-25, 0.2), (-15, 0.15), (-5, 0.15), (0, 0.18)],
    },
    "sleepy": {
        "description": "Gradual droop with small recovery bobs",
        "sequence": [
            (0, 0.4),
            (-5, 0.5),
            (-3, 0.3),
            (-10, 0.5),
            (-8, 0.3),
            (-15, 0.5),
            (-12, 0.3),
            (-20, 0.5),
            (-25, 0.6),
            (-25, 0.4),
        ],
    },
    "proud": {
        "description": "Slow confident rise and hold high",
        "sequence": [(0, 0.25), (5, 0.3), (10, 0.3), (15, 0.35), (15, 0.4), (15, 0.4), (10, 0.3), (5, 0.25), (0, 0.25)],
    },
    "agreeing": {
        "description": "Nodding yes",
        "sequence": [(-10, 0.15), (5, 0.15), (-10, 0.15), (5, 0.15), (-10, 0.15), (5, 0.15), (0, 0.2)],
    },
    "disagreeing": {
        "description": "Slow deliberate shake no via tilt",
        "sequence": [(-5, 0.18), (5, 0.18), (-8, 0.2), (8, 0.2), (-5, 0.18), (5, 0.18), (0, 0.2)],
    },
}
EmotionName = Literal[
    "happy",
    "very_happy",
    "sad",
    "excited",
    "thinking",
    "disappointed",
    "surprised",
    "confused",
    "angry",
    "sleepy",
    "proud",
    "agreeing",
    "disagreeing",
]

INTERPOLATION_RATE_HZ = 30.0


class HeadEmotion(Skill):
    """Express an emotion through head tilt movements."""

    head: Head
    head_position: HeadState | None

    def guidelines(self) -> str:
        return (
            "Express an emotion through head tilt movements. Requires 'emotion' "
            f"parameter, one of: {', '.join(repr(name) for name in EMOTIONS)}. "
            "Optionally pass 'repeat' (int, default 1) to loop the animation."
        )

    def execute(self, emotion: EmotionName, repeat: int = 1) -> SkillReturn:
        emotion = cast(EmotionName, emotion.strip().lower())
        if emotion not in EMOTIONS:
            self.fail(f"Unknown emotion '{emotion}'. Available: {', '.join(sorted(EMOTIONS))}")

        repeat = max(1, min(int(repeat), 5))
        entry = EMOTIONS[emotion]
        self.feedback(f"Expressing: {emotion}")

        # Offsets ride on the current tilt so the nod happens where Mars is looking;
        # the arm clamps out-of-range commands.
        state = self.head_position
        base_angle = 0 if state is None else round(state.pitch_degrees)
        dt = 1.0 / INTERPOLATION_RATE_HZ
        try:
            for r in range(repeat):
                current_offset = 0.0
                for target_offset, duration in entry["sequence"]:
                    steps = max(1, int(round(duration * INTERPOLATION_RATE_HZ)))
                    for i in range(1, steps + 1):
                        offset = current_offset + (target_offset - current_offset) * i / steps
                        self.head.set_position(round(base_angle + offset))
                        self.sleep(dt)
                    current_offset = float(target_offset)
                if r < repeat - 1:
                    self.sleep(0.2)
        finally:
            self.head.set_position(base_angle)

        return f"Expressed '{emotion}' ({entry['description']})"
