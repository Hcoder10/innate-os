"""Operator-side benchmark driver: streams 200 Hz control packets (the real
38-byte teleop packet) over the chosen transport, receives the robot's echo,
and reports RTT / jitter / loss / staleness.

Examples:
  # LAN UDP baseline against reflector on the robot
  python teleop_operator.py --transport udp --host mars-the-27th.local --port 9998

  # WS relay via cloud VM (both sides connect out, like innate-cloud proxy)
  python teleop_operator.py --transport ws --relay ws://VM_IP:8765

  # WebRTC DataChannel; --ice-mode relay forces the TURN path
  python teleop_operator.py --transport webrtc --relay ws://VM_IP:8765 \
      --turn turn:VM_IP:3478 --ice-mode relay

  # LiveKit lossy data via livekit-server on the VM
  python teleop_operator.py --transport livekit --lk-url ws://VM_IP:7880

  # Zenoh via router on the VM (Adamo-class transport)
  python teleop_operator.py --transport zenoh --connect tcp/VM_IP:7447
"""

import argparse
import asyncio
import json
import sys
import time

sys.path.insert(0, __import__("os").path.dirname(__file__))
from common import (PACKET_SIZE, RttStats, pack_control, print_summary,  # noqa: E402
                    save_result, unpack_control)


class Bench:
    """Shared 200 Hz send loop + RTT bookkeeping; transports plug in a
    send() coroutine and call on_echo() for every echoed packet."""

    def __init__(self, args):
        self.args = args
        self.stats = RttStats(stale_ms=args.stale_ms)
        self.pending: dict[int, int] = {}
        self.sent = 0

    def on_echo(self, data: bytes):
        try:
            seq, _, _ = unpack_control(data)
        except Exception:
            return
        t0 = self.pending.pop(seq, None)
        if t0 is not None:
            self.stats.add((time.perf_counter_ns() - t0) / 1e6)

    async def send_loop(self, send):
        interval = 1.0 / self.args.rate
        start = time.perf_counter()
        next_t = start
        end = start + self.args.duration
        seq = 0
        while time.perf_counter() < end:
            self.pending[seq] = time.perf_counter_ns()
            try:
                await send(pack_control(seq))
            except Exception as e:
                print(f"send error: {e}")
                break
            seq += 1
            next_t += interval
            delay = next_t - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
        self.sent = seq
        await asyncio.sleep(self.args.drain)  # let stragglers arrive

    def finish(self, label):
        dur = self.args.duration
        s = self.stats.summary(self.sent, dur)
        s["transport"] = label
        s["rate_hz"] = self.args.rate
        print_summary(label, s)
        if self.args.json_out:
            save_result(self.args.json_out, s)
        return s


# ---------------------------------------------------------------- udp
async def run_udp(args, bench):
    loop = asyncio.get_running_loop()

    class Proto(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            bench.on_echo(data)

    import socket
    transport, _ = await loop.create_datagram_endpoint(
        Proto, remote_addr=(args.host, args.port), family=socket.AF_INET)

    async def send(pkt):
        transport.sendto(pkt)

    await bench.send_loop(send)
    transport.close()
    return f"udp {args.host}:{args.port}"


# ---------------------------------------------------------------- ws (relay)
async def run_ws(args, bench):
    import websockets
    url = f"{args.relay}/pair/{args.session}/operator"
    async with websockets.connect(url, compression=None, max_size=None) as ws:
        async def recv_loop():
            async for msg in ws:
                if isinstance(msg, (bytes, bytearray)):
                    bench.on_echo(msg)

        recv_task = asyncio.create_task(recv_loop())
        await bench.send_loop(ws.send)
        recv_task.cancel()
    return f"ws-relay {args.relay}"


# ---------------------------------------------------------------- ws (direct echo, VM floor)
async def run_wsecho(args, bench):
    import websockets
    url = f"{args.relay}/echo"
    async with websockets.connect(url, compression=None, max_size=None) as ws:
        async def recv_loop():
            async for msg in ws:
                if isinstance(msg, (bytes, bytearray)):
                    bench.on_echo(msg)

        recv_task = asyncio.create_task(recv_loop())
        await bench.send_loop(ws.send)
        recv_task.cancel()
    return f"ws-echo {args.relay}"


# ---------------------------------------------------------------- webrtc
async def run_webrtc(args, bench):
    import websockets
    from aiortc import RTCPeerConnection, RTCSessionDescription
    from reflector import build_ice_config, filter_remote_sdp

    url = f"{args.relay}/pair/{args.session}/operator"
    async with websockets.connect(url, compression=None, max_size=None) as ws:
        pc = RTCPeerConnection(configuration=build_ice_config(args))
        channel = pc.createDataChannel("control", ordered=False,
                                       maxRetransmits=0)
        open_evt = asyncio.Event()

        @channel.on("open")
        def on_open():
            open_evt.set()

        @channel.on("message")
        def on_message(message):
            if isinstance(message, bytes):
                bench.on_echo(message)

        await pc.setLocalDescription(await pc.createOffer())
        await ws.send(json.dumps({"type": "offer",
                                  "sdp": pc.localDescription.sdp}))
        print(f"[webrtc] offer sent (ice-mode={args.ice_mode}), waiting…")
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") == "answer":
                sdp = filter_remote_sdp(msg["sdp"], args.ice_mode)
                await pc.setRemoteDescription(
                    RTCSessionDescription(sdp=sdp, type="answer"))
                break
        await asyncio.wait_for(open_evt.wait(), timeout=30)

        # report which candidate pair won
        pair_desc = "?"
        for t in [pc.sctp.transport] if pc.sctp else []:
            ice = t.transport.iceGatherer.getLocalCandidates()
            pair_desc = ",".join(sorted({c.type for c in ice}))
        print(f"[webrtc] channel open (local candidate types: {pair_desc})")

        async def send(pkt):
            channel.send(pkt)

        await bench.send_loop(send)
        await pc.close()
    return f"webrtc dc ice={args.ice_mode}"


# ---------------------------------------------------------------- quic
async def run_quic(args, bench):
    import ssl
    from aioquic.asyncio import connect as quic_connect
    from aioquic.asyncio.protocol import QuicConnectionProtocol
    from aioquic.quic.configuration import QuicConfiguration
    from aioquic.quic.events import DatagramFrameReceived

    class Proto(QuicConnectionProtocol):
        def quic_event_received(self, event):
            if isinstance(event, DatagramFrameReceived):
                bench.on_echo(event.data)

    import socket
    cfg = QuicConfiguration(is_client=True, max_datagram_frame_size=65536)
    cfg.verify_mode = ssl.CERT_NONE
    host_v4 = socket.gethostbyname(args.host)
    async with quic_connect(host_v4, args.port, configuration=cfg,
                            create_protocol=Proto) as client:
        async def send(pkt):
            client._quic.send_datagram_frame(pkt)
            client.transmit()

        await bench.send_loop(send)
    return f"quic-datagram {args.host}:{args.port}"


# ---------------------------------------------------------------- livekit
async def run_livekit(args, bench):
    from livekit import api, rtc

    token = (api.AccessToken(args.lk_key, args.lk_secret)
             .with_identity("operator")
             .with_grants(api.VideoGrants(room_join=True, room=args.room))
             .to_jwt())
    room = rtc.Room()

    @room.on("data_received")
    def on_data(packet: rtc.DataPacket):
        if packet.topic == "echo":
            bench.on_echo(bytes(packet.data))

    await room.connect(args.lk_url, token)
    print(f"[livekit] joined '{args.room}'; waiting for robot participant…")
    for _ in range(100):
        if room.remote_participants:
            break
        await asyncio.sleep(0.1)

    async def send(pkt):
        await room.local_participant.publish_data(pkt, reliable=False,
                                                  topic="ctrl")

    await bench.send_loop(send)
    await room.disconnect()
    return f"livekit lossy {args.lk_url}"


# ---------------------------------------------------------------- zenoh
async def run_zenoh(args, bench):
    import zenoh

    conf = zenoh.Config()
    if args.connect:
        conf.insert_json5("mode", '"client"')
        conf.insert_json5("connect/endpoints", json.dumps([args.connect]))
    session = zenoh.open(conf)
    pub = session.declare_publisher(
        "bench/ctrl",
        congestion_control=zenoh.CongestionControl.DROP,
        priority=zenoh.Priority.REAL_TIME,
        express=True)
    loop = asyncio.get_running_loop()

    def on_sample(sample):
        loop.call_soon_threadsafe(bench.on_echo, sample.payload.to_bytes())

    session.declare_subscriber("bench/echo", on_sample)
    await asyncio.sleep(1.0)  # discovery

    async def send(pkt):
        pub.put(pkt)

    await bench.send_loop(send)
    session.close()
    mode = args.connect or "peer"
    return f"zenoh {mode}"


RUNNERS = {"udp": run_udp, "ws": run_ws, "wsecho": run_wsecho,
           "webrtc": run_webrtc, "quic": run_quic,
           "livekit": run_livekit, "zenoh": run_zenoh}


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--transport", required=True, choices=RUNNERS)
    ap.add_argument("--host", default="mars-the-27th.local")
    ap.add_argument("--port", type=int, default=9998)
    ap.add_argument("--rate", type=float, default=200.0)
    ap.add_argument("--duration", type=float, default=20.0)
    ap.add_argument("--drain", type=float, default=1.0)
    ap.add_argument("--stale-ms", type=float, default=60.0)
    ap.add_argument("--relay", default="ws://localhost:8765")
    ap.add_argument("--session", default="bench")
    ap.add_argument("--ice-mode", default="all",
                    choices=["all", "srflx", "relay"])
    ap.add_argument("--stun", default="stun:stun.l.google.com:19302")
    ap.add_argument("--turn", default=None)
    ap.add_argument("--turn-user", default="bench")
    ap.add_argument("--turn-pass", default="benchpw")
    ap.add_argument("--lk-url", default="ws://localhost:7880")
    ap.add_argument("--lk-key", default="devkey")
    ap.add_argument("--lk-secret", default="secret")
    ap.add_argument("--room", default="bench")
    ap.add_argument("--connect", default=None,
                    help="zenoh router endpoint, e.g. tcp/1.2.3.4:7447")
    ap.add_argument("--json-out", default=None,
                    help="append summary JSON to this file")
    ap.add_argument("--label", default=None, help="override result label")
    args = ap.parse_args()

    bench = Bench(args)

    async def run():
        label = await RUNNERS[args.transport](args, bench)
        bench.finish(args.label or label)

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
