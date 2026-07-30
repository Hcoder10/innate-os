#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Agent Type Definitions

Base class and types for robot agents.
"""

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Union

if TYPE_CHECKING:
    from brain_client.skills.types import Skill

# What get_skills() may list: the Skill class itself (typed — an import error
# or rename is caught by the editor, not at runtime on the robot), or an id
# string. Physical skills are data with no class, so they stay ids.
SkillRef = Union["type[Skill]", str]


class Agent(ABC):
    """
    Base class for all agents.

    An agent provides personality and behavior guidelines for the robot,
    along with the list of skills that should be available when this
    agent is active.
    """

    # Stamped by the loader to "shipped" or "user" based on origin directory.
    # Subclasses must not set this themselves.
    source: str = "user"

    @property
    @abstractmethod
    def id(self) -> str:
        """
        The name of the directive (used as identifier).
        Must be defined by every subclass.
        """
        pass

    @property
    @abstractmethod
    def display_name(self) -> str:
        """
        The human-readable display name of the directive.
        Must be defined by every subclass.
        """
        pass

    @abstractmethod
    def get_skills(self) -> list[SkillRef]:
        """
        Returns the skills that should be available when this agent is
        active. Prefer the Skill class itself for code skills::

            from innate_skills.navigate_to_position import NavigateToPosition

            def get_skills(self):
                return [NavigateToPosition, "local/pick_socks"]

        Id strings (e.g. "innate-os/navigate_to_position") are equivalent —
        and the only form for physical skills, which have no class. Ids are
        matched exactly against each available skill's id during
        registration — not by display name.

        Subclasses must implement this method.
        """
        pass

    def skill_ids(self) -> list[str]:
        """get_skills() normalized to id strings — the only form the rest of
        the system (registration, cloud agent, webapp) ever consumes. A class
        resolves through skill_id_for_class, the same derivation the catalog
        uses to id it, so a class reference and its id are interchangeable."""
        # lazy: keeps this module importable without the skill framework
        from brain_client.skills.types import Skill
        from brain_client.skills.workspace_import import skill_id_for_class

        ids = []
        for ref in self.get_skills():
            if isinstance(ref, str):
                ids.append(ref)
            elif isinstance(ref, type) and issubclass(ref, Skill):
                ids.append(skill_id_for_class(ref))
            else:
                raise TypeError(
                    f"{type(self).__name__}.get_skills() entries must be Skill classes or skill-id strings, got {ref!r}"
                )
        return ids

    @abstractmethod
    def get_prompt(self) -> str | None:
        """
        Returns the prompt/description for this directive.
        This defines the robot's personality and behavior guidelines.

        Subclasses must implement this method.
        """
        pass

    @property
    def display_icon(self) -> str | None:
        """
        Optional path to a 32x32 pixel icon asset for this directive.

        Subclasses can override this property to specify an icon.
        Default: return None (no icon).

        Example:
            return "assets/my_directive_icon.png"
        """
        return None

    def get_inputs(self) -> list[str]:
        """
        Returns a list of input device names that should be active
        when this directive is running.

        Subclasses can override this method to specify required inputs.
        Default: return empty list (no input devices required).

        Example:
            return ["micro", "camera"]
        """
        return []

    def uses_gaze(self) -> bool:
        """
        Whether this agent uses person-tracking gaze.
        When True, the robot will look at detected people during conversation
        and pause gazing during skill execution.

        Subclasses can override to enable gazing.
        Default: False.
        """
        return False
