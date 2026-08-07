# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""Whether a camera frame is worth remembering. Pure: no ROS, no I/O.

A refresh overwrites a viewpoint's only image, so one bad frame — mid-turn
motion blur, a lights-off room, a lens against a blank surface — must not
clobber a good view. These checks judge the frame, not the scene: too dark to
show anything, or too little detail to be anything.
"""

from __future__ import annotations

import cv2
import numpy as np

_ANALYSIS_WIDTH = 320  # thresholds are calibrated at this scale; camera resolution must not move them
_MIN_MEAN_BRIGHTNESS = 15.0  # of 255
MIN_SHARPNESS = 25.0  # variance of the Laplacian; below is heavy blur or a featureless close-up


def frame_sharpness(jpeg: bytes) -> float | None:
    """Sharpness score for ranking frames against each other, or None when the
    frame is unusable regardless (undecodable, or too dark to show anything).
    A keepable frame scores at least MIN_SHARPNESS."""
    gray = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_GRAYSCALE)
    if gray is None:
        return None
    if gray.shape[1] != _ANALYSIS_WIDTH:
        height = max(1, round(gray.shape[0] * _ANALYSIS_WIDTH / gray.shape[1]))
        gray = cv2.resize(gray, (_ANALYSIS_WIDTH, height), interpolation=cv2.INTER_AREA)
    if float(gray.mean()) < _MIN_MEAN_BRIGHTNESS:
        return None
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def frame_worth_keeping(jpeg: bytes) -> bool:
    sharpness = frame_sharpness(jpeg)
    return sharpness is not None and sharpness >= MIN_SHARPNESS
