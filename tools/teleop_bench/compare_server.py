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
import struct
import time
import types

from aiohttp import web

sys_dir = os.path.dirname(os.path.abspath(__file__))
import sys  # noqa: E402
sys.path.insert(0, sys_dir)
from common import (pack_control, unpack_control, unpack_video_chunk,  # noqa: E402
                    ECHO_TS_SIZE, PACKET_SIZE, VIDEO_HDR_SIZE, VIDEO_MAGIC)
from transports import make_link  # noqa: E402

VM = os.environ.get("TELEOP_VM", "35.233.134.107")

_key_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".adamo_key")
if not os.environ.get("ADAMO_API_KEY") and os.path.exists(_key_file):
    os.environ["ADAMO_API_KEY"] = open(_key_file).read().strip()
ROBOT = os.environ.get("TELEOP_ROBOT", "mars-the-27th.local")
ROBOT_USER = "jetson1"
ROBOT_PASS = os.environ.get("TELEOP_ROBOT_PASS", "goodbot27")
PORT = 8399
# The RAW topic — same source the webapp's H.264 streamer uses. The bridge
# subscribes with rclpy and JPEG-encodes at the full camera rate (~14fps on
# MARS); the /compressed republisher only runs at half that.
VIDEO_TOPIC = os.environ.get(
    "TELEOP_VIDEO_TOPIC", "/mars/main_camera/left/image_raw")
# 0 = ship every camera frame (~14fps × ~25KB jpeg-q80 ≈ 3Mbps). A nonzero
# throttle near the camera's frame interval ALIASES the rate down — that is
# how an early 150ms default turned the 7fps compressed topic into 3.6.
VIDEO_MS = float(os.environ.get("TELEOP_VIDEO_MS", "0"))
# Sourced before the bridge starts so rclpy (raw video path) works; harmless
# if absent — the bridge falls back to the rosbridge /compressed path.
ROBOT_ROS_ENV = ("export INNATE_OS_ROOT=$HOME/innate-os; "
                 "[ -f ~/innate-os/ros2_ws/install/setup.zsh ] && { "
                 "source ~/innate-os/config/dds/setup_dds.zsh 2>/dev/null; "
                 "source ~/innate-os/ros2_ws/install/setup.zsh; }; ")

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


TICK = 2 * 3.141592653589793 / 4096  # rad per servo tick


async def _rosbridge_ws():
    import socket
    import aiohttp
    try:
        ip = socket.gethostbyname(ROBOT)
    except OSError:
        ip = ROBOT
    http = aiohttp.ClientSession()
    try:
        ws = await http.ws_connect(f"ws://{ip}:9090", timeout=8)
    except Exception:
        await http.close()
        raise
    return http, ws


async def call_ros_service(service: str, srv_type: str, args: dict | None,
                           timeout_s: float = 12.0):
    """One-shot rosbridge service call; returns the `values` dict or None.
    Only existing robot services are used — recovery and motion planning stay
    the robot's job (same services the webapp/mobile app buttons call)."""
    try:
        http, ws = await _rosbridge_ws()
    except Exception as e:
        print(f"[ros] connect failed: {e}")
        return None
    try:
        req = {"op": "call_service", "service": service, "id": "dash",
               "type": srv_type}
        if args:
            req["args"] = args
        await ws.send_json(req)
        async with asyncio.timeout(timeout_s):
            async for msg in ws:
                m = json.loads(msg.data)
                if m.get("op") == "service_response" and m.get("id") == "dash":
                    return m.get("values", {})
    except Exception as e:
        print(f"[ros] {service} failed: {e}")
        return None
    finally:
        await ws.close()
        await http.close()


async def get_arm_status():
    """Latest /mars/arm/status (is_ok, error, is_torque_enabled) or None."""
    try:
        http, ws = await _rosbridge_ws()
    except Exception:
        return None
    try:
        await ws.send_json({"op": "subscribe", "topic": "/mars/arm/status",
                            "queue_length": 1})
        async with asyncio.timeout(8):
            async for msg in ws:
                m = json.loads(msg.data)
                if m.get("op") == "publish":
                    return m["msg"]
    except Exception:
        return None
    finally:
        await ws.close()
        await http.close()


async def goto_js(target_rad: list, secs: float) -> bool:
    """Planned move via the robot's own /mars/arm/goto_js. The robot applies
    its joint limits itself and ramps there — call it with ANY target (even
    the arm's current out-of-envelope pose) and it lands smoothly on the
    nearest commandable pose. No limit math on our side."""
    v = await call_ros_service(
        "/mars/arm/goto_js", "mars_msgs/srv/GotoJS",
        {"data": {"data": target_rad}, "time": secs}, timeout_s=secs + 10)
    return bool(v and v.get("success"))


async def prepare_arm(sess) -> bool:
    """Get the arm streamable using the robot's existing recovery services —
    the same ones behind the webapp buttons: fix_error clears latched servo
    overloads, torque_on syncs goals to the current pose (holds, no snap)."""
    st = await get_arm_status()
    if st is None:
        sess.arm_note = "rosbridge (:9090) unreachable — arm state unknown"
        return True  # stream anyway; dry-run etc. still useful
    actions = []
    if not st.get("is_ok"):
        actions.append(f"fixing '{st.get('error')}'")
        sess.status = f"arm recovery: {st.get('error')} — calling fix_error"
        await call_ros_service("/mars/arm/fix_error", "std_srvs/srv/Trigger", None)
        await asyncio.sleep(2.0)
        st = await get_arm_status() or {}
    if not st.get("is_torque_enabled"):
        actions.append("torque on")
        sess.status = "arm recovery: enabling torque (holds current pose)"
        await call_ros_service("/mars/arm/torque_on", "std_srvs/srv/Trigger", None)
        await asyncio.sleep(1.0)
        st = await get_arm_status() or {}
    if not st.get("is_ok"):
        sess.arm_note = (f"arm error persists: {st.get('error')} — "
                         "it may be stuck under load; reposition it by hand")
        return False
    sess.arm_note = "; ".join(actions) if actions else None
    return True


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
        self.arm_note = None
        # NTP-style clock sync from timestamped control echoes:
        # (t_mono, rtt_wall_ms, offset_ms) — the min-RTT sample wins
        self.sync = []
        # video: chunk reassembly + one-way latency of completed frames
        self.vassy = {}           # frame_seq -> {t_mono, ts_ms, total, chunks}
        self.vlat = []            # (t_recv_mono, latency_ms)
        self.vframes = 0
        self.vdropped = 0
        self.jpeg = None
        self.jpeg_seq = 0

    def clock_offset(self):
        """robot_clock - operator_clock (ms), from the best recent echo."""
        now = time.monotonic()
        recent = [s for s in self.sync[-2000:] if now - s[0] < 10.0]
        if not recent:
            return None
        return min(recent, key=lambda s: s[1])[2]

    def on_video_chunk(self, data: bytes):
        try:
            frame_seq, ts_ms, total, idx, payload = unpack_video_chunk(data)
        except (ValueError, struct.error):
            return
        now = time.monotonic()
        entry = self.vassy.setdefault(
            frame_seq, {"t": now, "ts_ms": ts_ms, "total": total, "chunks": {}})
        entry["chunks"][idx] = payload
        if len(entry["chunks"]) == entry["total"]:
            del self.vassy[frame_seq]
            if frame_seq <= self.jpeg_seq:
                return  # stale frame overtaken by a newer one
            self.jpeg = b"".join(entry["chunks"][i] for i in range(entry["total"]))
            self.jpeg_seq = frame_seq
            self.vframes += 1
            offset = self.clock_offset()
            if offset is not None:
                self.vlat.append((now, time.time() * 1000.0 - (ts_ms - offset)))
                if len(self.vlat) > 2000:
                    del self.vlat[:1000]
        # frames with chunks missing for >2s are lost, not late
        for k in [k for k, v in self.vassy.items() if now - v["t"] > 2.0]:
            del self.vassy[k]
            self.vdropped += 1

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
            "arm_note": self.arm_note,
        }
        if vals:
            out.update(
                p50=round(vals[len(vals) // 2], 1),
                p95=round(vals[max(0, int(len(vals) * 0.95) - 1)], 1),
                maxx=round(vals[-1], 1),
                stale_pct=round(100 * sum(1 for v in vals if v > 60) / len(vals), 1),
                echo_hz=round(len(recent) / 5.0, 1),
            )
        vrecent = sorted(l for t, l in self.vlat[-200:] if now - t < 5.0)
        if vrecent:
            done = self.vframes + self.vdropped
            out.update(
                video_p50=round(vrecent[len(vrecent) // 2], 1),
                video_p95=round(vrecent[max(0, int(len(vrecent) * 0.95) - 1)], 1),
                video_fps=round(len(vrecent) / 5.0, 1),
                video_seq=self.jpeg_seq,
                video_kb=round(len(self.jpeg) / 1024, 1) if self.jpeg else None,
                video_drop_pct=round(100 * self.vdropped / done, 1) if done else 0,
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
        f"{ROBOT_ROS_ENV}"
        f"cd ~/teleop_bench && ({env}PYTHONUNBUFFERED=1 nohup .venv/bin/python "
        f"follower_bridge.py {spec['bridge']} --forward-port {fwd} "
        f"--video-topic {VIDEO_TOPIC} --video-ms {VIDEO_MS:.0f} "
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
            if (len(data) >= VIDEO_HDR_SIZE
                    and struct.unpack_from("<H", data)[0] == VIDEO_MAGIC):
                sess.on_video_chunk(data)
                return
            if len(data) < PACKET_SIZE:
                return
            try:
                seq, t_send_ms, _ = unpack_control(data)
            except ValueError:
                return
            t0 = pending.pop(seq, None)
            if t0 is not None:
                sess.rtts.append((time.monotonic(),
                                  (time.perf_counter_ns() - t0) / 1e6))
                if len(sess.rtts) > 5000:
                    del sess.rtts[:2500]
            if len(data) >= PACKET_SIZE + ECHO_TS_SIZE:
                # bridge appended its wall clock: NTP-style offset sample
                (t_robot_ms,) = struct.unpack_from("<d", data, PACKET_SIZE)
                t_now_ms = time.time() * 1000.0
                sess.sync.append((time.monotonic(), t_now_ms - t_send_ms,
                                  t_robot_ms - (t_send_ms + t_now_ms) / 2))
                if len(sess.sync) > 5000:
                    del sess.sync[:2500]

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

    blend_from = None
    if live:
        # Bring the arm to a streamable state and to the start pose using the
        # robot's OWN machinery (webapp-button services + its trajectory
        # planner with its own joint limits) — never a raw command step:
        # streaming a pose far from the arm is the current-limit yank.
        if not await prepare_arm(sess):
            await stop_session_keep_note(sess.arm_note)
            return
        target = None
        if sess.arm:
            pos0 = sess.arm.read_positions()
            if pos0:
                target = [(t - 2048) * TICK for t in pos0]
        elif center:
            target = [(t - 2048) * TICK for t in center]
        if target:
            sess.status = "moving arm to start pose (robot-planned, 3s)"
            await goto_js(target, 3.0)
            # wherever the robot's limits actually put it is the start pose
            check = await get_arm_pose_ticks()
            if check:
                if sess.arm:
                    blend_from = check
                else:
                    center = check

    async def pump():
        import math
        sess.status = "streaming"
        seq = 0
        t0 = time.perf_counter()
        next_t = t0
        base = center or [2048] * 6
        # leader mode: even after the robot-planned move to the leader's
        # pose, the residual gap (robot-side limits) is taken over smoothly —
        # first 1.5s lerps our reference from the arm's actual pose to the
        # live leader stream instead of stepping (the production app snaps
        # here; blending costs nothing and spares the shoulder)
        BLEND_S = 1.5
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
                t = time.perf_counter() - t0
                if blend_from and t < BLEND_S:
                    k = t / BLEND_S
                    pos = [int(b + (p - b) * k)
                           for b, p in zip(blend_from, pos)]
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
    await robot_sh("pkill -f 'follower_bridg[e]'; sleep 0.7; "
                   "pkill -9 -f 'follower_bridg[e]'; true")


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


async def h_frame(request):
    sess = current.get("session")
    if not sess or not sess.jpeg:
        return web.Response(status=204)
    return web.Response(body=sess.jpeg, content_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})


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
    app.router.add_get("/api/frame", h_frame)
    app.router.add_post("/api/start", h_start)
    app.router.add_post("/api/stop", h_stop)

    async def on_shutdown(app):
        await stop_session()

    app.on_shutdown.append(on_shutdown)
    print(f"teleop compare dashboard: http://localhost:{PORT}")
    web.run_app(app, host="127.0.0.1", port=PORT, print=None)


if __name__ == "__main__":
    main()
