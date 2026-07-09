"""Robot-side echo reflector: receives control packets over the chosen
transport and echoes them straight back, so the operator can measure RTT.

Transports:
  udp      listen on --port (default 9998) and echo datagrams
  ws       connect OUT to relay --relay ws://HOST:8765 as role "robot",
           echo binary frames (models the innate-cloud proxy path)
  webrtc   answer a WebRTC offer signaled through the relay /pair endpoint,
           echo on the "control" DataChannel (unordered, no retransmits)
  quic     QUIC datagram echo server on --port (Kyber-class transport)
  livekit  join a LiveKit room, echo lossy data packets
  zenoh    subscribe bench/ctrl, publish echoes on bench/echo
           (Adamo-class transport; --connect tcp/HOST:7447 for a WAN router)

Run on the robot. Only stdlib needed for udp; see requirements.txt for others.
"""

import argparse
import asyncio
import json
import sys

sys.path.insert(0, __import__("os").path.dirname(__file__))
from common import PACKET_SIZE  # noqa: E402


# ---------------------------------------------------------------- udp
async def run_udp(args):
    loop = asyncio.get_running_loop()

    class Echo(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, data, addr):
            self.transport.sendto(data, addr)

    transport, _ = await loop.create_datagram_endpoint(
        Echo, local_addr=("0.0.0.0", args.port))
    print(f"[udp] echoing on :{args.port}")
    await asyncio.Future()


# ---------------------------------------------------------------- ws
async def run_ws(args):
    import websockets
    url = f"{args.relay}/pair/{args.session}/robot"
    async for ws in websockets.connect(url, compression=None, max_size=None):
        print(f"[ws] connected to {url}")
        try:
            async for msg in ws:
                await ws.send(msg)
        except websockets.ConnectionClosed:
            print("[ws] connection closed, reconnecting")
            continue


# ---------------------------------------------------------------- webrtc
def filter_remote_sdp(sdp: str, ice_mode: str) -> str:
    """Drop remote ICE candidates so pairing is forced onto srflx/relay paths."""
    if ice_mode == "all":
        return sdp
    out = []
    for line in sdp.split("\r\n"):
        if line.startswith("a=candidate:") and " typ " in line:
            typ = line.split(" typ ")[1].split()[0]
            if ice_mode == "relay" and typ != "relay":
                continue
            if ice_mode == "srflx" and typ == "host":
                continue
        out.append(line)
    return "\r\n".join(out)


def build_ice_config(args):
    from aiortc import RTCConfiguration, RTCIceServer
    servers = []
    if args.stun:
        servers.append(RTCIceServer(urls=[args.stun]))
    if args.turn:
        servers.append(RTCIceServer(urls=[args.turn],
                                    username=args.turn_user,
                                    credential=args.turn_pass))
    return RTCConfiguration(iceServers=servers)


async def run_webrtc(args):
    import websockets
    from aiortc import RTCPeerConnection, RTCSessionDescription

    url = f"{args.relay}/pair/{args.session}/robot"
    async with websockets.connect(url, compression=None, max_size=None) as ws:
        print(f"[webrtc] signaling via {url}, waiting for offer "
              f"(ice-mode={args.ice_mode})")
        pc = RTCPeerConnection(configuration=build_ice_config(args))
        done = asyncio.Event()

        @pc.on("datachannel")
        def on_channel(channel):
            print(f"[webrtc] datachannel '{channel.label}' open")

            @channel.on("message")
            def on_message(message):
                if isinstance(message, bytes) and len(message) >= PACKET_SIZE:
                    channel.send(message)

        @pc.on("connectionstatechange")
        async def on_state():
            print(f"[webrtc] state={pc.connectionState}")
            if pc.connectionState in ("failed", "closed"):
                done.set()

        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") == "offer":
                sdp = filter_remote_sdp(msg["sdp"], args.ice_mode)
                await pc.setRemoteDescription(
                    RTCSessionDescription(sdp=sdp, type="offer"))
                await pc.setLocalDescription(await pc.createAnswer())
                await ws.send(json.dumps({
                    "type": "answer", "sdp": pc.localDescription.sdp}))
                print("[webrtc] answer sent")
            elif msg.get("type") == "bye":
                break
        await done.wait()
        await pc.close()


# ---------------------------------------------------------------- quic
async def run_quic(args):
    import ipaddress
    import datetime
    import os
    import tempfile
    from aioquic.asyncio import serve as quic_serve
    from aioquic.asyncio.protocol import QuicConnectionProtocol
    from aioquic.quic.configuration import QuicConfiguration
    from aioquic.quic.events import DatagramFrameReceived
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    # self-signed cert on the fly
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "bench")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=30))
            .add_extension(x509.SubjectAlternativeName(
                [x509.DNSName("bench"),
                 x509.IPAddress(ipaddress.ip_address("0.0.0.0"))]),
                critical=False)
            .sign(key, hashes.SHA256()))
    tmp = tempfile.mkdtemp()
    cert_pem = os.path.join(tmp, "cert.pem")
    key_pem = os.path.join(tmp, "key.pem")
    with open(cert_pem, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_pem, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM,
                                  serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))

    class EchoProtocol(QuicConnectionProtocol):
        def quic_event_received(self, event):
            if isinstance(event, DatagramFrameReceived):
                self._quic.send_datagram_frame(event.data)
                self.transmit()

    cfg = QuicConfiguration(is_client=False, max_datagram_frame_size=65536)
    cfg.load_cert_chain(cert_pem, key_pem)
    await quic_serve("0.0.0.0", args.port, configuration=cfg,
                     create_protocol=EchoProtocol)
    print(f"[quic] datagram echo on :{args.port}")
    await asyncio.Future()


# ---------------------------------------------------------------- livekit
async def run_livekit(args):
    from livekit import api, rtc

    token = (api.AccessToken(args.lk_key, args.lk_secret)
             .with_identity("robot")
             .with_grants(api.VideoGrants(room_join=True, room=args.room))
             .to_jwt())
    room = rtc.Room()
    loop = asyncio.get_running_loop()

    @room.on("data_received")
    def on_data(packet: rtc.DataPacket):
        if packet.topic == "ctrl":
            asyncio.run_coroutine_threadsafe(
                room.local_participant.publish_data(
                    packet.data, reliable=False, topic="echo"), loop)

    await room.connect(args.lk_url, token)
    print(f"[livekit] joined room '{args.room}' at {args.lk_url}")
    await asyncio.Future()


# ---------------------------------------------------------------- zenoh
async def run_zenoh(args):
    import zenoh

    conf = zenoh.Config()
    if args.connect:
        conf.insert_json5("mode", '"client"')
        conf.insert_json5("connect/endpoints", json.dumps([args.connect]))
    session = zenoh.open(conf)
    pub = session.declare_publisher(
        "bench/echo",
        congestion_control=zenoh.CongestionControl.DROP,
        priority=zenoh.Priority.REAL_TIME,
        express=True)

    def on_sample(sample):
        pub.put(sample.payload.to_bytes())

    session.declare_subscriber("bench/ctrl", on_sample)
    mode = f"client via {args.connect}" if args.connect else "peer (multicast)"
    print(f"[zenoh] echoing bench/ctrl -> bench/echo ({mode})")
    await asyncio.Future()


RUNNERS = {"udp": run_udp, "ws": run_ws, "webrtc": run_webrtc,
           "quic": run_quic, "livekit": run_livekit, "zenoh": run_zenoh}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--transport", required=True, choices=RUNNERS)
    ap.add_argument("--port", type=int, default=9998)
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
    args = ap.parse_args()
    try:
        asyncio.run(RUNNERS[args.transport](args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
