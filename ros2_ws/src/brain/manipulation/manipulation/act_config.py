# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""ROS-free factory for the ACT inference config.

Kept separate from manipulation_server so the offline engine pre-build
(`python -m manipulation.act_trt`, spawned by innate_training_node) can build
the exact same architecture without importing the ROS node (rclpy, cv_bridge,
mars_msgs, ...). manipulation_server re-exports this for its own use.
"""

import torch

from manipulation.ACT import ACTConfig
from manipulation.config_validation import DEFAULT_ACTION_DIM

# chunk_size is the row count of the decoder positional embedding in the checkpoint.
_CHUNK_SIZE_KEY = "model.decoder_pos_embed.weight"

# action_dim is the output width of the action head (nn.Linear(dim_model, action_dim)).
_ACTION_HEAD_KEY = "model.action_head.weight"


def load_torch_file(path, mmap=False, log=None):
    """``torch.load`` onto CPU with a ``weights_only=True`` fast path and a fallback.

    ``weights_only=True`` is faster/safer for a pure tensor state_dict but rejects
    checkpoints whose outer dict holds non-basic types (a training config dataclass,
    enum, numpy scalar, ...). Fall back to the unrestricted loader rather than failing
    the load opaquely. Shared by the runtime load (manipulation_server) and the offline
    engine pre-build (act_trt) so checkpoint and dataset_stats loads behave identically
    regardless of the torch default (>=2.6 defaults weights_only=True). ``log``, if given,
    is called with a message when the fast path fails and we retry.
    """
    try:
        return torch.load(path, map_location="cpu", weights_only=True, mmap=mmap)
    except Exception as e:  # noqa: BLE001 - fall back to the unrestricted loader
        if log is not None:
            log(f"weights_only load failed ({e}); retrying with weights_only=False")
        return torch.load(path, map_location="cpu", weights_only=False, mmap=mmap)


def normalize_state_dict(state_dict):
    """Unwrap common checkpoint containers and strip torch.compile/DDP key prefixes.

    Shared by the runtime load (manipulation_server) and the offline engine pre-build
    (act_trt._main) so both infer the same architecture from the same checkpoint --
    otherwise a wrapped or compiled checkpoint that the server loads fine makes the
    pre-build raise a bare KeyError on the missing un-prefixed key.
    """
    if isinstance(state_dict, dict) and "model_state_dict" in state_dict:
        state_dict = state_dict["model_state_dict"]
    elif isinstance(state_dict, dict) and "state_dict" in state_dict:
        state_dict = state_dict["state_dict"]

    if any(k.startswith(("_orig_mod.", "module.")) for k in state_dict):
        cleaned = {}
        for key, value in state_dict.items():
            # Handle nested cases like module._orig_mod. by stripping both, in order.
            if key.startswith("module."):
                key = key.replace("module.", "", 1)
            if key.startswith("_orig_mod."):
                key = key.replace("_orig_mod.", "", 1)
            cleaned[key] = value
        state_dict = cleaned
    return state_dict


def infer_chunk_size(state_dict, default=30):
    """Infer chunk_size from the checkpoint's decoder positional embedding.

    Falls back to ``default`` when the key is absent so the runtime and the offline
    pre-build behave identically (both build/load an engine for the same chunk_size)
    instead of one raising and the other silently defaulting.
    """
    if _CHUNK_SIZE_KEY in state_dict:
        return state_dict[_CHUNK_SIZE_KEY].shape[0]
    return default


def infer_action_dim(state_dict):
    """Infer the checkpoint's action_dim from its action-head weight, or None if absent.

    The action head is nn.Linear(dim_model, action_dim), so its weight has shape
    (action_dim, dim_model). Used to validate the caller-supplied action_dim (from skill
    metadata) against the checkpoint before an engine is built: with load_state_dict's
    assign=True a width mismatch is adopted/exported silently and baked into the cached
    engine, so the check has to come from the weights, not the load result.
    """
    weight = state_dict.get(_ACTION_HEAD_KEY)
    return weight.shape[0] if weight is not None else None


def validate_action_dim(state_dict, action_dim, checkpoint_path=""):
    """Raise if the caller-supplied action_dim disagrees with the checkpoint.

    No-op when the checkpoint has no action-head key (older/wrapped formats). Shared by
    the runtime load and the offline pre-build so a metadata/checkpoint mismatch fails
    loudly in both places instead of caching an engine that emits wrong-width commands.
    """
    ckpt_action_dim = infer_action_dim(state_dict)
    if ckpt_action_dim is not None and ckpt_action_dim != action_dim:
        where = f" {checkpoint_path}" if checkpoint_path else ""
        raise ValueError(
            f"action_dim mismatch: configured action_dim={action_dim} but checkpoint{where} "
            f"has a {ckpt_action_dim}-wide action head. Fix the skill's execution.action_dim "
            f"(or re-export the checkpoint); refusing to build an engine that would emit "
            f"wrong-width arm/base commands."
        )


def create_act_config(
    action_dim=DEFAULT_ACTION_DIM, chunk_size=30, n_action_steps=None, speed=1.5, temporal_ensemble_coeff=None
):
    """ACT config matching the training setup.

    n_action_steps is the replanning horizon; defaults to min(40, chunk_size) and
    is clamped to [1, chunk_size], the range ACTConfig requires (a 0/negative
    override would otherwise raise opaquely deep in the constructor).

    When temporal_ensemble_coeff is set, the horizon is pinned to 1 (the only value
    ACT temporal ensembling allows), overriding n_action_steps.
    """
    if n_action_steps is None:
        n_action_steps = min(40, chunk_size)
    n_action_steps = max(1, min(n_action_steps, chunk_size))
    if temporal_ensemble_coeff is not None:
        n_action_steps = 1

    return ACTConfig(
        n_obs_steps=1,
        chunk_size=chunk_size,
        n_action_steps=n_action_steps,
        speed=speed,
        input_shapes={
            "observation.image_camera_1": [3, 224, 224],
            "observation.image_camera_2": [3, 224, 224],
            "observation.state": [6],
        },
        output_shapes={"action": [action_dim]},
        vision_backbone="resnet18",
        replace_final_stride_with_dilation=False,
        pre_norm=False,
        dim_model=512,
        n_heads=8,
        dim_feedforward=3200,
        n_encoder_layers=4,
        n_decoder_layers=4,
        use_vae=True,
        dropout=0.1,
        kl_weight=10.0,
        temporal_ensemble_coeff=temporal_ensemble_coeff,
        optimizer_lr=1e-5,
        optimizer_weight_decay=1e-4,
        optimizer_lr_backbone=1e-5,
    )
