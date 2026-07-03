# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Validation for per-behavior execution configs stored in a skill's metadata.json.

Parses and validates the ``behavior_config`` payload that
``manipulation_server`` receives on every ``/behavior/execute`` goal. The
single source of truth for defaults lives here, so the rest of
``manipulation_server`` can operate on typed, already-validated config
objects instead of scattering ``dict.get(key, default)`` calls and ad hoc
type checks through every execution path.

Contract:

- Happy path: ``validate_behavior_config`` returns a :class:`ValidatedBehavior`
  whose ``params`` is one of :class:`LearnedExecCfg` / :class:`PosesExecCfg`
  / :class:`ReplayExecCfg`, and whose ``resolved_path`` (for learned /
  replay skills) is the absolute path to the file referenced by
  ``execution.checkpoint`` / ``execution.replay_file``.
- Any failure (bad JSON, unknown type, missing/wrong-type/out-of-bounds
  field, missing file on disk) raises :class:`BehaviorConfigError` with a
  human-readable message prefixed by ``execution.<field>`` where
  applicable.

This module is deliberately ROS-free so it can be unit-tested without a
ROS environment.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from typing import Annotated, Any, Union

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)


class BehaviorConfigError(ValueError):
    """Raised when a behavior_config payload cannot be validated."""


# ---------------------------------------------------------------------------
# Shared primitives
# ---------------------------------------------------------------------------

ARM_DOF = 6  # Length expected for every pose (joint-space target).

# Default action-head width for a learned skill whose metadata omits
# ``execution.action_dim``. Single source of truth for this default: it backs
# LearnedExecCfg.action_dim (runtime/consumer) and is imported by the engine pre-build
# (act_config.create_act_config / act_trt), so producer and consumer agree on the
# engine-cache key. ``innate_training_node.workers`` mirrors this value locally because
# the long-running node can't import the manipulation package (see its comment there).
DEFAULT_ACTION_DIM = 10

KNOWN_BEHAVIOR_TYPES = ("learned", "poses", "replay")


def _reject_bool_and_str(value: Any) -> Any:
    """Reject bool and str inputs on numeric fields.

    pydantic in non-strict mode happily coerces ``True`` to ``1`` (bool is
    an int subclass in Python) and ``"120"`` to ``120``; both are almost
    always bugs in a JSON-backed config, so we reject them up front with a
    clear message.
    """
    if isinstance(value, bool):
        raise ValueError(f"expected a number, got bool ({value!r})")
    if isinstance(value, str):
        raise ValueError(f"expected a number, got string ({value!r})")
    return value


def _finite_number(value: Any) -> Any:
    """Reject non-finite floats (NaN / +-inf) after the bool/str guard."""
    value = _reject_bool_and_str(value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"expected a finite number, got {value!r}")
    return value


def _empty_pose_to_none(value: Any) -> Any:
    """Coerce an empty list to ``None`` so the existing ``[] means skip``
    semantics keep working without sprinkling the check in every caller.
    """
    if isinstance(value, list) and len(value) == 0:
        return None
    return value


# Length-constrained pose vector. Applied after _empty_pose_to_none via the
# field validator on each pose field below.
Pose6 = Annotated[list[float], Field(min_length=ARM_DOF, max_length=ARM_DOF)]


# ---------------------------------------------------------------------------
# Per-type execution configs
# ---------------------------------------------------------------------------


class _BaseExecCfg(BaseModel):
    """Shared pydantic configuration for every execution schema."""

    # ``extra='ignore'`` keeps existing metadata files (e.g. wave's
    # ``model_type`` / ``downloads`` keys) compatible.
    model_config = ConfigDict(extra="ignore", strict=False)

    @model_validator(mode="before")
    @classmethod
    def _null_means_default(cls, data: Any) -> Any:
        """Treat JSON ``null`` the same as a missing key.

        The canonical skill-creation template emits ``null`` placeholders
        for every optional override (``duration``, ``progress_threshold``,
        ``start_pose``, ...) so the file self-documents which knobs exist
        without baking in default values that could drift from the ones
        baked into ``manipulation_server``. Without this pre-hook, pydantic
        would reject ``"duration": null`` because ``None`` isn't a
        ``float`` - here we strip null entries so each field falls back to
        its declared default instead.

        For required fields (``checkpoint``, ``replay_file``, ``poses``),
        an explicit null still fails validation - but with a clearer
        ``Field required`` message instead of a type-mismatch one.
        """
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if v is not None}
        return data


class LearnedExecCfg(_BaseExecCfg):
    """``execution`` block for ``type: learned`` skills."""

    checkpoint: str = Field(..., min_length=1)
    action_dim: int = Field(DEFAULT_ACTION_DIM, ge=1, le=64)
    duration: float = Field(120.0, gt=0)
    progress_threshold: float = Field(2.0, ge=0)
    start_pose: Pose6 | None = None
    end_pose: Pose6 | None = None
    start_pose_time: float = Field(1.0, gt=0)
    end_pose_time: float = Field(1.0, gt=0)
    # chunk_size-aware clamping happens inside create_act_config once the
    # checkpoint is loaded; the schema only enforces ``>= 1``.
    n_action_steps: int | None = Field(None, ge=1)

    # --- Auto-stop tuning ---------------------------------------------------
    # The learned loop ends early when one of these fires, otherwise at ``duration``
    # (the always-on hard cap). Every field below defaults to a no-op, so a skill
    # behaves exactly as before until it opts in. See
    # ``manipulation.auto_stop.LearnedStopDetector`` for how they combine.
    #
    # Floor (s) before any early stop may fire (the idle dwell also only starts
    # counting after it); 0 = no floor.
    min_duration: float = Field(0.0, ge=0)
    # EMA smoothing for the progress signal, in (0, 1]; 1.0 = raw (no smoothing),
    # smaller = smoother but slower to react. Applied to both the progress-threshold
    # and progress-gate checks.
    progress_ema_alpha: float = Field(1.0, gt=0, le=1.0)
    # Stop once the robot has held still this long (s); 0 = idle stop disabled.
    idle_seconds: float = Field(0.0, ge=0)
    # Per-step "not moving" thresholds: arm = L2 norm of the commanded joint-target
    # change (rad), base = |linear.x| + |angular.z| (m/s + rad/s). Both are published
    # on the inference-profile topic (``arm_jerk`` / ``base_speed``) while a learned
    # skill runs -- read them off while the skill holds its end pose.
    idle_arm_eps: float = Field(0.01, gt=0)
    idle_base_eps: float = Field(0.01, gt=0)
    # Idle only counts as "done" once smoothed progress reaches this (the "stopped
    # moving AND made progress" guard against mid-task pauses); 0 = idle alone may stop.
    progress_gate: float = Field(0.0, ge=0)
    # Progress-stability stop, for checkpoints whose progress head saturates high both
    # before the task engages and after it finishes (so a bare progress threshold fires
    # in the opening steps). Stop once smoothed progress -- having first dipped below
    # ``engage_below`` -- then holds >= ``stable_min`` for ``stable_seconds``.
    # ``stable_seconds`` = 0 disables this stop; ``engage_below`` = 0 arms it immediately
    # (no dip required). Read ``progress_max`` / the progress trace off the profiling tab
    # to pick ``stable_min`` (just under the settled peak) and ``engage_below`` (above the
    # active-phase dips).
    engage_below: float = Field(0.0, ge=0)
    stable_min: float = Field(0.0, ge=0)
    stable_seconds: float = Field(0.0, ge=0)

    @field_validator("start_pose", "end_pose", mode="before")
    @classmethod
    def _coerce_empty_pose(cls, value: Any) -> Any:
        return _empty_pose_to_none(value)

    @field_validator(
        "action_dim",
        "duration",
        "progress_threshold",
        "start_pose_time",
        "end_pose_time",
        "n_action_steps",
        "min_duration",
        "progress_ema_alpha",
        "idle_seconds",
        "idle_arm_eps",
        "idle_base_eps",
        "progress_gate",
        "engage_below",
        "stable_min",
        "stable_seconds",
        mode="before",
    )
    @classmethod
    def _guard_numeric(cls, value: Any) -> Any:
        if value is None:
            return value
        return _finite_number(value)

    @model_validator(mode="after")
    def _progress_features_need_progress_head(self) -> LearnedExecCfg:
        """Reject progress-dependent stops on a skill without a progress head.

        With ``action_dim < 10`` the policy has no progress output. A ``progress_gate``
        could never pass (so the idle stop would silently never fire) and the
        progress-stability stop has no signal at all -- either way the skill would
        always run to the ``duration`` cap. Fail loudly at validation time instead.
        """
        if self.action_dim < 10:
            if self.progress_gate > 0:
                raise ValueError(
                    f"progress_gate={self.progress_gate} requires a progress head "
                    f"(action_dim >= 10, got {self.action_dim}): the gate could never pass, "
                    "so the idle stop would silently never fire. Set progress_gate to 0/null, "
                    "or use a checkpoint whose action head includes progress."
                )
            if self.stable_seconds > 0:
                raise ValueError(
                    f"stable_seconds={self.stable_seconds} requires a progress head "
                    f"(action_dim >= 10, got {self.action_dim}): the progress-stability stop "
                    "has no signal without one, so it would silently never fire. Set "
                    "stable_seconds to 0/null, or use a checkpoint whose action head includes progress."
                )
        return self


class PosesExecCfg(_BaseExecCfg):
    """``execution`` block for ``type: poses`` skills."""

    poses: list[Pose6] = Field(..., min_length=1)
    # ``steps`` is the per-pose duration in seconds that the task manager
    # holds between waypoints. ``None`` => defer to ``len(poses)`` in the
    # caller (preserves the legacy default).
    steps: float | None = Field(None, gt=0)

    @field_validator("steps", mode="before")
    @classmethod
    def _guard_steps(cls, value: Any) -> Any:
        if value is None:
            return value
        return _finite_number(value)


class ReplayExecCfg(_BaseExecCfg):
    """``execution`` block for ``type: replay`` skills."""

    replay_file: str = Field(..., min_length=1)
    start_pose: Pose6 | None = None
    end_pose: Pose6 | None = None
    start_pose_time: float = Field(1.0, gt=0)
    end_pose_time: float = Field(1.0, gt=0)
    replay_frequency: float = Field(12.0, gt=0)

    @field_validator("start_pose", "end_pose", mode="before")
    @classmethod
    def _coerce_empty_pose(cls, value: Any) -> Any:
        return _empty_pose_to_none(value)

    @field_validator(
        "start_pose_time",
        "end_pose_time",
        "replay_frequency",
        mode="before",
    )
    @classmethod
    def _guard_numeric(cls, value: Any) -> Any:
        if value is None:
            return value
        return _finite_number(value)


ExecCfg = Union[LearnedExecCfg, PosesExecCfg, ReplayExecCfg]  # noqa: UP007


_MODEL_FOR_TYPE: dict[str, type[_BaseExecCfg]] = {
    "learned": LearnedExecCfg,
    "poses": PosesExecCfg,
    "replay": ReplayExecCfg,
}


# ---------------------------------------------------------------------------
# Top-level entry point
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidatedBehavior:
    """Result of successfully validating a behavior_config payload."""

    behavior_type: str
    params: ExecCfg
    # Absolute path to the on-disk asset referenced by the config
    # (``checkpoint`` for learned, ``replay_file`` for replay). ``None`` for
    # poses skills, which don't reference any file.
    resolved_path: str | None = None


def _format_validation_error(exc: ValidationError, prefix: str = "execution") -> str:
    """Turn a pydantic ValidationError into a one-line message.

    Each error is rendered as ``{prefix}.{field}: {msg} (got {input!r})`` so
    whoever reads the log / action-result message can immediately see what
    was wrong in metadata.json.
    """
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        full = f"{prefix}.{loc}" if loc else prefix
        msg = err.get("msg", "invalid value")
        input_val = err.get("input", "<missing>")
        parts.append(f"{full}: {msg} (got {input_val!r})")
    return "; ".join(parts) if parts else str(exc)


def validate_behavior_config(
    behavior_config: str | dict,
    skill_dir: str,
    *,
    check_files_exist: bool = True,
) -> ValidatedBehavior:
    """Parse + validate a behavior_config payload.

    Parameters
    ----------
    behavior_config:
        Either the raw JSON string (as received over the
        ``ExecuteBehavior`` action request) or an already-decoded ``dict``.
    skill_dir:
        Absolute path to the skill directory. Used to resolve
        ``checkpoint`` / ``replay_file`` to absolute paths.
    check_files_exist:
        When ``True`` (default), also assert that the referenced checkpoint
        / replay file is present on disk. Pass ``False`` for pure schema
        tests.

    Returns
    -------
    ValidatedBehavior
        Behavior type, typed params, and resolved absolute asset path.

    Raises
    ------
    BehaviorConfigError
        On any parse, schema, bounds, or file-existence failure.
    """
    # 1. JSON decode if needed.
    if isinstance(behavior_config, str):
        try:
            payload = json.loads(behavior_config)
        except json.JSONDecodeError as exc:
            raise BehaviorConfigError(f"behavior_config is not valid JSON: {exc}") from exc
    elif isinstance(behavior_config, dict):
        payload = behavior_config
    else:
        raise BehaviorConfigError(
            f"behavior_config must be a JSON string or dict, got {type(behavior_config).__name__}"
        )

    if not isinstance(payload, dict):
        raise BehaviorConfigError(f"behavior_config must decode to a JSON object, got {type(payload).__name__}")

    # 2. Top-level shape.
    behavior_type = payload.get("type")
    if behavior_type not in KNOWN_BEHAVIOR_TYPES:
        raise BehaviorConfigError(f"type: must be one of {list(KNOWN_BEHAVIOR_TYPES)}, got {behavior_type!r}")

    exec_dict = payload.get("execution")
    if not isinstance(exec_dict, dict):
        raise BehaviorConfigError(f"execution: must be a JSON object, got {type(exec_dict).__name__}")

    # 3. Per-type schema validation.
    model_cls = _MODEL_FOR_TYPE[behavior_type]
    try:
        params = model_cls.model_validate(exec_dict)
    except ValidationError as exc:
        raise BehaviorConfigError(_format_validation_error(exc)) from exc

    # 4. Asset existence checks.
    resolved_path: str | None = None
    if behavior_type == "learned":
        assert isinstance(params, LearnedExecCfg)  # for type checkers
        resolved_path = os.path.join(skill_dir, params.checkpoint)
        if check_files_exist and not os.path.isfile(resolved_path):
            raise BehaviorConfigError(f"execution.checkpoint: file does not exist at {resolved_path!r}")
    elif behavior_type == "replay":
        assert isinstance(params, ReplayExecCfg)
        resolved_path = os.path.join(skill_dir, params.replay_file)
        if check_files_exist and not os.path.isfile(resolved_path):
            raise BehaviorConfigError(f"execution.replay_file: file does not exist at {resolved_path!r}")

    return ValidatedBehavior(
        behavior_type=behavior_type,
        params=params,
        resolved_path=resolved_path,
    )
