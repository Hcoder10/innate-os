# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Shared, cross-cutting brain state.

A handful of flags and references are genuinely shared across the brain's
collaborators (agent loop, lifecycle, skills). Rather than scatter them onto the
node as bare attributes, they live here in one named place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from brain_client.skills.registry import SkillRegistry

if TYPE_CHECKING:
    from brain_client.agents.types import Agent


@dataclass(frozen=True)
class RunningSkill:
    """The skill occupying the execution slot — the robot runs one at a time."""

    primitive_name: str
    skill_id: str
    primitive_id: str | None = None
    manual: bool = False  # a webapp/CLI run the brain didn't start (mirrored, not owned)


@dataclass
class BrainState:
    # --- lifecycle flags ---
    is_brain_active: bool = False
    # MicroInput open for STT → /brain/chat_in without BrainAgent consuming turns.
    is_onboarding_input: bool = False

    # --- runtime-toggleable logging (via /brain/set_logging_config) ---
    log_everything: bool = False

    # --- skill execution (owned by PrimitiveRunner; read by BrainAgent) ---
    primitive_running: RunningSkill | None = None

    # --- skills + directives ---
    registry: SkillRegistry = field(default_factory=SkillRegistry)
    directives: dict = field(default_factory=dict)
    # agent/module name -> load-error text for agents that failed to load;
    # published on get_available_directives so a broken agent shows up in the
    # UI with its error instead of silently vanishing (same contract as
    # SkillCatalog's broken skills).
    broken_agents: dict = field(default_factory=dict)
    current_directive: Agent | None = None
    active_skill_ids: list[str] | None = None
