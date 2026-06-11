"""Pure image/video encoding — no ROS, no I/O beyond a scratch temp file.

Centralises the JPEG-encode-then-base64 dance that was copy-pasted at four call
sites in the old node, plus the video-clip assembly used by the video feed.
``cv2``/``numpy`` are fine here — the rule for "pure" modules is only *no rclpy*,
so this stays unit-testable without a ROS runtime.
"""

from __future__ import annotations

import base64
import os
import tempfile

import cv2
import numpy as np

JPEG_QUALITY = 70


def encode_jpeg_b64(image: np.ndarray, quality: int = JPEG_QUALITY) -> str | None:
    """Encode a BGR image to a base64 JPEG string. Returns None on failure."""
    if image is None:
        return None
    try:
        success, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        return base64.b64encode(encoded.tobytes()).decode("utf-8") if success else None
    except Exception:
        return None


def encode_video_b64(frames: list[np.ndarray], fps: float) -> str | None:
    """Assemble ``frames`` into an MJPEG/AVI clip and return it base64-encoded.

    Returns None if there are no usable frames or encoding fails. Frames whose
    dimensions differ from the first frame are skipped. The scratch file is always
    removed before returning.
    """
    if not frames:
        return None
    first = frames[0]
    if first is None:
        return None
    height, width = first.shape[0], first.shape[1]
    if height == 0 or width == 0:
        return None

    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".avi", delete=False) as tmp:
            temp_path = tmp.name
        fourcc = cv2.VideoWriter_fourcc(*"MJPG")
        writer = cv2.VideoWriter(temp_path, fourcc, float(fps), (width, height))
        if not writer.isOpened():
            return None
        for frame in frames:
            if frame is not None and frame.shape[0] == height and frame.shape[1] == width:
                writer.write(frame)
        writer.release()
        if os.path.exists(temp_path) and os.path.getsize(temp_path) > 0:
            with open(temp_path, "rb") as f:
                return base64.b64encode(f.read()).decode("utf-8")
        return None
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
