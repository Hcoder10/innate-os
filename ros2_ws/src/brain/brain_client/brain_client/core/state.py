"""Shared, cross-cutting brain state.

A handful of flags and references are genuinely shared across the brain's
collaborators (orchestrator, lifecycle, skills, vision-output). Rather than
scatter them back onto the node as bare attributes, they live here in one named
place. Each collaborator receives this object and reads/updates the fields it owns.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from brain_client.skills.registry import SkillRegistry


@dataclass
class BrainState:
    # --- lifecycle flags ---
    is_brain_active: bool = False
    ready_for_image: bool = False
    primitives_registered: bool = False
    pose_image_started: bool = False

    # --- runtime-toggleable logging (via /brain/set_logging_config) ---
    log_everything: bool = False

    # --- registration / reconnection ---
    token: str = ""
    pending_reregistration: bool = False

    # --- skills + directives ---
    registry: SkillRegistry = field(default_factory=SkillRegistry)
    directives: dict = field(default_factory=dict)
    current_directive: object | None = None

    # --- pose stamping for local-nav compensation ---
    pose_at_image_send: tuple[float, float, float] | None = None

    # --- memory positions from the cloud agent posegraph ---
    memory_positions: list = field(default_factory=list)
