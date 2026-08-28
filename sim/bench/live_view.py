#!/usr/bin/env python3
"""Watch a live run in a browser, from the robot's own camera.

WHY NOT THE BUILT-IN WEB VIEWER. The webapp's 3D scene renders a fixed
apartment asset bundle baked into the image; it does not follow
VIRTUAL_MARS_ASSETS. Open it during a cafe episode and you watch a robot walk
around an apartment it is not in -- which is worse than having no view, because
it looks authoritative. (Its front door is https://localhost, by the way; 0.0.0.0
is a bind address, not somewhere a browser can go.)

The robot's camera does not have that problem. MuJoCo renders it from the world
that is actually loaded, and it is the exact image the brain reasons over -- so
if the picture looks wrong, the run IS wrong.

This runs on the HOST, not in the container, and needs no new port mapping: it
talks to the rosbridge already published on 127.0.0.1:9090, the same socket the
challenge engine uses. It subscribes to the camera, the odometry, the robot's
speech and its skill events, and serves them as an MJPEG stream plus a small
status panel.

  usage: live_view.py [port]        then open http://localhost:<port>
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from websockets.sync.client import connect

BRIDGE = "ws://127.0.0.1:9090"
CAM = "/mars/main_camera/left/image_raw/compressed"
ODOM = "/odom"
CHAT = "/brain/chat_out"
SKILL = "/brain/skill_status_update"
THROTTLE_MS = 100  # 10 fps to the browser; the camera itself runs ~9.6 Hz
LOG_LINES = 14
RECV_TIMEOUT_S = 5.0  # so a dead peer surfaces instead of blocking forever
STALE_AFTER_S = 20.0  # nothing at all for this long: assume the stack went away


class Feed:
    """Latest frame and a short rolling log, filled by the bridge thread."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.jpeg: bytes | None = None
        self.seq = 0
        self.pose = (0.0, 0.0, 0.0)
        self.log: list[str] = []
        self.frames = 0
        self.started = time.time()

    def say(self, line: str) -> None:
        with self.lock:
            self.log.append(f"{time.time() - self.started:6.1f}s  {line}")
            del self.log[:-LOG_LINES]


def _payload(data) -> bytes:
    """rosbridge sends uint8[] as base64; tolerate a plain list too."""
    if isinstance(data, str):
        return base64.b64decode(data)
    return bytes(data)


def _yaw(q) -> float:
    import math

    return math.degrees(
        math.atan2(
            2.0 * (q["w"] * q["z"] + q["x"] * q["y"]),
            1.0 - 2.0 * (q["y"] ** 2 + q["z"] ** 2),
        )
    )


def pump(feed: Feed) -> None:
    """Reconnect forever. The viewer must survive a runtime restart, since
    restarting the stack between maps is the normal way to run this bench."""
    subs = [
        {"op": "subscribe", "topic": CAM, "type": "sensor_msgs/CompressedImage",
         "throttle_rate": THROTTLE_MS, "queue_length": 1},
        {"op": "subscribe", "topic": ODOM, "type": "nav_msgs/Odometry",
         "throttle_rate": 200, "queue_length": 1},
        {"op": "subscribe", "topic": CHAT, "type": "std_msgs/String"},
        {"op": "subscribe", "topic": SKILL, "type": "std_msgs/String"},
    ]
    while True:
        try:
            with connect(BRIDGE, open_timeout=10, max_size=None) as ws:
                for s in subs:
                    ws.send(json.dumps(s))
                feed.say("connected to rosbridge")
                last_data = time.time()
                while True:
                    # recv WITH a timeout, and a staleness watchdog on top.
                    # `for raw in ws` blocks forever, so when the stack is
                    # restarted under it -- which this bench does eight times a
                    # run -- the peer vanishes without a clean close, no
                    # exception is ever raised, and the reconnect loop below
                    # never gets a turn. The symptom is a viewer that looks
                    # connected, keeps serving its last frame, and silently
                    # stops updating.
                    try:
                        raw = ws.recv(timeout=RECV_TIMEOUT_S)
                    except TimeoutError:
                        if time.time() - last_data > STALE_AFTER_S:
                            raise TimeoutError(
                                f"no data for {STALE_AFTER_S:.0f}s") from None
                        continue
                    last_data = time.time()
                    frame = json.loads(raw)
                    topic, msg = frame.get("topic"), frame.get("msg")
                    if not topic or msg is None:
                        continue
                    if topic == CAM:
                        with feed.lock:
                            feed.jpeg = _payload(msg["data"])
                            feed.seq += 1
                            feed.frames += 1
                    elif topic == ODOM:
                        p = msg["pose"]["pose"]
                        with feed.lock:
                            feed.pose = (p["position"]["x"], p["position"]["y"],
                                         _yaw(p["orientation"]))
                    elif topic == CHAT:
                        said = json.loads(msg["data"])
                        who = said.get("sender") or "system"
                        text = str(said.get("text", "")).strip()
                        if text:
                            feed.say(f"{who}: {text}")
                    elif topic == SKILL:
                        ev = json.loads(msg["data"])
                        name = ev.get("skill") or ev.get("name") or ev.get("type", "?")
                        feed.say(f"skill {name} -> {ev.get('status', ev.get('type', ''))}")
        except Exception as exc:  # noqa: BLE001 -- a viewer never kills a run
            feed.say(f"bridge down ({type(exc).__name__}), retrying")
            time.sleep(2.0)


PAGE = """<!doctype html><meta charset=utf-8><title>robot camera (live)</title>
<style>
 body{margin:0;background:#111418;color:#dfe3ea;font:14px ui-monospace,Menlo,Consolas,monospace}
 header{padding:10px 14px;border-bottom:1px solid #262b33}
 h1{margin:0;font-size:15px;font-weight:600}
 .sub{color:#8b94a3;font-size:12px;margin-top:3px}
 .wrap{display:flex;flex-wrap:wrap;gap:14px;padding:14px;align-items:flex-start}
 img{width:min(760px,96vw);border:1px solid #262b33;border-radius:6px;background:#000;display:block}
 .panel{flex:1;min-width:290px}
 .k{color:#8b94a3} .v{color:#7fd4a1}
 pre{white-space:pre-wrap;background:#171b21;border:1px solid #262b33;border-radius:6px;padding:10px;margin:8px 0 0;max-height:340px;overflow:auto}
</style>
<header><h1>robot camera &mdash; live</h1>
<div class=sub>MuJoCo renders this from the world that is actually loaded; it is the
image the brain reasons over. Not the webapp's baked apartment scene.</div></header>
<div class=wrap>
 <img src="/stream" alt="camera">
 <div class=panel>
  <div><span class=k>pose</span> <span class=v id=pose>&hellip;</span></div>
  <div><span class=k>frames</span> <span class=v id=frames>0</span></div>
  <pre id=log>waiting for the bridge&hellip;</pre>
 </div>
</div>
<script>
// A <img src="/stream"> that dies is never retried by the browser: it just
// keeps showing its last frame. The bench restarts the stack once per map, so
// that WILL happen -- watch the server's frame counter and re-point the img
// when it stops advancing, otherwise the page looks live while being frozen.
let lastFrames=-1, stallTicks=0;
const img=document.querySelector('img');
setInterval(async()=>{try{
 const s=await (await fetch('/state')).json();
 pose.textContent=`x ${s.x.toFixed(2)}  y ${s.y.toFixed(2)}  yaw ${s.yaw.toFixed(0)}°`;
 frames.textContent=s.frames;
 log.textContent=s.log.join('\\n')||'(quiet)';
 if(s.frames===lastFrames){
   if(++stallTicks===12){ img.src='/stream?'+Date.now(); }   // ~6s of nothing
 } else { stallTicks=0; lastFrames=s.frames; }
}catch(e){}},500);
</script>
"""


class Handler(BaseHTTPRequestHandler):
    feed: Feed

    def log_message(self, *_a) -> None:  # keep the console for the bench
        pass

    def do_GET(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's name
        if self.path.startswith("/stream"):
            return self._stream()
        if self.path.startswith("/state"):
            with self.feed.lock:
                body = json.dumps({
                    "x": self.feed.pose[0], "y": self.feed.pose[1],
                    "yaw": self.feed.pose[2], "frames": self.feed.frames,
                    "log": list(self.feed.log),
                }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream(self) -> None:
        self.send_response(200)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        sent = -1
        try:
            while True:
                with self.feed.lock:
                    jpeg, seq = self.feed.jpeg, self.feed.seq
                if jpeg is None or seq == sent:
                    time.sleep(0.03)
                    continue
                sent = seq
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                 b"Content-Length: " + str(len(jpeg)).encode()
                                 + b"\r\n\r\n" + jpeg + b"\r\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the tab was closed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("port", nargs="?", type=int, default=8088)
    port = ap.parse_args().port
    feed = Feed()
    Handler.feed = feed
    threading.Thread(target=pump, args=(feed,), daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"live view on http://localhost:{port}   (ctrl-c to stop)", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
