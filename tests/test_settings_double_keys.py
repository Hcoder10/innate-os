# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Drift guard for ``config_loader._SETTINGS_DOUBLE_KEYS`` (a hardcoded set).

Reconstructs settings.yaml.template into YAML and asserts every float-valued key is guarded,
so adding a float knob without guarding it fails here. Pure Python — no ROS or colcon build.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "ros2_ws/src/mars_bot/mars_bringup"))

import yaml  # noqa: E402
from mars_bringup import config_loader  # noqa: E402

_TEMPLATE = _REPO_ROOT / "config/settings.yaml.template"

# Matches a commented-out YAML mapping line (``# key:`` or ``# key: value``) once the
# leading ``# `` is stripped — i.e. the actual config stanzas. Prose, ``── dividers ──``
# and ``SOME SENTENCE:`` lines are skipped because their key token contains spaces.
_YAML_KEY = re.compile(r"^\s*[\w./*]+:(\s|$)")


def _template_float_keys() -> dict[str, float]:
    """Flatten settings.yaml.template to ``{dotted_key: float_default}`` for every
    float-valued tunable, keyed exactly as ``_validate_settings_param_types`` sees them."""
    uncommented = [
        re.sub(r"^(\s*)#\s?", r"\1", line)
        for line in _TEMPLATE.read_text().splitlines()
        if line.lstrip().startswith("#")
    ]
    structural = [line for line in uncommented if _YAML_KEY.match(line)]

    # Parse top-level blocks independently so a repeated node key can't clobber an
    # earlier one (PyYAML keeps only the last). The template now keeps the /** knobs in a
    # single block, but per-block parsing keeps this faithful no matter how it's split.
    blocks: list[list[str]] = []
    for line in structural:
        if line[:1] and not line[0].isspace():
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)

    floats: dict[str, float] = {}
    for block in blocks:
        doc = yaml.safe_load("\n".join(block)) or {}
        for body in doc.values():
            params = body.get("ros__parameters") if isinstance(body, dict) else None
            if not isinstance(params, dict):
                continue
            for key, value in config_loader._flatten_params(params, "").items():
                # bool subclasses int but never float; floats are the double tunables.
                if isinstance(value, float):
                    floats[key] = value
    return floats


def test_parser_finds_the_template_floats():
    """Guard against a silently-broken parser making the checks below pass vacuously."""
    found = _template_float_keys()
    # Sentinels spanning the /** block, a nested key, and a per-node key.
    assert {"motion_control.max_speed", "joystick.slow_mode_factor", "fps"} <= set(found)
    assert len(found) >= 20


def test_every_template_double_is_guarded():
    """Every float knob in the template must be in _SETTINGS_DOUBLE_KEYS, or it would
    crash its node with an unhelpful type error instead of the validator's clear message."""
    unguarded = sorted(set(_template_float_keys()) - config_loader._SETTINGS_DOUBLE_KEYS)
    assert not unguarded, (
        "settings.yaml.template exposes float tunables missing from "
        f"config_loader._SETTINGS_DOUBLE_KEYS: {unguarded}. Add them so an int value is "
        "caught with a clear message instead of a node-startup InvalidParameterTypeException."
    )


def test_no_stale_guarded_keys():
    """And nothing guarded should have left the template — keep the set mirroring the
    documented tunables so the list stays a faithful, reviewable map of what's exposed."""
    stale = sorted(config_loader._SETTINGS_DOUBLE_KEYS - set(_template_float_keys()))
    assert not stale, (
        "config_loader._SETTINGS_DOUBLE_KEYS guards keys no longer present as floats in "
        f"settings.yaml.template: {stale}. Drop them or restore them in the template."
    )
