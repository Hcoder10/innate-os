# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Skill-facing head state. ROS-free on purpose."""

from dataclasses import dataclass, field
from typing import Any

from brain_client.skills.dictcompat import LegacyMapping


@dataclass(frozen=True)
class HeadState(LegacyMapping):
    """A head snapshot, read via ``self.head_position`` in skills.

    MARS's head is a single pitch axis; negative pitch looks down.
    """

    pitch_degrees: float
    """Current head pitch in degrees (the topic's ``current_position``)."""
    min_degrees: float | None = None
    """Lowest commandable pitch, if the driver reports it."""
    max_degrees: float | None = None
    """Highest commandable pitch, if the driver reports it."""
    default_degrees: float | None = None
    """The driver's neutral pitch, if it reports one."""
    raw_source: Any = field(default=None, repr=False, compare=False)
    """The original topic payload (a dict) — nothing the driver publishes
    is lost, even keys this snapshot doesn't model. Excluded from ==/hash."""

    # LAST_HEAD_POSITION injected the topic payload dict before ambient state
    _legacy_hint = "the Head attributes (head_position.pitch_degrees, ...)"

    @property
    def _legacy_dict(self) -> dict:
        if self.raw_source is not None:
            return self.raw_source
        return {
            "current_position": self.pitch_degrees,
            "min_angle": self.min_degrees,
            "max_angle": self.max_degrees,
            "default_angle": self.default_degrees,
        }
