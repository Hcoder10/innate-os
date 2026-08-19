# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""The robot camera and browser viewer share the same apartment lighting model."""

import sys
from pathlib import Path

import pytest

mujoco = pytest.importorskip("mujoco")
if not hasattr(mujoco.MjModel, "from_xml_string"):
    pytest.skip("MuJoCo runtime is unavailable", allow_module_level=True)

REPO_ROOT = Path(__file__).resolve().parent.parent
DRIVER_PACKAGE = REPO_ROOT / "ros2_ws" / "src" / "mars_bot" / "mars_sim_driver"
sys.path.insert(0, str(DRIVER_PACKAGE))

from mars_sim_driver.world import build_world_xml  # noqa: E402


def test_apartment_key_and_fill_lights_are_directional():
    model = mujoco.MjModel.from_xml_string(build_world_xml({}, include_placeholder_robot=False))

    assert model.nlight == 2
    assert model.light_type.tolist() == [
        mujoco.mjtLightType.mjLIGHT_DIRECTIONAL,
        mujoco.mjtLightType.mjLIGHT_DIRECTIONAL,
    ]
    assert model.light_castshadow.tolist() == [1, 0]
