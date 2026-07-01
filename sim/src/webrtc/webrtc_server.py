"""aiortc WebRTC server core for the sim.

Transport-agnostic: `WebRTCManager` drives one peer connection per browser
(keyed by the webapp's `client_id`) and is fed signaling messages (start /
answer / ice) by whatever carries them — the rosbridge bridge in the live sim,
or plain HTTP in the isolation harness. The server is the OFFERER (matching the
robot protocol): on `start` it builds that client's offer and returns the SDP;
the browser answers. Multiple viewers stream concurrently — a client's START
never disturbs another client's peer.

Three video tracks are always present in the SDP (first_person -> mid 0,
arm_wrist -> mid 1, chase -> mid 2, all sendonly). Encoding is lazy: only the
cameras named in the latest `active_streams` message are fed to the encoder.
"""

import asyncio
import os
import queue
import socket
import threading
from collections.abc import Callable

import numpy as np
from aiortc import RTCConfiguration, RTCIceCandidate, RTCPeerConnection, RTCRtpSender
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


class _Peer:
    """One browser's connection plus its own CameraTracks.

    An aiortc track binds to a single RTCPeerConnection, so peers can't share
    track objects — each peer gets its own set, all pulling from the shared frame
    source (lazy-encoded only while that peer is watching the camera).
    """

    def __init__(self, pc: RTCPeerConnection, tracks: dict[str, CameraTrack]):
        self.pc = pc
        self.tracks = tracks

    def apply_active(self, cameras: list[str]) -> None:
        wanted = set(cameras or [])
        for name, track in self.tracks.items():
            track.set_active(name in wanted)


class WebRTCManager:
    """Multi-peer manager: one RTCPeerConnection per browser, keyed by the
    `client_id` the webapp stamps on every signaling message.

    Several viewers (a teleop tab, the Agent page, a phone) stream concurrently;
    a START only (re)builds or retargets that one client's peer, never the
    others. Each peer offers all cameras up front and encodes lazily, so a camera
    is fed to the encoder only while some peer is actually watching it.
    """

    def __init__(self, get_frame_for: Callable[[str], np.ndarray | None]):
        self._get_frame_for = get_frame_for
        self._peers: dict[str, _Peer] = {}

    async def on_start(self, client_id: str, payload: dict) -> str | None:
        """(Re)build this client's connection + offer, or — for a no-reneg START —
        just retarget which cameras it encodes. Returns the offer SDP, or None when
        no new offer is needed (a no-reneg active-set change)."""
        # `video` is the active (encoded) set the browser wants; fall back to the
        # first camera so something shows before the UI learns the real roster.
        requested = payload.get("video") or []
        active = [c for c in requested if c in CAMERAS] or CAMERAS[:1]

        peer = self._peers.get(client_id)
        # No-reneg START: this client is just switching which cameras we push on its
        # already-negotiated transceivers — flip its active set, emit no new offer.
        if peer is not None and not payload.get("renegotiate"):
            peer.apply_active(active)
            return None

        # (Re)build just this client's connection; other peers are untouched.
        await self._close_peer(client_id)
        peer = await self._build_peer(client_id, active)
        return peer.pc.localDescription.sdp

    async def _build_peer(self, client_id: str, active: list[str]) -> _Peer:
        # No ICE servers: the server is reached directly by host/LAN/public IP, so
        # host candidates suffice. The default config would point aiortc at Google's
        # public STUN, whose unreachable retries crash on teardown (see aioice
        # Transaction.__retry). The local _StunResponder still serves the browser.
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        vp8 = [c for c in RTCRtpSender.getCapabilities("video").codecs if c.mimeType == "video/VP8"]

        # Offer all cameras up front so one that renders its first frame after the
        # offer is still selectable; encoding stays lazy (tracks gated off below).
        tracks: dict[str, CameraTrack] = {}
        for name in CAMERAS:
            track = CameraTrack(lambda n=name: self._get_frame_for(n), name)
            tracks[name] = track
            transceiver = pc.addTransceiver(track, direction="sendonly")
            if vp8:
                transceiver.setCodecPreferences(vp8)

        peer = _Peer(pc, tracks)
        self._peers[client_id] = peer
        peer.apply_active(active)

        @pc.on("connectionstatechange")
        async def _on_state():
            # Tear down only THIS client's pc on a terminal state. Guard against a
            # stale handler: a rebuild replaces the peer, and the old pc's late state
            # change must not evict the new one. "disconnected" is transient.
            if self._peers.get(client_id) is peer and pc.connectionState in ("failed", "closed"):
                await self._close_peer(client_id)

        offer = await pc.createOffer()
        # aiortc gathers ICE within setLocalDescription, so localDescription.sdp
        # already carries the server candidates (non-trickle) — no ice_out needed.
        await pc.setLocalDescription(offer)
        return peer

    async def on_answer(self, client_id: str, sdp: str) -> None:
        peer = self._peers.get(client_id)
        if peer is None or not sdp:
            return
        # Only an offer we just sent expects an answer; a stale/duplicate answer
        # (from handshake churn) would raise "cannot handle answer in state stable".
        if peer.pc.signalingState != "have-local-offer":
            return
        from aiortc import RTCSessionDescription

        await peer.pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="answer"))

    async def on_ice(self, client_id: str, payload: dict) -> None:
        peer = self._peers.get(client_id)
        if peer is None:
            return
        candidate = _parse_ice(payload)
        if candidate is not None:
            await peer.pc.addIceCandidate(candidate)

    def encode_counts(self) -> dict[str, int]:
        # Per-camera frames encoded, summed across peers (diagnostics).
        counts: dict[str, int] = {}
        for peer in self._peers.values():
            for name, track in peer.tracks.items():
                counts[name] = counts.get(name, 0) + track.frames_encoded
        return counts

    async def _close_peer(self, client_id: str) -> None:
        peer = self._peers.pop(client_id, None)
        if peer is not None:
            try:
                await peer.pc.close()
            except Exception:
                pass

    async def close(self) -> None:
        for client_id in list(self._peers):
            await self._close_peer(client_id)


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Local STUN Binding responder.
#
# The native sim ships no STUN server, so a browser reaching it across a network
# (e.g. the GCP demo box) never learns a server-reflexive (srflx) candidate and
# ICE can fail. This minimal IPv4 responder answers a browser's binding request
# with the source address it arrived on as XOR-MAPPED-ADDRESS, so the browser
# emits an srflx candidate for the LAN/public IP that reached us — the same trick
# mars_cam's webrtc_stun.cpp does for the real robot. IPv6 is ignored (host
# candidates cover it). Point the client at stun:<host>:SIM_STUN_PORT.
# --------------------------------------------------------------------------- #
_STUN_BINDING_REQUEST = 0x0001
_STUN_BINDING_SUCCESS = 0x0101
_STUN_ATTR_XOR_MAPPED_ADDRESS = 0x0020
_STUN_MAGIC_COOKIE = 0x2112A442
_STUN_HEADER_SIZE = 20


def _is_stun_binding_request(data: bytes) -> bool:
    if len(data) < _STUN_HEADER_SIZE:
        return False
    # A STUN message type has its top two bits clear; this also filters out
    # RTP/DTLS-ish noise that lands on the same port.
    if data[0] & 0xC0:
        return False
    msg_type = int.from_bytes(data[0:2], "big")
    body_len = int.from_bytes(data[2:4], "big")
    cookie = int.from_bytes(data[4:8], "big")
    if msg_type != _STUN_BINDING_REQUEST or cookie != _STUN_MAGIC_COOKIE:
        return False
    if body_len % 4:
        return False
    return _STUN_HEADER_SIZE + body_len <= len(data)


def _build_ipv4_binding_response(request: bytes, addr: tuple[str, int]) -> bytes:
    ip, port = addr
    out = bytearray(_STUN_HEADER_SIZE + 12)  # header + one XOR-MAPPED-ADDRESS attr
    out[0:2] = _STUN_BINDING_SUCCESS.to_bytes(2, "big")
    out[2:4] = (12).to_bytes(2, "big")  # body: 4-byte attr header + 8-byte value
    out[4:8] = _STUN_MAGIC_COOKIE.to_bytes(4, "big")
    out[8:20] = request[8:20]  # echo the 12-byte transaction id
    out[20:22] = _STUN_ATTR_XOR_MAPPED_ADDRESS.to_bytes(2, "big")
    out[22:24] = (8).to_bytes(2, "big")
    out[24] = 0  # reserved
    out[25] = 0x01  # family: IPv4
    out[26:28] = ((port ^ (_STUN_MAGIC_COOKIE >> 16)) & 0xFFFF).to_bytes(2, "big")
    packed_ip = int.from_bytes(socket.inet_aton(ip), "big")
    out[28:32] = ((packed_ip ^ _STUN_MAGIC_COOKIE) & 0xFFFFFFFF).to_bytes(4, "big")
    return bytes(out)


class _StunResponder(asyncio.DatagramProtocol):
    """Replies to STUN Binding requests; ignores everything else."""

    def __init__(self) -> None:
        self._transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr) -> None:
        # IPv4 addr is (host, port); IPv6 is a 4-tuple — skip it (v4-only, like mars_cam).
        if len(addr) != 2 or self._transport is None or not _is_stun_binding_request(data):
            return
        try:
            self._transport.sendto(_build_ipv4_binding_response(data, addr), addr)
        except OSError as exc:
            print(f"[STUN] failed to reply to {addr}: {exc}")


async def _start_stun_server(loop: asyncio.AbstractEventLoop):
    """Bind the responder on UDP :SIM_STUN_PORT (default 3478). Returns the
    transport to close on shutdown, or None if disabled/unavailable."""
    port = int(os.environ.get("SIM_STUN_PORT", "3478"))
    if not 0 < port <= 65535:
        print(f"[STUN] disabled: invalid SIM_STUN_PORT {port}")
        return None
    try:
        transport, _ = await loop.create_datagram_endpoint(_StunResponder, local_addr=("0.0.0.0", port))
    except OSError as exc:
        print(f"[STUN] failed to bind UDP :{port}: {exc}")
        return None
    print(f"[STUN] Binding responder listening on UDP :{port}")
    return transport


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
    stun_transport = await _start_stun_server(loop)
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

    if stun_transport is not None:
        stun_transport.close()
    await manager.close()


async def _dispatch(manager: WebRTCManager, item: dict, sig_out: queue.Queue) -> None:
    kind = item.get("kind")
    client_id = item.get("client_id", "")
    if kind == "start":
        offer_sdp = await manager.on_start(client_id, item.get("payload", {}))
        if offer_sdp is not None:  # None = no-reneg active-set change, no offer to send
            sig_out.put_nowait({"kind": "offer", "sdp": offer_sdp, "client_id": client_id})
    elif kind == "answer":
        await manager.on_answer(client_id, item.get("sdp", ""))
    elif kind == "ice":
        await manager.on_ice(client_id, item.get("payload", {}))
