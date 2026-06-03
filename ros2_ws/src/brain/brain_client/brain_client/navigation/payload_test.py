"""Unit tests for pure navigation-payload assembly (needs numpy, no ROS)."""

import base64

import numpy as np

from brain_client.navigation import payload


def test_encode_depth_uint16():
    frame = np.zeros((4, 8), dtype=np.uint16)
    block = payload.encode_depth(frame)
    assert block["encoding"] == "16UC1"
    assert block["height"] == 4 and block["width"] == 8
    assert block["step"] == 8 * 2  # bytes per row
    assert base64.b64decode(block["data"]) == frame.tobytes()


def test_encode_depth_float32_and_fallback():
    assert payload.encode_depth(np.zeros((2, 2), dtype=np.float32))["encoding"] == "32FC1"
    assert payload.encode_depth(np.zeros((2, 2), dtype=np.uint8))["encoding"] == "8UC1"


def test_robot_coords_extracts_variances():
    cov = [0.0] * 36
    cov[0], cov[7], cov[35] = 1.1, 2.2, 3.3
    coords = payload.robot_coords(x=1, y=2, z=0, theta=0.5, frame_id="map", covariance=cov)
    assert (coords["cov_x"], coords["cov_y"], coords["cov_yaw"]) == (1.1, 2.2, 3.3)


def test_robot_coords_without_covariance():
    coords = payload.robot_coords(x=1, y=2, z=0, theta=0.5, frame_id="map")
    assert "cov_x" not in coords


def test_dummy_map_is_1x1():
    m = payload.dummy_map()
    assert m["width"] == 1 and m["height"] == 1 and m["frame_id"] == "odom"


def test_assemble_omits_depth_when_none():
    p = payload.assemble(camera_info={"f": 1}, robot_coords={"x": 0}, map_block={"w": 1})
    assert "depth" not in p
    p2 = payload.assemble(camera_info={}, robot_coords={}, map_block={}, depth_block={"d": 1})
    assert p2["depth"] == {"d": 1}
