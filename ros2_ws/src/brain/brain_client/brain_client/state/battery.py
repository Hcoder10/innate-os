# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Skill-facing battery state. ROS-free on purpose."""

from dataclasses import dataclass

from brain_client.state.dictcompat import LegacyMapping


@dataclass(frozen=True)
class Battery(LegacyMapping):
    """A battery snapshot, read via ``self.battery`` in skills."""

    percentage: float
    """State of charge, 0.0-1.0."""
    voltage: float
    """Pack voltage in volts."""
    current: float
    """Pack current in amps."""
    charging: bool
    """True while the charger reports charging."""

    # --- legacy dict compatibility ---------------------------------------
    # LAST_BATTERY injected this dict through 0.6.x; the LegacyMapping mixin
    # keeps that access working (see dictcompat.py). Do not delete.

    _legacy_hint = "the Battery attributes (battery.percentage, battery.charging, ...)"

    @property
    def _legacy_dict(self) -> dict:
        """Exactly the 0.3.0-0.6.x injected shape — the same four fields."""
        return {
            "percentage": self.percentage,
            "voltage": self.voltage,
            "current": self.current,
            "charging": self.charging,
        }
