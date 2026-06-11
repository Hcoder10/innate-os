#!/usr/bin/env python3
"""
Dynamic Agent Loader

This module provides functionality to dynamically discover and load agent classes
from specified directories. It validates that agents inherit from the Agent base class
and can automatically register them with the brain client.
"""

import base64
import os
from pathlib import Path

from brain_client.agents.types import Agent
from brain_client.common.dynamic_loader import DynamicLoader
from brain_client.common.script_paths import classify_source


class AgentLoader(DynamicLoader):
    """
    Dynamically loads agent classes from specified directories.
    """

    base_class = Agent
    name_suffixes = ("Agent", "Directive")

    def _iter_candidate_files(self, directory: Path) -> list[Path]:
        # Look for Python files (excluding __init__.py, types.py, and _-prefixed)
        return [
            f
            for f in directory.glob("*.py")
            if f.name not in ["__init__.py", "types.py"] and not f.name.startswith("_")
        ]

    def _validate_class(self, agent_class: type[Agent]) -> bool:
        """
        Validates that an agent class is properly implemented.

        Args:
            agent_class: The agent class to validate

        Returns:
            True if valid, False otherwise
        """
        try:
            # Check that required abstract methods are implemented
            required_methods = ["id", "display_name", "get_skills", "get_prompt"]
            for method_name in required_methods:
                if not hasattr(agent_class, method_name):
                    self.logger.error(f"Agent {agent_class.__name__} missing required method: {method_name}")
                    return False

            # Check that id is a property
            if not hasattr(agent_class, "id") or not isinstance(agent_class.id, property):
                self.logger.error(f"Agent {agent_class.__name__} id must be a property")
                return False

            # Check that display_name is a property
            if not hasattr(agent_class, "display_name") or not isinstance(agent_class.display_name, property):
                self.logger.error(f"Agent {agent_class.__name__} display_name must be a property")
                return False

            return True

        except Exception as e:
            self.logger.error(f"Error validating agent {agent_class.__name__}: {e}")
            return False

    def _get_name(self, agent_class: type[Agent]) -> str:
        """
        Gets the agent name by creating a temporary instance.
        This is needed because the name is a property that requires instantiation.
        """
        try:
            return agent_class().id
        except Exception as e:
            self.logger.debug(f"Could not get name from agent {agent_class.__name__}: {e}")
            return self._fallback_name(agent_class)

    def reload_agent_by_name(self, agent_name: str, directories: list[str]) -> tuple[type[Agent], Path] | None:
        """
        Reload a specific agent by name from the given directories.

        Args:
            agent_name: The name of the agent to reload
            directories: List of directories to search for the agent

        Returns:
            (class, source_file) for the reloaded agent, or None if not found
        """
        for directory in directories:
            directory_path = Path(directory)
            if not directory_path.exists():
                continue

            for py_file in self._iter_candidate_files(directory_path):
                try:
                    discovered = self.discover_in_file(py_file)
                    if agent_name in discovered:
                        self.logger.info(f"Reloaded agent '{agent_name}' from {py_file}")
                        return discovered[agent_name]
                except Exception as e:
                    self.logger.debug(f"Error checking {py_file} for agent {agent_name}: {e}")

        self.logger.warning(f"Could not find agent '{agent_name}' in any directory")
        return None

    def create_agent_instances(
        self,
        agent_classes: dict[str, tuple[type[Agent], Path]],
        available_skills: dict[str, any] | None = None,
    ) -> dict[str, Agent]:
        """
        Create instances of agent classes.

        Args:
            agent_classes: Dictionary of agent name to (class, source_file) mappings
            available_skills: Optional dictionary of available skill names to validate against

        Returns:
            Dictionary mapping agent names to their instances
        """
        agent_instances = {}

        for agent_name, entry in agent_classes.items():
            agent_class, source_file = entry
            try:
                agent_instance = agent_class()

                # Stamp provenance based on where the file lives on disk.
                agent_instance.source = classify_source(source_file)

                # Load and encode the display icon as base64, resolving the icon
                # path relative to the agent's own source directory.
                self._load_display_icon(agent_instance, str(source_file.parent))

                # Validate skills if available_skills dict is provided
                if available_skills is not None:
                    self._validate_agent_skills(agent_instance, available_skills)

                agent_instances[agent_name] = agent_instance
                self.logger.debug(f"Created agent instance: {agent_name} (source={agent_instance.source})")
            except Exception as e:
                self.logger.error(f"Error creating agent instance {agent_name}: {e}")

        return agent_instances

    def _load_display_icon(self, agent_instance: Agent, agents_directory: str | None) -> None:
        """
        Load and encode the agent's display icon as base64.

        Args:
            agent_instance: The agent instance
            agents_directory: Path to the agents directory
        """
        # Initialize the attribute for storing base64 icon data
        agent_instance.display_icon_data = None

        if not agent_instance.display_icon or not agents_directory:
            return

        icon_path = os.path.join(agents_directory, agent_instance.display_icon)
        if os.path.exists(icon_path):
            try:
                with open(icon_path, "rb") as f:
                    icon_bytes = f.read()
                    agent_instance.display_icon_data = base64.b64encode(icon_bytes).decode("utf-8")
                    self.logger.debug(f"Loaded icon for agent '{agent_instance.id}'")
            except Exception as e:
                self.logger.warning(f"Failed to load icon for agent '{agent_instance.id}': {e}")

    def _validate_agent_skills(self, agent_instance: Agent, available_skills: dict[str, any]) -> None:
        """
        Validates that all skills referenced by an agent have corresponding
        skill files available.

        Args:
            agent_instance: The agent instance to validate
            available_skills: Dictionary of available skill names

        Raises:
            Warning if a skill is not found (logged, not raised)
        """
        try:
            agent_skills = agent_instance.get_skills()
            missing_skills = []

            for skill_name in agent_skills:
                if skill_name not in available_skills:
                    missing_skills.append(skill_name)

            if missing_skills:
                self.logger.warning(
                    f"Agent '{agent_instance.id}' references skills that are not available: "
                    f"{missing_skills}. Available skills: {list(available_skills.keys())}"
                )
            else:
                self.logger.debug(f"Agent '{agent_instance.id}' skills validated successfully: {agent_skills}")
        except Exception as e:
            self.logger.error(f"Error validating skills for agent '{agent_instance.id}': {e}")
