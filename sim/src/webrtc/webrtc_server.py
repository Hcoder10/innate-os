"""aiortc WebRTC server core for the sim.

Transport-agnostic: `WebRTCManager` drives the peer connection and is fed
signaling messages (start / answer / ice / active_streams) by whatever carries
them — the rosbridge bridge in the live sim, or plain HTTP in the isolation
harness. The server is the OFFERER (matching the robot protocol): on `start` it
builds the offer and returns the SDP; the browser answers.

Three video tracks are always present in the SDP (first_person -> mid 0,
arm_wrist -> mid 1, chase -> mid 2, all sendonly). Encoding is lazy: only the
cameras named in the latest `active_streams` message are fed to the encoder.
"""

import asyncio
import os
import queue
import threading
from typing import Any, Callable, Optional

import numpy as np
from aiortc import RTCIceCandidate, RTCPeerConnection, RTCRtpSender
from aiortc.sdp import candidate_from_sdp

from .camera_track import CameraTrack

# Canonical order -> deterministic transceiver mids. Only the cameras that are
# actually producing frames are offered, so the frontend (which derives the set
# from the SDP) shows exactly what's streaming.
CAMERAS = ["first_person", "arm_wrist", "chase"]


def default_fps() -> float:
    try:
        return float(os.getenv("SIM_RENDER_FPS", "25"))
    except ValueError:
        return 25.0


def _parse_ice(payload: dict) -> Optional[RTCIceCandidate]:
    candidate_str = (payload or {}).get("candidate") or ""
    if not candidate_str:
        return None  # end-of-candidates / empty
    if candidate_str.startswith("candidate:"):
        candidate_str = candidate_str[len("candidate:") :]
    candidate = candidate_from_sdp(candidate_str)
    candidate.sdpMid = payload.get("sdpMid")
    candidate.sdpMLineIndex = payload.get("sdpMLineIndex")
    return candidate


class WebRTCManager:
    """Single-peer manager. A new `start` tears down the previous connection."""

    def __init__(
        self,
        get_frame_for: Callable[[str], Optional[np.ndarray]],
        fps: Optional[float] = None,
        list_cameras: Optional[Callable[[], list[str]]] = None,
    ):
        self._get_frame_for = get_frame_for
        self._fps = fps or default_fps()
        # Returns the camera names currently available; defaults to all canonical
        # cameras (used by the isolation harness, which synthesizes every camera).
        self._list_cameras = list_cameras or (lambda: list(CAMERAS))
        self._pc: Optional[RTCPeerConnection] = None
        self._tracks: dict[str, CameraTrack] = {}

    def _available(self) -> list[str]:
        present = set(self._list_cameras())
        cameras = [c for c in CAMERAS if c in present]
        return cameras or list(CAMERAS)  # fall back if nothing has rendered yet

    async def on_start(self, payload: dict) -> str:
        """Build the peer connection + offer. Returns the offer SDP."""
        await self.close()

        pc = RTCPeerConnection()
        self._pc = pc
        self._tracks = {}

        vp8 = [c for c in RTCRtpSender.getCapabilities("video").codecs if c.mimeType == "video/VP8"]

        cameras = self._available()
        for name in cameras:
            track = CameraTrack(lambda n=name: self._get_frame_for(n), name)
            self._tracks[name] = track
            transceiver = pc.addTransceiver(track, direction="sendonly")
            if vp8:
                transceiver.setCodecPreferences(vp8)

        # First available camera shows immediately; the rest stay gated off.
        self._apply_active(cameras[:1])

        @pc.on("connectionstatechange")
        async def _on_state():
            if pc.connectionState in ("failed", "closed", "disconnected"):
                await self.close()

        offer = await pc.createOffer()
        # aiortc gathers ICE within setLocalDescription, so localDescription.sdp
        # already carries the server candidates (non-trickle) — no ice_out needed.
        await pc.setLocalDescription(offer)
        return pc.localDescription.sdp

    async def on_answer(self, sdp: str) -> None:
        if not self._pc or not sdp:
            return
        from aiortc import RTCSessionDescription

        await self._pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="answer"))

    async def on_ice(self, payload: dict) -> None:
        if not self._pc:
            return
        candidate = _parse_ice(payload)
        if candidate is not None:
            await self._pc.addIceCandidate(candidate)

    def on_active_streams(self, cameras: list[str]) -> None:
        self._apply_active(cameras)

    def _apply_active(self, cameras: list[str]) -> None:
        wanted = set(cameras or [])
        for name, track in self._tracks.items():
            track.set_active(name in wanted)

    def encode_counts(self) -> dict[str, int]:
        return {name: track.frames_encoded for name, track in self._tracks.items()}

    async def close(self) -> None:
        if self._pc is not None:
            pc, self._pc = self._pc, None
            try:
                await pc.close()
            except Exception:
                pass
        self._tracks = {}


# --------------------------------------------------------------------------- #
# Live-sim runner: own thread + asyncio loop, bridged to rosbridge only via the
# shared_queues seam (no cross-thread loop coupling).
# --------------------------------------------------------------------------- #
def start_webrtc_server(shared_queues, fps: Optional[float] = None) -> threading.Thread:
    thread = threading.Thread(target=_run, args=(shared_queues, fps), daemon=True, name="webrtc-server")
    thread.start()
    return thread


def _run(shared_queues, fps: Optional[float]) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_main(shared_queues, fps))
    finally:
        loop.close()


async def _main(shared_queues, fps: Optional[float]) -> None:
    manager = WebRTCManager(
        lambda name: shared_queues.latest_frames.get(name),
        fps=fps,
        list_cameras=lambda: [c for c in CAMERAS if shared_queues.latest_frames.get(c) is not None],
    )
    sig_in: queue.Queue = shared_queues.webrtc_signal_in
    sig_out: queue.Queue = shared_queues.webrtc_signal_out

    print("[WebRTC] server started (3 cameras, lazy VP8 encoding)")
    while not shared_queues.exit_event.is_set():
        try:
            item = sig_in.get_nowait()
        except queue.Empty:
            await asyncio.sleep(0.005)
            continue

        try:
            await _dispatch(manager, item, sig_out)
        except Exception as exc:  # one bad message must not kill the server
            print(f"[WebRTC] error handling {item.get('kind')!r}: {exc}")

    await manager.close()


async def _dispatch(manager: WebRTCManager, item: dict, sig_out: queue.Queue) -> None:
    kind = item.get("kind")
    if kind == "start":
        offer_sdp = await manager.on_start(item.get("payload", {}))
        sig_out.put_nowait({"kind": "offer", "sdp": offer_sdp})
    elif kind == "answer":
        await manager.on_answer(item.get("sdp", ""))
    elif kind == "ice":
        await manager.on_ice(item.get("payload", {}))
    elif kind == "active_streams":
        manager.on_active_streams(item.get("cameras", []))
