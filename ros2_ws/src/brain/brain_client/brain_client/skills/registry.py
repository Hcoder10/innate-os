"""Skill registry — pure name<->id bookkeeping, no ROS.

The cloud agent and the LLM refer to skills sometimes by deterministic *id*
(e.g. ``innate-os/navigate_to_position``) and sometimes by human *name*
(``navigate_to_position``). This module is the single source of truth for that
mapping, replacing three inconsistent inline translations in the old node.

The ROS layer converts an ``AvailableSkills`` message into a list of plain metadata
dicts and calls :meth:`SkillRegistry.from_metadata`; nothing here imports rclpy.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class PrimitiveStub:
    """Lightweight stand-in holding a skill's metadata (no executable body).

    Exposes the ``guidelines`` accessors the registration payload reads.
    """

    def __init__(self, metadata: dict):
        self.metadata = metadata

    def guidelines(self) -> str:
        return self.metadata.get("guidelines", "")

    def guidelines_when_running(self) -> str:
        return self.metadata.get("guidelines_when_running", "")


@dataclass
class SkillRegistry:
    """Immutable-ish view of the currently available skills.

    ``primitives`` is keyed by skill id; ``metadata`` is the ordered list used for
    registration.
    """

    primitives: dict[str, PrimitiveStub] = field(default_factory=dict)
    metadata: list[dict] = field(default_factory=list)
    name_to_id: dict[str, str] = field(default_factory=dict)
    id_to_name: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_metadata(cls, metadata_list: list[dict], on_duplicate=None) -> SkillRegistry:
        """Build a registry from a list of skill metadata dicts.

        Each dict must contain at least ``id`` and ``name``. ``on_duplicate`` is an
        optional callback ``(name, existing_id, new_id)`` invoked when two skills
        share a name (the later one wins, matching prior behaviour).
        """
        primitives: dict[str, PrimitiveStub] = {}
        name_to_id: dict[str, str] = {}
        id_to_name: dict[str, str] = {}

        for meta in metadata_list:
            skill_id = meta["id"]
            name = meta["name"]
            primitives[skill_id] = PrimitiveStub(meta)
            if name in name_to_id and on_duplicate is not None:
                on_duplicate(name, name_to_id[name], skill_id)
            name_to_id[name] = skill_id
            id_to_name[skill_id] = name

        return cls(
            primitives=primitives,
            metadata=list(metadata_list),
            name_to_id=name_to_id,
            id_to_name=id_to_name,
        )

    def resolve_skill_id(self, type_or_name: str) -> str | None:
        """Resolve a task ``type`` (an id) or a skill name to a skill id.

        Resolution order is id-first then name, applied consistently everywhere
        (the old node checked these in different orders at different call sites).
        Returns None if neither matches.
        """
        if type_or_name in self.primitives:
            return type_or_name
        return self.name_to_id.get(type_or_name)

    def name_for(self, skill_id: str) -> str:
        """Cosmetic name for a skill id, falling back to the id itself."""
        return self.id_to_name.get(skill_id, skill_id)

    def __bool__(self) -> bool:
        return bool(self.primitives)
