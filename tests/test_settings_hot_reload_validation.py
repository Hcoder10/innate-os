# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The int-vs-double settings guard must fail fast at launch.

Locks in that launch-time ``settings_params`` raises on an int-typed double
(motion_control.max_speed: 4, not 4.0) instead of letting ROS reject the
param at node startup. Pure Python — no ROS or colcon build required.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "ros2_ws/src/mars_bot/mars_bringup"))

from mars_bringup import config_loader  # noqa: E402

# An int written where ROS declares a double — the case
# _validate_settings_param_types rejects.
_SETTINGS_INT_DOUBLE = """\
/**:
  ros__parameters:
    motion_control:
      max_speed: 4
"""


@pytest.fixture
def settings_with_int_double(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text(_SETTINGS_INT_DOUBLE)
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))


def test_launch_path_fails_fast(settings_with_int_double):
    """settings_params() (the ROS-param path) must reject the int-typed double."""
    with pytest.raises(ValueError, match="decimals"):
        config_loader.settings_params()
