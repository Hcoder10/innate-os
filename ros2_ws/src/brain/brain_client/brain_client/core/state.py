# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Shared, cross-cutting brain state.

A handful of flags and references are genuinely shared across the brain's
collaborators (agent loop, lifecycle, skills). Rather than scatter them onto the
node as bare attributes, they live here in one named place.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from brain_client.skills.registry import SkillRegistry


@dataclass
class BrainState:
    # --- lifecycle flags ---
    is_brain_active: bool = False

    # --- runtime-toggleable logging (via /brain/set_logging_config) ---
    log_everything: bool = False

    # --- skill execution (owned by PrimitiveRunner; read by BrainAgent) ---
    primitive_running: dict | None = None

    # --- skills + directives ---
    registry: SkillRegistry = field(default_factory=SkillRegistry)
    directives: dict = field(default_factory=dict)
    current_directive: object | None = None
    active_skill_ids: list[str] | None = None
