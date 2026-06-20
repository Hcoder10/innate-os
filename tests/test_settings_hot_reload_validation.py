"""The int-vs-double settings guard must fail fast at launch but never crash a reload.

Locks in the split: launch-time ``settings_params`` raises on an int-typed double, while the
runtime ``load_extra_script_dirs`` read tolerates the same file and still returns its dirs.
Pure Python — no ROS or colcon build required.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "ros2_ws/src/mars_bot/mars_bringup"))

from mars_bringup import config_loader  # noqa: E402

# An int written where ROS declares a double (motion_control.max_speed: 4, not 4.0) —
# the case _validate_settings_param_types rejects — alongside a valid script_paths block.
_SETTINGS_INT_DOUBLE = """\
/**:
  ros__parameters:
    motion_control:
      max_speed: 4
script_paths:
  ros__parameters:
    extra_agent_dirs: ["/opt/team/agents"]
    extra_skill_dirs: ["/opt/team/skills"]
"""


@pytest.fixture
def settings_with_int_double(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text(_SETTINGS_INT_DOUBLE)
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))


def test_launch_path_still_fails_fast(settings_with_int_double):
    """settings_params() (the ROS-param path) must still reject the int-typed double."""
    with pytest.raises(ValueError, match="decimals"):
        config_loader.settings_params()


def test_hot_reload_path_does_not_raise(settings_with_int_double):
    """load_extra_script_dirs() (the runtime hot-reload path) must read the script_paths
    block without re-raising the launch-time int-vs-double guard."""
    assert config_loader.load_extra_script_dirs("extra_agent_dirs") == ["/opt/team/agents"]
    assert config_loader.load_extra_script_dirs("extra_skill_dirs") == ["/opt/team/skills"]
