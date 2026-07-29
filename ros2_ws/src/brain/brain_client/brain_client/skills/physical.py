# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Physical-skill validation: learned checkpoints, replay trajectories, episodes.

Physical skills are data (``metadata.json`` + checkpoints/H5), not Python, so
they are read and validated rather than imported — the one part of discovery
the import model does not cover.
"""

import json
import os

import h5py


def shutdown_quietly(instance, logger) -> None:
    """Shut down a throwaway skill instance; log rather than raise."""
    try:
        instance.shutdown()
    except Exception as e:  # noqa: BLE001 — teardown must not mask the real error
        logger.debug(f"Error shutting down temp {type(instance).__name__} instance: {e}")


def validate_physical_skill(skill_dir: str, metadata: dict, logger) -> tuple:
    """Validate a physical skill.

    Returns:
        tuple: (is_valid: bool, is_in_training: bool)
            - is_valid: True if the skill can be loaded (either ready or in training)
            - is_in_training: True if the skill is a learned type missing its checkpoint

    Episode counts are deliberately NOT part of validation: episodes accumulate
    while a skill trains, so the roster re-reads them at publish time
    (catalog._build_physical_skill_info -> get_episode_count).
    """
    skill_type = metadata.get("type", "").lower()
    execution = metadata.get("execution", {})

    if skill_type == "learned":
        return _validate_learned_skill(skill_dir, execution, logger)
    elif skill_type == "eval":
        # A rollout-capture dataset: always valid, never "in training" (it has
        # no checkpoint and is never trained).
        return (True, False)
    elif skill_type == "replay":
        # A replay draft (created up front, still being recorded into data/) has no
        # replay_file yet — treat it as in_training rather than invalid so it loads
        # cleanly until the take is saved and the trajectory is written.
        if not execution.get("replay_file") and os.path.isdir(os.path.join(skill_dir, "data")):
            return (True, True)
        # Replay skills are never "in training"
        return (_validate_replay_skill(skill_dir, execution, logger), False)
    else:
        logger.warning(f"Unknown skill type '{skill_type}' in {skill_dir}")
        return (True, False)  # Allow unknown types but log warning


def _validate_learned_skill(skill_dir: str, execution: dict, logger) -> tuple:
    """Validate a learned skill.

    Returns:
        tuple: (is_valid: bool, is_in_training: bool)
    """
    checkpoint_file = execution.get("checkpoint")
    # If no checkpoint specified, it's in training
    if not checkpoint_file:
        logger.info(f"Learned skill in {skill_dir} has no checkpoint - marked as in_training")
        return (True, True)  # Valid but in training

    checkpoint_path = os.path.join(skill_dir, checkpoint_file)
    if not os.path.exists(checkpoint_path):
        logger.info(f"Learned skill checkpoint not found: {checkpoint_path} - marked as in_training")
        return (True, True)  # Valid but in training

    # Check for stats file (optional but commonly needed)
    stats_file = execution.get("stats_file", "dataset_stats.pt")
    stats_path = os.path.join(skill_dir, stats_file)
    if not os.path.exists(stats_path):
        logger.warning(f"Learned skill stats file not found: {stats_path} (optional)")

    logger.info(f"Learned skill validation passed: {skill_dir}")
    return (True, False)  # Valid and ready


def get_episode_count(skill_dir: str, logger) -> int:
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
        logger.warning(f"Error reading dataset metadata from {dataset_metadata_path}: {e}")
        return 0


def _validate_replay_skill(skill_dir: str, execution: dict, logger) -> bool:
    """Validate a replay skill's trajectory file. Returns bool for validity."""
    replay_file = execution.get("replay_file")
    if not replay_file:
        logger.warning(f"Replay skill in {skill_dir} missing replay_file in execution config")
        return False

    replay_path = os.path.join(skill_dir, replay_file)
    if not os.path.exists(replay_path):
        logger.warning(f"Replay skill file not found: {replay_path}")
        return False

    # Validate H5 file structure
    try:
        with h5py.File(replay_path, "r") as h5file:
            if "action" not in h5file:
                logger.warning(f"Replay file {replay_path} missing required 'action' dataset")
                return False

            actions = h5file["action"][:]
            if actions.shape[0] == 0:
                logger.warning(f"Replay file {replay_path} contains no actions")
                return False

    except Exception as e:
        logger.warning(f"Failed to validate replay file {replay_path}: {e}")
        return False

    logger.info(f"Replay skill validation passed: {skill_dir}")
    return True
