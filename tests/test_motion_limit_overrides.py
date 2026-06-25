"""Unit tests for the nav2 motion-limit remap (``config_loader.load_motion_limit_overrides``).

Locks in: the exact dict each schema emits, that every emitted name still exists in the real
nav2 config (so a renamed plugin/param fails here, not silently), the no-override no-op, and
the reduce-only reverse-linear cap. Pure Python — no ROS or colcon build required.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "ros2_ws/src/mars_bot/mars_bringup"))

from mars_bringup import config_loader  # noqa: E402

_NAV_CONFIG = _REPO_ROOT / "ros2_ws/src/mars_bot/mars_nav/config"
_CONTROLLER_YAML = _NAV_CONFIG / "controller.yaml"  # mppi (real robot)
_SIM_PARAMS_YAML = _NAV_CONFIG / "nav2_navigation_params_sim.yaml"  # dwb (sim)
_SMOOTHER_YAML = _NAV_CONFIG / "velocity_smoother.yaml"  # smoother

_LIN = 0.25  # looser than the package reverse magnitude (|vx_min| = 0.2): reverse preserved
_ANG = 0.5
_LIN_TIGHT = 0.1  # below the package reverse magnitude: the reverse cap bites


def _settings_with_nav(lin: float, ang: float) -> str:
    return f"/**:\n  ros__parameters:\n    nav:\n      max_speed: {lin}\n      max_angular_speed: {ang}\n"


def _point_config_loader_at(tmp_path, monkeypatch, settings_yaml: str) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text(settings_yaml)
    monkeypatch.setenv("INNATE_OS_ROOT", str(tmp_path))


@pytest.fixture
def override(tmp_path, monkeypatch):
    """settings.yaml that sets ``/** nav`` to (_LIN, _ANG)."""
    _point_config_loader_at(tmp_path, monkeypatch, _settings_with_nav(_LIN, _ANG))


@pytest.fixture
def tight_override(tmp_path, monkeypatch):
    """settings.yaml whose nav.max_speed (_LIN_TIGHT) is below the package reverse magnitude."""
    _point_config_loader_at(tmp_path, monkeypatch, _settings_with_nav(_LIN_TIGHT, _ANG))


@pytest.fixture
def no_override(tmp_path, monkeypatch):
    """settings.yaml with no nav block (the all-commented / default case)."""
    _point_config_loader_at(tmp_path, monkeypatch, "# no overrides\n")


# --- exact emitted dicts ---------------------------------------------------


def test_mppi_emits_expected(override):
    assert config_loader.load_motion_limit_overrides("mppi") == {
        "InnateFollowPath.vx_max": _LIN,
        "InnateFollowPath.wz_max": _ANG,
    }


def test_dwb_emits_expected(override):
    assert config_loader.load_motion_limit_overrides("dwb") == {
        "FollowPath.max_vel_x": _LIN,
        "FollowPath.max_speed_xy": _LIN,
        "FollowPath.max_vel_theta": _ANG,
        "FollowPath.min_vel_theta": -_ANG,  # symmetric reverse rotation
    }


def test_smoother_overrides_forward_and_theta_preserving_the_rest(override):
    defaults = config_loader.load_yaml_param_defaults(_SMOOTHER_YAML)
    out = config_loader.load_motion_limit_overrides("smoother", defaults=defaults)
    # Forward-x and theta are overridden ...
    assert out["max_velocity"][0] == _LIN
    assert out["max_velocity"][2] == _ANG
    assert out["min_velocity"][2] == -_ANG
    # ... lateral (y) and reverse-x are preserved from the package YAML default.
    assert out["max_velocity"][1] == defaults["max_velocity"][1]
    assert out["min_velocity"][0] == defaults["min_velocity"][0]
    assert out["min_velocity"][1] == defaults["min_velocity"][1]


# --- reverse-linear is capped toward zero (the "turn nav down for safety" guarantee) ---


def test_mppi_caps_reverse_when_nav_below_default(tight_override):
    """A nav cap below the package |vx_min| tightens the mppi reverse limit to match it."""
    defaults = config_loader.load_yaml_param_defaults(_CONTROLLER_YAML)
    out = config_loader.load_motion_limit_overrides("mppi", defaults=defaults)
    assert out["InnateFollowPath.vx_max"] == _LIN_TIGHT
    assert out["InnateFollowPath.vx_min"] == -_LIN_TIGHT  # reverse reduced, was -0.2


def test_mppi_reverse_not_loosened_above_default(override):
    """_LIN (0.25) exceeds |vx_min| (0.2): reverse is never increased past the package default."""
    defaults = config_loader.load_yaml_param_defaults(_CONTROLLER_YAML)
    out = config_loader.load_motion_limit_overrides("mppi", defaults=defaults)
    assert out["InnateFollowPath.vx_min"] == defaults["InnateFollowPath.vx_min"]


def test_dwb_reverse_stays_forward_only(tight_override):
    """The sim controller is forward-only (min_vel_x = 0.0); capping must not enable reverse."""
    defaults = config_loader.load_yaml_param_defaults(_SIM_PARAMS_YAML)
    out = config_loader.load_motion_limit_overrides("dwb", defaults=defaults)
    assert out["FollowPath.min_vel_x"] == 0.0


def test_smoother_caps_reverse_when_nav_below_default(tight_override):
    """The smoother's reverse-x limit is reduced to match a low nav cap."""
    defaults = config_loader.load_yaml_param_defaults(_SMOOTHER_YAML)
    out = config_loader.load_motion_limit_overrides("smoother", defaults=defaults)
    assert out["min_velocity"][0] == -_LIN_TIGHT  # reverse-x reduced, was -0.2


def test_reverse_not_capped_without_defaults(tight_override):
    """Without defaults the reverse limit can't be reduced safely, so it is left untouched."""
    out = config_loader.load_motion_limit_overrides("mppi")
    assert out["InnateFollowPath.vx_max"] == _LIN_TIGHT
    assert "InnateFollowPath.vx_min" not in out


# --- the key guard: emitted names must exist in the real nav2 config -------


@pytest.mark.parametrize("schema,config", [("mppi", _CONTROLLER_YAML), ("dwb", _SIM_PARAMS_YAML)])
def test_remap_targets_exist_in_config(override, schema, config):
    valid = config_loader.load_yaml_param_defaults(config)
    # Pass defaults so the reverse-linear targets (vx_min / min_vel_x) are emitted and checked too.
    emitted = config_loader.load_motion_limit_overrides(schema, defaults=valid)
    assert emitted, f"{schema}: expected a non-empty override"
    missing = sorted(k for k in emitted if k not in valid)
    assert not missing, (
        f"{schema}: these remap targets no longer exist in {config.name} — a rename "
        f"would make the motion_control override a silent no-op: {missing}"
    )


def test_smoother_targets_exist_in_config(override):
    valid = config_loader.load_yaml_param_defaults(_SMOOTHER_YAML)
    emitted = config_loader.load_motion_limit_overrides("smoother", defaults=valid)
    assert emitted
    missing = sorted(k for k in emitted if k not in valid)
    assert not missing, f"smoother: these remap targets no longer exist in {_SMOOTHER_YAML.name}: {missing}"


# --- no-override no-op -----------------------------------------------------


@pytest.mark.parametrize("schema", ["mppi", "dwb", "smoother"])
def test_no_override_is_noop(no_override, schema):
    assert config_loader.load_motion_limit_overrides(schema) == {}
