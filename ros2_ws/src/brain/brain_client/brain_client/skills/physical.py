# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Physical-skill validation: learned checkpoints, replay trajectories, episodes.

Physical skills are data (``metadata.json`` + checkpoints/H5), not Python, so
they are read and validated rather than imported — the one part of discovery
the import model does not cover.
"""

import json
import os
from typing import cast

import h5py


def has_physical_metadata(skill_dir) -> bool:
    """True if ``skill_dir`` holds a physical skill's ``metadata.json``.

    Existence alone is not enough: the training client's file lock
    (``skill_manager._locked_metadata``) historically touched 0-byte
    ``metadata.json`` files into arbitrary ``custom_skills/`` subdirs, and a
    crash mid-write can leave one behind. Real metadata is always written
    whole, so an empty file carries no user intent and must read as "absent" —
    everywhere, consistently: the catalog and workspace import both classify
    "data dir vs code package" off this file, so disagreeing on empties would
    either roster phantom broken skills or silently drop a code package from
    import.
    """
    try:
        return os.path.getsize(os.path.join(str(skill_dir), "metadata.json")) > 0
    except OSError:
        return False


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
            - is_in_training: True if the skill's runnable data isn't on disk yet (a
              learned skill's checkpoint, or a replay skill's trajectory)

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
        # A named replay_file that isn't on disk is the not-fetched-yet state, not
        # damage: recording folders ship in git with metadata.json but without the
        # trajectory (see metadata["downloads"]); the catalog writes the ref shim
        # at runtime. Mirror
        # the missing-checkpoint treatment learned skills get — roster it as
        # in_training so the typed ref keeps existing (agents importing it stay
        # loadable) and execution is refused with a reason, not "unknown skill".
        replay_file = execution.get("replay_file")
        if replay_file and not os.path.exists(os.path.join(skill_dir, replay_file)):
            logger.info(
                f"Replay trajectory not on disk yet: {os.path.join(skill_dir, replay_file)} - marked as in_training"
            )
            return (True, True)
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

            # cast: narrows h5py's Group | Dataset | Datatype lookup union
            actions = cast(h5py.Dataset, h5file["action"])[:]
            if actions.shape[0] == 0:
                logger.warning(f"Replay file {replay_path} contains no actions")
                return False

    except Exception as e:
        logger.warning(f"Failed to validate replay file {replay_path}: {e}")
        return False

    logger.info(f"Replay skill validation passed: {skill_dir}")
    return True
