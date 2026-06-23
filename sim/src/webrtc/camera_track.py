"""A single WebRTC video track sourced from one sim camera.

The track is decoupled from the sim: it pulls the latest BGR frame through a
caller-supplied callable, so the same class serves both the live sim
(`shared_queues.latest_frames`) and the standalone isolation harness
(synthetic frames).

Lazy encoding: while the track is inactive its `recv()` never returns a frame,
so aiortc's VP8 encoder is never fed for that camera — the CPU is only spent on
cameras a browser is actually watching.
"""

import threading
from typing import Callable, Optional

import numpy as np
from aiortc import VideoStreamTrack
from av import VideoFrame


class CameraTrack(VideoStreamTrack):
    kind = "video"

    def __init__(self, get_frame: Callable[[], Optional[np.ndarray]], name: str):
        super().__init__()
        self._get_frame = get_frame
        self.name = name
        # Carry the camera name as the track id -> aiortc emits it in the SDP
        # `a=msid:<stream> <name>`, so the frontend derives the camera identity
        # straight from the offer (no separate metadata channel).
        self._id = name
        self._active = threading.Event()
        self._last_frame: Optional[np.ndarray] = None
        # Diagnostics: counts frames actually handed to the encoder (lazy-encoding proof).
        self.frames_encoded = 0

    def set_active(self, active: bool) -> None:
        if active:
            self._active.set()
        else:
            self._active.clear()

    @property
    def active(self) -> bool:
        return self._active.is_set()

    async def recv(self) -> VideoFrame:
        # next_timestamp() paces each iteration to the track clock and yields a
        # monotonic pts. We loop (not recurse) so a gated/empty tick just paces
        # and retries without growing the stack.
        while True:
            pts, time_base = await self.next_timestamp()

            if not self._active.is_set():
                # Gated off: produce nothing -> no VP8 encode for this camera.
                continue

            frame = self._get_frame()
            if frame is None:
                frame = self._last_frame
            if frame is None:
                # No frame yet (startup); next_timestamp already paced the retry.
                continue
            self._last_frame = frame

            video_frame = VideoFrame.from_ndarray(np.ascontiguousarray(frame), format="bgr24")
            video_frame.pts = pts
            video_frame.time_base = time_base
            self.frames_encoded += 1
            return video_frame
