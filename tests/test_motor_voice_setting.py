# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Drift guard for the settings page's motor-sound picker.

The picker's choices come from the robot -- the node declares them as a read-only
parameter built from ``VOICES``, and the page reads that over rosbridge -- which is
the whole point. What is left to drift is the three constants around it: the
catalog's default, the node's declared default, and whether that name is a voice at
all. Pure text parsing, no numpy and no ROS, because CI has neither.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_CATALOG = _REPO_ROOT / "webapp/js/settings/catalog.js"
_NODE = _REPO_ROOT / "ros2_ws/src/mars_bot/mars_control/mars_control/motor_sound.py"
_VOICES = _REPO_ROOT / "ros2_ws/src/mars_bot/mars_control/mars_control/accel_voices.py"


def _knob() -> str:
    """The catalog line declaring the motor-sound knob."""
    line = next((row for row in _CATALOG.read_text().splitlines() if '"motor_sound", "voice"' in row), None)
    assert line, "the motor-sound knob is gone from the settings catalog"
    return line


def test_picker_asks_the_robot_rather_than_listing_voices() -> None:
    knob = _knob()
    assert 'optionsFrom: { node: "/motor_sound", param: "motor_sound.voice_options" }' in knob
    assert "options: []" in knob, "a static list here goes stale the first time a voice is added"
    assert 'live: "/motor_sound"' in knob, "the node rebuilds its voice on a parameter update"

    node = _NODE.read_text()
    assert '"motor_sound.voice_options"' in node, "the parameter the picker reads is gone"
    assert '["motor", *VOICES]' in node, "the options must be built from the registry, not typed out"


def test_catalog_default_matches_the_node() -> None:
    catalog_default = re.search(r'default: "(\w+)"', _knob()).group(1)
    declared = re.search(r'\("motor_sound\.voice", "(\w+)"\)', _NODE.read_text()).group(1)
    assert catalog_default == declared, "the settings page would show the wrong built-in default"

    slugs = set(re.findall(r'^\s+slug = "([a-z_0-9]+)"', _VOICES.read_text(), re.M))
    assert catalog_default in slugs | {"motor"}
