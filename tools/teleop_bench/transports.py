"""Bidirectional packet links used by the live teleop demo
(leader_link.py on the operator machine, follower_bridge.py on the robot).

Each link exposes:
    await link.start(on_packet)   # on_packet(bytes) called for every inbound
    await link.send(data)         # fire-and-forget outbound
    await link.close()

Direction is symmetric; the operator sends on the "ctrl" lane and receives
the "echo" lane, the robot the reverse. For lane-less transports (udp,
webrtc) both directions share the socket/channel.
"""

import asyncio
import json
import os
import socket


class UdpLink:
    """Operator: connect to host:port. Robot: listen on port."""

    def __init__(self, host=None, port=9996, listen=False):
        self.host, self.port, self.listen = host, port, listen
        self.transport = None
        self._peer = None

    async def start(self, on_packet):
        loop = asyncio.get_running_loop()
        outer = self

        class Proto(asyncio.DatagramProtocol):
            def datagram_received(self, data, addr):
                outer._peer = addr
                on_packet(data)

        if self.listen:
            self.transport, _ = await loop.create_datagram_endpoint(
                Proto, local_addr=("0.0.0.0", self.port))
        else:
            self.transport, _ = await loop.create_datagram_endpoint(
                Proto, remote_addr=(socket.gethostbyname(self.host), self.port),
                family=socket.AF_INET)

    async def send(self, data):
        if self.listen:
            if self._peer:
                self.transport.sendto(data, self._peer)
        else:
            self.transport.sendto(data)

    async def close(self):
        if self.transport:
            self.transport.close()


class WsPairLink:
    """Both sides connect out to relay_server /pair/{session}/{role}."""

    def __init__(self, relay, session, role):
        self.url = f"{relay}/pair/{session}/{role}"
        self.ws = None
        self._task = None

    async def start(self, on_packet):
        import websockets
        self.ws = await websockets.connect(self.url, compression=None,
                                           max_size=None)

        async def rx():
            async for msg in self.ws:
                if isinstance(msg, (bytes, bytearray)):
                    on_packet(bytes(msg))

        self._task = asyncio.create_task(rx())

    async def send(self, data):
        await self.ws.send(data)

    async def close(self):
        if self._task:
            self._task.cancel()
        if self.ws:
            await self.ws.close()


class WebRtcLink:
    """DataChannel (unordered, no retransmits). Operator offers, robot answers.
    Signaling rides a WsPairLink on session '<session>-sig'."""

    def __init__(self, relay, session, role, ice_mode="all",
                 stun="stun:stun.l.google.com:19302", turn=None,
                 turn_user="bench", turn_pass="benchpw"):
        self.relay, self.session, self.role = relay, session, role
        self.ice_mode, self.stun = ice_mode, stun
        self.turn, self.turn_user, self.turn_pass = turn, turn_user, turn_pass
        self.pc = None
        self.channel = None

    def _config(self):
        from aiortc import RTCConfiguration, RTCIceServer
        servers = []
        if self.stun:
            servers.append(RTCIceServer(urls=[self.stun]))
        if self.turn:
            servers.append(RTCIceServer(urls=[self.turn],
                                        username=self.turn_user,
                                        credential=self.turn_pass))
        return RTCConfiguration(iceServers=servers)

    async def start(self, on_packet):
        import websockets
        from aiortc import RTCPeerConnection, RTCSessionDescription
        from reflector import filter_remote_sdp

        sig_url = f"{self.relay}/pair/{self.session}-sig/{self.role}"
        ws = await websockets.connect(sig_url, compression=None, max_size=None)
        self.pc = RTCPeerConnection(configuration=self._config())
        open_evt = asyncio.Event()

        def wire(channel):
            self.channel = channel

            @channel.on("open")
            def _():
                open_evt.set()

            @channel.on("message")
            def _(message):
                if isinstance(message, bytes):
                    on_packet(message)

            if channel.readyState == "open":
                open_evt.set()

        if self.role == "operator":
            wire(self.pc.createDataChannel("teleop", ordered=False,
                                           maxRetransmits=0))
            await self.pc.setLocalDescription(await self.pc.createOffer())
            await ws.send(json.dumps({"type": "offer",
                                      "sdp": self.pc.localDescription.sdp}))
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") == "answer":
                    await self.pc.setRemoteDescription(RTCSessionDescription(
                        sdp=filter_remote_sdp(msg["sdp"], self.ice_mode),
                        type="answer"))
                    break
        else:
            @self.pc.on("datachannel")
            def _(channel):
                wire(channel)

            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") == "offer":
                    await self.pc.setRemoteDescription(RTCSessionDescription(
                        sdp=filter_remote_sdp(msg["sdp"], self.ice_mode),
                        type="offer"))
                    await self.pc.setLocalDescription(await self.pc.createAnswer())
                    await ws.send(json.dumps(
                        {"type": "answer", "sdp": self.pc.localDescription.sdp}))
                    break
        await asyncio.wait_for(open_evt.wait(), timeout=60)
        await ws.close()

    async def send(self, data):
        if self.channel and self.channel.readyState == "open":
            self.channel.send(data)

    async def close(self):
        if self.pc:
            await self.pc.close()


class ZenohLink:
    def __init__(self, connect, role):
        self.connect = connect
        self.pub_key = "teleop/ctrl" if role == "operator" else "teleop/echo"
        self.sub_key = "teleop/echo" if role == "operator" else "teleop/ctrl"
        self.session = None
        self.pub = None

    async def start(self, on_packet):
        import zenoh
        conf = zenoh.Config()
        if self.connect:
            conf.insert_json5("mode", '"client"')
            conf.insert_json5("connect/endpoints", json.dumps([self.connect]))
        self.session = zenoh.open(conf)
        self.pub = self.session.declare_publisher(
            self.pub_key, congestion_control=zenoh.CongestionControl.DROP,
            priority=zenoh.Priority.REAL_TIME, express=True)
        loop = asyncio.get_running_loop()
        self.session.declare_subscriber(
            self.sub_key,
            lambda s: loop.call_soon_threadsafe(on_packet, s.payload.to_bytes()))
        await asyncio.sleep(1.0)

    async def send(self, data):
        self.pub.put(data)

    async def close(self):
        if self.session:
            self.session.close()


class LiveKitLink:
    def __init__(self, url, room, role, key="devkey", secret="secret"):
        self.url, self.room_name, self.role = url, room, role
        self.key, self.secret = key, secret
        self.pub_topic = "ctrl" if role == "operator" else "echo"
        self.sub_topic = "echo" if role == "operator" else "ctrl"
        self.room = None

    async def start(self, on_packet):
        from livekit import api, rtc
        token = (api.AccessToken(self.key, self.secret)
                 .with_identity(self.role)
                 .with_grants(api.VideoGrants(room_join=True,
                                              room=self.room_name)).to_jwt())
        self.room = rtc.Room()

        @self.room.on("data_received")
        def _(packet):
            if packet.topic == self.sub_topic:
                on_packet(bytes(packet.data))

        await self.room.connect(self.url, token)

    async def send(self, data):
        await self.room.local_participant.publish_data(
            data, reliable=False, topic=self.pub_topic)

    async def close(self):
        if self.room:
            await self.room.disconnect()


class AdamoLink:
    def __init__(self, role, mode="client", protocol="quic"):
        self.role, self.mode, self.protocol = role, mode, protocol
        self.pub_key = "teleop/ctrl" if role == "operator" else "teleop/echo"
        self.sub_key = "teleop/echo" if role == "operator" else "teleop/ctrl"
        self.session = None
        self.pub = None

    async def start(self, on_packet):
        import adamo
        self.session = adamo.connect(api_key=os.environ["ADAMO_API_KEY"],
                                     protocol=self.protocol, mode=self.mode)
        await asyncio.sleep(2.0)
        self.pub = self.session.publisher(self.pub_key, raw=True, priority=10,
                                          express=True, reliable=False)
        loop = asyncio.get_running_loop()
        self.session.subscribe(
            self.sub_key, raw=True,
            callback=lambda s: loop.call_soon_threadsafe(
                on_packet, bytes(s.payload)))
        rtt = self.session.relay_rtt()
        if rtt:
            print(f"[adamo] relay_rtt={rtt * 1000:.1f}ms")

    async def send(self, data):
        self.pub.put(data)

    async def close(self):
        if self.session:
            self.session.close()


def make_link(args, role):
    t = args.transport
    if t == "udp":
        return UdpLink(host=getattr(args, "host", None), port=args.port,
                       listen=(role == "robot"))
    if t == "ws":
        return WsPairLink(args.relay, args.session, role)
    if t == "webrtc":
        return WebRtcLink(args.relay, args.session, role,
                          ice_mode=args.ice_mode, stun=args.stun,
                          turn=args.turn, turn_user=args.turn_user,
                          turn_pass=args.turn_pass)
    if t == "zenoh":
        return ZenohLink(args.connect, role)
    if t == "livekit":
        return LiveKitLink(args.lk_url, args.room, role,
                           key=args.lk_key, secret=args.lk_secret)
    if t == "adamo":
        return AdamoLink(role, mode=args.adamo_mode)
    raise ValueError(t)


def add_link_args(ap):
    ap.add_argument("--transport", required=True,
                    choices=["udp", "ws", "webrtc", "zenoh", "livekit", "adamo"])
    ap.add_argument("--host", default="mars-the-27th.local",
                    help="udp: robot hostname (operator side)")
    ap.add_argument("--port", type=int, default=9996,
                    help="udp: demo port (NOT 9999)")
    ap.add_argument("--relay", default="ws://localhost:8765")
    ap.add_argument("--session", default="drive")
    ap.add_argument("--ice-mode", default="all", choices=["all", "srflx", "relay"])
    ap.add_argument("--stun", default="stun:stun.l.google.com:19302")
    ap.add_argument("--turn", default=None)
    ap.add_argument("--turn-user", default="bench")
    ap.add_argument("--turn-pass", default="benchpw")
    ap.add_argument("--connect", default=None,
                    help="zenoh: router endpoint tcp/IP:7447")
    ap.add_argument("--lk-url", default="ws://localhost:7880")
    ap.add_argument("--lk-key", default="devkey")
    ap.add_argument("--lk-secret", default="secret")
    ap.add_argument("--room", default="drive")
    ap.add_argument("--adamo-mode", default="client", choices=["client", "peer"])
