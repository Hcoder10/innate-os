"""Teleop transport comparison dashboard (runs on the operator's Mac).

Serves http://localhost:8399 with:
  * recorded benchmark results (results.jsonl, local + robot's copy)
  * one-click LIVE sessions: picks a transport, starts follower_bridge on
    the robot over SSH, streams the leader arm (or a safe sine) through it,
    and shows live RTT — switch transports and compare side by side.

Safety: sessions default to DRY RUN (bridge forwards to dead port 9995).
The "live arm" toggle forwards to the production :9999 input instead.

Usage:
  .venv/bin/python compare_server.py            # then open localhost:8399
  ADAMO_API_KEY=ak_... .venv/bin/python compare_server.py   # enables adamo
"""

import asyncio
import json
import os
import time
import types

from aiohttp import web

sys_dir = os.path.dirname(os.path.abspath(__file__))
import sys  # noqa: E402
sys.path.insert(0, sys_dir)
from common import pack_control, unpack_control, PACKET_SIZE  # noqa: E402
from transports import make_link  # noqa: E402

VM = os.environ.get("TELEOP_VM", "35.233.134.107")

_key_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".adamo_key")
if not os.environ.get("ADAMO_API_KEY") and os.path.exists(_key_file):
    os.environ["ADAMO_API_KEY"] = open(_key_file).read().strip()
ROBOT = os.environ.get("TELEOP_ROBOT", "mars-the-27th.local")
ROBOT_USER = "jetson1"
ROBOT_PASS = os.environ.get("TELEOP_ROBOT_PASS", "goodbot27")
PORT = 8399

TRANSPORTS = {
    "udp": {
        "title": "UDP direct (LAN)",
        "note": "today's teleop path — only works on the same network",
        "args": {"transport": "udp", "host": ROBOT, "port": 9996},
        "bridge": "--transport udp --port 9996",
    },
    "webrtc": {
        "title": "WebRTC DataChannel (ICE auto)",
        "note": "the RFC path — P2P when possible, TURN when not",
        "args": {"transport": "webrtc", "relay": f"ws://{VM}:8765",
                 "session": "dash", "ice_mode": "all",
                 "turn": f"turn:{VM}:3478"},
        "bridge": f"--transport webrtc --relay ws://{VM}:8765 --session dash "
                  f"--turn turn:{VM}:3478",
    },
    "webrtc-turn": {
        "title": "WebRTC forced TURN (GCP)",
        "note": "remote worst case: every packet relays through us-west1",
        "args": {"transport": "webrtc", "relay": f"ws://{VM}:8765",
                 "session": "dashturn", "ice_mode": "relay",
                 "turn": f"turn:{VM}:3478"},
        "bridge": f"--transport webrtc --relay ws://{VM}:8765 "
                  f"--session dashturn --turn turn:{VM}:3478 --ice-mode relay",
    },
    "ws": {
        "title": "WebSocket relay (GCP)",
        "note": "innate-cloud proxy pattern (TCP through us-west1)",
        "args": {"transport": "ws", "relay": f"ws://{VM}:8765",
                 "session": "dashws"},
        "bridge": f"--transport ws --relay ws://{VM}:8765 --session dashws",
    },
    "zenoh": {
        "title": "Zenoh router (GCP)",
        "note": "rmw_zenoh's WAN story / Adamo-class, self-hosted",
        "args": {"transport": "zenoh", "connect": f"tcp/{VM}:7447"},
        "bridge": f"--transport zenoh --connect tcp/{VM}:7447",
    },
    "livekit": {
        "title": "LiveKit SFU (GCP)",
        "note": "lossy data channel — drops badly on lossy networks",
        "args": {"transport": "livekit", "lk_url": f"ws://{VM}:7880",
                 "room": "dash"},
        "bridge": f"--transport livekit --lk-url ws://{VM}:7880 --room dash",
    },
    "adamo": {
        "title": "Adamo (hosted routers)",
        "note": "their Zenoh/QUIC network — needs ADAMO_API_KEY",
        "args": {"transport": "adamo", "adamo_mode": "client"},
        "bridge": "--transport adamo",
        "needs_key": True,
    },
}

LINK_DEFAULTS = dict(
    host=ROBOT, port=9996, relay=f"ws://{VM}:8765", session="dash",
    ice_mode="all", stun="stun:stun.l.google.com:19302", turn=None,
    turn_user="bench", turn_pass="benchpw", connect=None,
    lk_url=f"ws://{VM}:7880", lk_key="devkey", lk_secret="secret",
    room="dash", adamo_mode="client")


async def get_arm_pose_ticks():
    """Current arm pose (6 raw ticks) via rosbridge, so demo motion wiggles
    around where the arm actually is instead of snapping to neutral."""
    import math
    import socket
    import aiohttp
    try:
        ip = socket.gethostbyname(ROBOT)
    except OSError:
        ip = ROBOT
    try:
        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(f"ws://{ip}:9090", timeout=8) as ws:
                await ws.send_json({"op": "subscribe", "topic": "/joint_states",
                                    "throttle_rate": 0})
                async with asyncio.timeout(8):
                    async for msg in ws:
                        m = json.loads(msg.data)
                        if m.get("op") == "publish" and m.get("topic") == "/joint_states":
                            rad = m["msg"]["position"][:6]
                            return [int(r / (2 * math.pi / 4096) + 2048) for r in rad]
    except Exception as e:
        print(f"[pose] rosbridge read failed ({e}); using neutral")
    return None


async def robot_sh(cmd: str) -> tuple[int, str]:
    # ssh exits 255 on connection/auth-level failures — those are transient
    # here (WiFi blips, sshd churn), so retry them; remote-command exit
    # codes pass through untouched.
    out = ""
    for attempt in range(3):
        proc = await asyncio.create_subprocess_exec(
            "sshpass", "-p", ROBOT_PASS, "ssh",
            "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
            f"{ROBOT_USER}@{ROBOT}", cmd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        raw, _ = await proc.communicate()
        out = raw.decode(errors="replace")
        if proc.returncode != 255:
            return proc.returncode, out
        print(f"[ssh] attempt {attempt + 1} failed (rc=255): {out.strip()[-120:]}")
        await asyncio.sleep(1.5 * (attempt + 1))
    return 255, out


class Session:
    def __init__(self, key, live, source):
        self.key, self.live, self.source = key, live, source
        self.link = None
        self.task = None
        self.rtts = []            # (t_recv, rtt_ms)
        self.sent = 0
        self.started = time.time()
        self.status = "starting"
        self.arm = None

    def stats(self):
        now = time.monotonic()
        recent = [(t, r) for t, r in self.rtts[-800:] if now - t < 5.0]
        vals = sorted(r for _, r in recent)
        out = {
            "transport": self.key, "live": self.live, "source": self.source,
            "status": self.status, "sent": self.sent,
            "uptime_s": round(time.time() - self.started, 1),
            "samples": [round(r, 1) for _, r in recent[-240:]],
            "leader_pos": getattr(self, "last_pos", None),
        }
        if vals:
            out.update(
                p50=round(vals[len(vals) // 2], 1),
                p95=round(vals[max(0, int(len(vals) * 0.95) - 1)], 1),
                maxx=round(vals[-1], 1),
                stale_pct=round(100 * sum(1 for v in vals if v > 60) / len(vals), 1),
                echo_hz=round(len(recent) / 5.0, 1),
            )
        return out


current: dict = {"session": None}


async def start_session(key: str, live: bool, source: str):
    await stop_session()
    current["note"] = None
    spec = TRANSPORTS[key]
    sess = Session(key, live, source)
    current["session"] = sess

    fwd = 9999 if live else 9995
    env = ""
    if spec.get("needs_key"):
        env = f"ADAMO_API_KEY={os.environ.get('ADAMO_API_KEY', '')} "
    bridge_cmd = (
        f"cd ~/teleop_bench && ({env}PYTHONUNBUFFERED=1 nohup .venv/bin/python "
        f"follower_bridge.py {spec['bridge']} --forward-port {fwd} "
        f"> /tmp/bridge_dash.log 2>&1 </dev/null &); echo started")
    sess.status = "starting robot bridge"
    rc, out = await robot_sh(bridge_cmd)
    if rc != 0:
        sess.status = f"bridge start failed: {out[-200:]}"
        return
    if not live:
        # dry run: make sure something drains the dead port so logs stay clean
        await robot_sh("pgrep -f 'udp_sink_999[5]' >/dev/null || "
                       "(nohup python3 -c \"import socket;"
                       "s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);"
                       "s.bind(('127.0.0.1',9995));"
                       "exec('while True: s.recvfrom(2048)')\" "
                       "> /dev/null 2>&1 </dev/null & echo udp_sink_9995)")

    args = types.SimpleNamespace(**{**LINK_DEFAULTS, **spec["args"]})
    sess.status = "connecting link"
    try:
        sess.link = make_link(args, role="operator")

        def on_packet(data: bytes):
            if len(data) < PACKET_SIZE:
                return
            try:
                seq, _, _ = unpack_control(data)
            except ValueError:
                return
            t0 = pending.pop(seq, None)
            if t0 is not None:
                sess.rtts.append((time.monotonic(),
                                  (time.perf_counter_ns() - t0) / 1e6))
                if len(sess.rtts) > 5000:
                    del sess.rtts[:2500]

        pending: dict[int, int] = {}
        await asyncio.wait_for(sess.link.start(on_packet), timeout=45)
    except Exception as e:
        sess.status = f"link failed: {type(e).__name__}: {e}"
        return

    if source == "arm":
        # no silent sine fallback: if the operator asked for the arm and it
        # is not fully readable, fail loudly with a per-servo diagnosis
        try:
            from leader_link import LeaderArm, autodetect_device
            dev = autodetect_device()
            if not dev:
                raise RuntimeError("no USB serial device found")
            sess.arm = LeaderArm(dev, 1000000, [1, 2, 3, 4, 5, 6])
            if sess.arm.read_positions() is None:
                missing = sess.arm.missing_servos()
                if missing:
                    raise RuntimeError(
                        f"leader servo(s) {missing} not responding — "
                        "check that segment's cable/connector and power")
                raise RuntimeError("leader bus not responding")
        except Exception as e:
            sess.status = f"LEADER ARM FAULT: {e}"
            sess.arm = None
            await stop_session_keep_note(sess.status)
            return

    center = None
    if not sess.arm:
        sess.status = "reading current arm pose"
        center = await get_arm_pose_ticks()

    async def pump():
        import math
        sess.status = "streaming"
        seq = 0
        t0 = time.perf_counter()
        next_t = t0
        base = center or [2048] * 6
        idle_ref, idle_since = None, None
        none_since = None
        while True:
            if sess.arm:
                pos = sess.arm.read_positions()
                if pos is None:
                    now = time.monotonic()
                    if none_since is None:
                        none_since = now
                    elif now - none_since > 2.0:
                        missing = sess.arm.missing_servos()
                        current["note"] = (
                            f"LEADER ARM FAULT mid-session: servo(s) "
                            f"{missing or 'bus'} stopped responding — check cabling")
                        print(f"[session] {current['note']}")
                        asyncio.get_running_loop().create_task(stop_session())
                        return
                    await asyncio.sleep(0.02)
                    continue
                none_since = None
                sess.last_pos = pos
                # auto-stop when the leader sits untouched — a forgotten live
                # session must not keep feeding :9999 while someone teleops
                # from the app (two commanders make the arm fight itself)
                now = time.monotonic()
                if idle_ref is None or any(abs(a - b) > 4 for a, b in zip(pos, idle_ref)):
                    idle_ref, idle_since = pos, now
                elif now - idle_since > 90:
                    current["note"] = "auto-stopped: leader arm idle for 90s"
                    print(f"[session] {current['note']}")
                    asyncio.get_running_loop().create_task(stop_session())
                    return
            else:
                # gentle wiggle around the arm's CURRENT pose (wrist + gripper)
                t = time.perf_counter() - t0
                pos = list(base)
                pos[4] = int(base[4] + 120 * math.sin(2 * math.pi * 0.25 * t))
                pos[5] = int(base[5] + 120 * math.sin(2 * math.pi * 0.4 * t))
            pending[seq] = time.perf_counter_ns()
            try:
                await sess.link.send(pack_control(seq, pos))
            except Exception as e:
                sess.status = f"send error: {e}"
                return
            seq += 1
            sess.sent = seq
            if seq % 500 == 0:
                for k in [k for k in pending if k < seq - 1000]:
                    pending.pop(k, None)
            next_t += 0.01  # 100 Hz
            d = next_t - time.perf_counter()
            if d > 0:
                await asyncio.sleep(d)
            else:
                next_t = time.perf_counter()

    sess.task = asyncio.create_task(pump())


async def stop_session_keep_note(note: str):
    await stop_session()
    current["note"] = note


async def stop_session():
    sess = current.get("session")
    if not sess:
        return
    current["session"] = None
    if sess.task:
        sess.task.cancel()
    if sess.link:
        try:
            await sess.link.close()
        except Exception:
            pass
    await robot_sh("pkill -f 'follower_bridg[e]'; true")


def load_results():
    rows = []
    for path, origin in ((os.path.join(sys_dir, "results.jsonl"), "mac"),
                         (os.path.join(sys_dir, "results_robot.jsonl"), "robot")):
        if os.path.exists(path):
            for line in open(path):
                try:
                    r = json.loads(line)
                    r["origin"] = origin
                    rows.append(r)
                except json.JSONDecodeError:
                    pass
    return rows


# ------------------------------------------------------------------ web
async def h_index(request):
    return web.FileResponse(os.path.join(sys_dir, "compare.html"))


async def h_results(request):
    return web.json_response(load_results())


async def h_config(request):
    import socket
    try:  # browsers don't reliably resolve .local for WebSockets
        robot_ip = socket.gethostbyname(ROBOT)
    except OSError:
        robot_ip = ROBOT
    return web.json_response({"robot": robot_ip})


async def h_transports(request):
    have_key = bool(os.environ.get("ADAMO_API_KEY"))
    out = []
    for k, v in TRANSPORTS.items():
        out.append({"key": k, "title": v["title"], "note": v["note"],
                    "enabled": not (v.get("needs_key") and not have_key)})
    return web.json_response(out)


async def h_start(request):
    body = await request.json()
    key = body.get("transport")
    if key not in TRANSPORTS:
        return web.json_response({"error": "unknown transport"}, status=400)
    asyncio.create_task(start_session(
        key, bool(body.get("live")), body.get("source", "sine")))
    return web.json_response({"ok": True})


async def h_stop(request):
    await stop_session()
    return web.json_response({"ok": True})


async def h_stats(request):
    sess = current.get("session")
    if sess:
        return web.json_response(sess.stats())
    note = current.get("note")
    return web.json_response(
        {"status": f"idle ({note})" if note else "idle"})


def main():
    app = web.Application()
    app.router.add_get("/", h_index)
    app.router.add_get("/api/config", h_config)
    app.router.add_get("/api/results", h_results)
    app.router.add_get("/api/transports", h_transports)
    app.router.add_get("/api/stats", h_stats)
    app.router.add_post("/api/start", h_start)
    app.router.add_post("/api/stop", h_stop)

    async def on_shutdown(app):
        await stop_session()

    app.on_shutdown.append(on_shutdown)
    print(f"teleop compare dashboard: http://localhost:{PORT}")
    web.run_app(app, host="127.0.0.1", port=PORT, print=None)


if __name__ == "__main__":
    main()
