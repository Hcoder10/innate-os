"""Unit tests for pure image encoding (needs numpy + cv2, no ROS)."""

import numpy as np

from brain_client.perception import image_codec


def test_encode_jpeg_returns_base64_string():
    img = np.zeros((16, 16, 3), dtype=np.uint8)
    out = image_codec.encode_jpeg_b64(img)
    assert isinstance(out, str) and len(out) > 0


def test_encode_jpeg_none_input():
    assert image_codec.encode_jpeg_b64(None) is None


def test_encode_video_empty_frames():
    assert image_codec.encode_video_b64([], fps=10.0) is None


def test_encode_video_returns_base64_string():
    frames = [np.zeros((16, 16, 3), dtype=np.uint8) for _ in range(3)]
    out = image_codec.encode_video_b64(frames, fps=10.0)
    assert isinstance(out, str) and len(out) > 0
