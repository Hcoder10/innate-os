#!/usr/bin/env python3
"""
Dynamic Skill Loader

This module provides functionality to dynamically discover and load skill classes
from specified directories. It validates that skills inherit from the Skill base class
and can automatically register them with the execution server.
"""

import json
import logging
import os
from pathlib import Path

import h5py

from brain_client.common.dynamic_loader import DynamicLoader
from brain_client.skills.types import Skill


class SkillLoader(DynamicLoader):
    """
    Dynamically loads skill classes from specified directories.
    """

    base_class = Skill

    def _validate_class(self, skill_class: type[Skill]) -> bool:
        # Check that required abstract methods are implemented
        required_methods = ["name", "execute", "cancel"]
        for method_name in required_methods:
            if not hasattr(skill_class, method_name):
                self.logger.error(f"Skill {skill_class.__name__} missing required method: {method_name}")
                return False

        # Check that name is a property
        if not hasattr(skill_class, "name") or not isinstance(skill_class.name, property):
            self.logger.error(f"Skill {skill_class.__name__} name must be a property")
            return False

        return True

    def _get_name(self, skill_class: type[Skill]) -> str:
        try:
            temp_logger = logging.getLogger(f"temp_{skill_class.__name__}")
            return skill_class(temp_logger).name
        except Exception as e:
            self.logger.debug(f"Could not get name from skill {skill_class.__name__}: {e}")
            return self._fallback_name(skill_class)

    def reload_skill_by_file_stem(self, file_stem: str, directories: list[str]) -> tuple[type[Skill], Path] | None:
        """
        Reload a code skill by its file stem (e.g. 'navigate_to_position').

        Returns:
            (class, source_file) or None if not found.
        """
        for directory in directories:
            py_file = Path(directory) / f"{file_stem}.py"
            if not py_file.exists():
                continue
            try:
                discovered = self.discover_in_file(py_file)
                # Return the first skill found in the file
                for _name, entry in discovered.items():
                    self.logger.info(f"Reloaded skill from {py_file}")
                    return entry
            except Exception as e:
                self.logger.debug(f"Error reloading {py_file}: {e}")

        self.logger.warning(f"Could not find code skill file '{file_stem}.py' in any directory")
        return None

    def validate_physical_skill(self, skill_dir: str, metadata: dict) -> tuple:
        """Validate a physical skill.

        Returns:
            tuple: (is_valid: bool, is_in_training: bool, episode_count: int)
                - is_valid: True if the skill can be loaded (either ready or in training)
                - is_in_training: True if the skill is a learned type missing its checkpoint
                - episode_count: Number of recorded episodes (0 if not applicable or not found)
        """
        skill_type = metadata.get("type", "").lower()
        execution = metadata.get("execution", {})

        if skill_type == "learned":
            is_valid, is_in_training = self._validate_learned_skill(skill_dir, execution)
            episode_count = self._get_episode_count(skill_dir)
            return (is_valid, is_in_training, episode_count)
        elif skill_type == "replay":
            is_valid = self._validate_replay_skill(skill_dir, execution)
            return (
                is_valid,
                False,
                0,
            )  # Replay skills are never "in training", no episodes
        else:
            self.logger.warning(f"Unknown skill type '{skill_type}' in {skill_dir}")
            return (True, False, 0)  # Allow unknown types but log warning

    def _validate_learned_skill(self, skill_dir: str, execution: dict) -> tuple:
        """Validate a learned skill.

        Returns:
            tuple: (is_valid: bool, is_in_training: bool)
        """
        checkpoint_file = execution.get("checkpoint")
        # If no checkpoint specified, it's in training
        if not checkpoint_file:
            self.logger.info(f"Learned skill in {skill_dir} has no checkpoint - marked as in_training")
            return (True, True)  # Valid but in training

        checkpoint_path = os.path.join(skill_dir, checkpoint_file)
        if not os.path.exists(checkpoint_path):
            self.logger.info(f"Learned skill checkpoint not found: {checkpoint_path} - marked as in_training")
            return (True, True)  # Valid but in training

        # Check for stats file (optional but commonly needed)
        stats_file = execution.get("stats_file", "dataset_stats.pt")
        stats_path = os.path.join(skill_dir, stats_file)
        if not os.path.exists(stats_path):
            self.logger.warning(f"Learned skill stats file not found: {stats_path} (optional)")

        self.logger.info(f"Learned skill validation passed: {skill_dir}")
        return (True, False)  # Valid and ready

    def _get_episode_count(self, skill_dir: str) -> int:
        """Get the number of recorded episodes for a learned skill.

        Looks for data/dataset_metadata.json and reads the number_of_episodes field.

        Args:
            skill_dir: Path to the skill directory.

        Returns:
            int: Number of episodes, or 0 if not found.
        """
        dataset_metadata_path = os.path.join(skill_dir, "data", "dataset_metadata.json")

        if not os.path.exists(dataset_metadata_path):
            return 0

        try:
            with open(dataset_metadata_path) as f:
                dataset_metadata = json.load(f)
                return dataset_metadata.get("number_of_episodes", 0)
        except Exception as e:
            self.logger.warning(f"Error reading dataset metadata from {dataset_metadata_path}: {e}")
            return 0

    def _validate_replay_skill_internal(self, skill_dir: str, execution: dict) -> bool:
        """Internal validation for replay skills. Returns bool for validity."""
        replay_file = execution.get("replay_file")
        if not replay_file:
            self.logger.warning(f"Replay skill in {skill_dir} missing replay_file in execution config")
            return False

        replay_path = os.path.join(skill_dir, replay_file)
        if not os.path.exists(replay_path):
            self.logger.warning(f"Replay skill file not found: {replay_path}")
            return False

        # Validate H5 file structure
        try:
            with h5py.File(replay_path, "r") as h5file:
                if "action" not in h5file:
                    self.logger.warning(f"Replay file {replay_path} missing required 'action' dataset")
                    return False

                actions = h5file["action"][:]
                if actions.shape[0] == 0:
                    self.logger.warning(f"Replay file {replay_path} contains no actions")
                    return False

        except Exception as e:
            self.logger.warning(f"Failed to validate replay file {replay_path}: {e}")
            return False

        self.logger.info(f"Replay skill validation passed: {skill_dir}")
        return True

    def _validate_replay_skill(self, skill_dir: str, execution: dict) -> bool:
        """Validate a replay skill. Returns bool for backwards compatibility."""
        return self._validate_replay_skill_internal(skill_dir, execution)
