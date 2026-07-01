# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
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
import time
from collections.abc import Callable

import numpy as np
from aiortc import VideoStreamTrack
from av import VideoFrame

# Stop re-encoding a frozen frame once the producer has been handing back the
# same array for longer than this; the track then stalls (no new frames) instead
# of streaming a stale frame as if it were live.
STALE_AFTER_S = 2.0


class CameraTrack(VideoStreamTrack):
    kind = "video"

    def __init__(self, get_frame: Callable[[], np.ndarray | None], name: str):
        super().__init__()
        self._get_frame = get_frame
        self.name = name
        # Carry the camera name as the track id -> aiortc emits it in the SDP
        # `a=msid:<stream> <name>`, so the frontend derives the camera identity
        # straight from the offer (no separate metadata channel).
        self._id = name
        self._active = threading.Event()
        self._last_frame: np.ndarray | None = None
        # Wall-clock time the producer last handed back a *new* frame, used to
        # detect a stalled producer (same array returned indefinitely).
        self._last_change_t: float | None = None
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

            now = time.monotonic()
            if frame is self._last_frame:
                # Producer handed back the same array. If it has been stale too
                # long, stop encoding it so the stream stalls instead of looking
                # live with a frozen frame.
                if self._last_change_t is not None and now - self._last_change_t > STALE_AFTER_S:
                    continue
            else:
                self._last_change_t = now
            self._last_frame = frame

            video_frame = VideoFrame.from_ndarray(np.ascontiguousarray(frame), format="bgr24")
            video_frame.pts = pts
            video_frame.time_base = time_base
            self.frames_encoded += 1
            return video_frame
