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
import queue
import threading
from collections.abc import Callable

import numpy as np
from aiortc import RTCIceCandidate, RTCPeerConnection, RTCRtpSender
from aiortc.sdp import candidate_from_sdp

from .camera_track import CameraTrack

# Canonical order -> deterministic transceiver mids. All three tracks are always
# present in the SDP (sendonly); the frontend derives the camera set from it.
CAMERAS = ["first_person", "arm_wrist", "chase"]


def _parse_ice(payload: dict) -> RTCIceCandidate | None:
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

    def __init__(self, get_frame_for: Callable[[str], np.ndarray | None]):
        self._get_frame_for = get_frame_for
        self._pc: RTCPeerConnection | None = None
        self._tracks: dict[str, CameraTrack] = {}

    async def on_start(self, payload: dict) -> str | None:
        """(Re)build the peer connection + offer, or — for a no-reneg START — just
        retarget which cameras we encode. Returns the offer SDP, or None when no new
        offer is needed (a no-reneg active-set change)."""
        # `video` is the active (encoded) set the browser wants; fall back to the
        # first camera so something shows before the UI learns the real roster.
        requested = payload.get("video") or []
        active = [c for c in requested if c in CAMERAS] or CAMERAS[:1]

        # No-reneg START: the browser is just switching which cameras we push on the
        # already-negotiated transceivers — flip the active set, emit no new offer.
        if self._pc is not None and not payload.get("renegotiate"):
            self._apply_active(active)
            return None

        await self.close()

        pc = RTCPeerConnection()
        self._pc = pc
        self._tracks = {}

        vp8 = [c for c in RTCRtpSender.getCapabilities("video").codecs if c.mimeType == "video/VP8"]

        # Offer all cameras up front so one that renders its first frame after the
        # offer is still selectable; encoding stays lazy (tracks gated off below).
        for name in CAMERAS:
            track = CameraTrack(lambda n=name: self._get_frame_for(n), name)
            self._tracks[name] = track
            transceiver = pc.addTransceiver(track, direction="sendonly")
            if vp8:
                transceiver.setCodecPreferences(vp8)

        self._apply_active(active)

        @pc.on("connectionstatechange")
        async def _on_state():
            # Only tear down on terminal states — "disconnected" is transient and
            # usually self-heals. Guard against a stale handler: a new `start`
            # replaces self._pc, and the old pc's late state change must not close
            # the live connection.
            if pc is self._pc and pc.connectionState in ("failed", "closed"):
                await self.close()

        offer = await pc.createOffer()
        # aiortc gathers ICE within setLocalDescription, so localDescription.sdp
        # already carries the server candidates (non-trickle) — no ice_out needed.
        await pc.setLocalDescription(offer)
        return pc.localDescription.sdp

    async def on_answer(self, sdp: str) -> None:
        if not self._pc or not sdp:
            return
        # Only an offer we just sent expects an answer; a stale/duplicate answer
        # (from handshake churn) would raise "cannot handle answer in state stable".
        if self._pc.signalingState != "have-local-offer":
            return
        from aiortc import RTCSessionDescription

        await self._pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="answer"))

    async def on_ice(self, payload: dict) -> None:
        if not self._pc:
            return
        candidate = _parse_ice(payload)
        if candidate is not None:
            await self._pc.addIceCandidate(candidate)

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
def start_webrtc_server(shared_queues) -> threading.Thread:
    thread = threading.Thread(target=_run, args=(shared_queues,), daemon=True, name="webrtc-server")
    thread.start()
    return thread


def _run(shared_queues) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_main(shared_queues))
    finally:
        loop.close()


async def _main(shared_queues) -> None:
    manager = WebRTCManager(lambda name: shared_queues.latest_frames.get(name))
    sig_in: queue.Queue = shared_queues.webrtc_signal_in
    sig_out: queue.Queue = shared_queues.webrtc_signal_out

    loop = asyncio.get_running_loop()
    print("[WebRTC] server started (3 cameras, lazy VP8 encoding)")
    while not shared_queues.exit_event.is_set():
        try:
            # Blocking get on a worker thread — no busy-poll. The timeout bounds
            # how long we wait before re-checking exit_event.
            item = await loop.run_in_executor(None, sig_in.get, True, 0.5)
        except queue.Empty:
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
        if offer_sdp is not None:  # None = no-reneg active-set change, no offer to send
            sig_out.put_nowait({"kind": "offer", "sdp": offer_sdp, "client_id": item.get("client_id", "")})
    elif kind == "answer":
        await manager.on_answer(item.get("sdp", ""))
    elif kind == "ice":
        await manager.on_ice(item.get("payload", {}))
