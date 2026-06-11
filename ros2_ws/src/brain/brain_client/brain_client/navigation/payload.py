"""Pure navigation-payload assembly — no ROS.

Builds the ``depth`` / ``map`` / ``robot_coords`` blocks the cloud agent expects.
Everything here takes plain numbers and numpy arrays; the ROS layer
(``navigation/map.py``, ``perception/pose_tracking.py``) is responsible for
pulling those out of ``OccupancyGrid`` / ``Odometry`` / TF messages and calling in.
"""

from __future__ import annotations

import base64

import numpy as np

# numpy dtype -> (cloud encoding string, bytes per pixel)
_DEPTH_ENCODINGS = {
    np.dtype(np.uint16): ("16UC1", 2),
    np.dtype(np.float32): ("32FC1", 4),
}
_DEPTH_FALLBACK = ("8UC1", 1)


def encode_depth(depth_frame: np.ndarray) -> dict:
    """Encode a 2-D depth array into the agent's depth block."""
    encoding, bytes_per_pixel = _DEPTH_ENCODINGS.get(depth_frame.dtype, _DEPTH_FALLBACK)
    return {
        "height": int(depth_frame.shape[0]),
        "width": int(depth_frame.shape[1]),
        "encoding": encoding,
        "is_bigendian": 0,
        "step": int(depth_frame.shape[1] * bytes_per_pixel),
        "data": base64.b64encode(depth_frame.tobytes()).decode("utf-8"),
    }


def encode_map(
    *,
    resolution: float,
    width: int,
    height: int,
    origin_x: float,
    origin_y: float,
    origin_z: float,
    origin_yaw: float,
    frame_id: str,
    data,
) -> dict:
    """Encode an occupancy grid into the agent's map block."""
    return {
        "resolution": resolution,
        "width": width,
        "height": height,
        "origin_x": origin_x,
        "origin_y": origin_y,
        "origin_z": origin_z,
        "origin_yaw": origin_yaw,
        "frame_id": frame_id,
        "data": base64.b64encode(np.array(data, dtype=np.int8).tobytes()).decode("utf-8"),
    }


def dummy_map() -> dict:
    """1x1 placeholder map used in map-free navigation mode."""
    return encode_map(
        resolution=100.0,
        width=1,
        height=1,
        origin_x=0.0,
        origin_y=0.0,
        origin_z=0.0,
        origin_yaw=0.0,
        frame_id="odom",
        data=[0],
    )


def robot_coords(*, x: float, y: float, z: float, theta: float, frame_id: str, covariance=None) -> dict:
    """Build the robot_coords block, attaching x/y/yaw variances when available."""
    coords = {"x": x, "y": y, "z": z, "theta": theta, "frame_id": frame_id}
    if covariance is not None:
        try:
            if not hasattr(covariance, "__len__") or len(covariance) >= 36:
                coords["cov_x"] = covariance[0]
                coords["cov_y"] = covariance[7]
                coords["cov_yaw"] = covariance[35]
        except Exception:
            pass
    return coords


def assemble(*, camera_info: dict, robot_coords: dict, map_block: dict, depth_block: dict | None = None) -> dict:
    """Combine the blocks into the final navigation payload."""
    payload = {"camera_info": camera_info, "map": map_block, "robot_coords": robot_coords}
    if depth_block is not None:
        payload["depth"] = depth_block
    return payload
