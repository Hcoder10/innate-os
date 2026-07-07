"""The world server: VirtualMars behind a tiny localhost RPC.

The ONLY way the driver node runs the sim -- there is no embedded mode. The
world (physics + sensor rendering) needs no ROS, so it runs wherever the
best GL lives, and only the endpoint changes:

- host placement (default via ./innate-sim up): native GL, ~15ms full-res
  renders on an M1 vs ~105ms software GL in Docker. Docker has no GPU on
  macOS/Windows.
- container placement (CI, no-uv hosts): sim_driver.launch.py starts this
  same server next to the node with --render-scale 2 (software-GL fill-rate
  mitigation); the node can't tell the difference.

Either way the node (a thin client, see remote_world.py) publishes the same
hardware topic contract: RGB is upscaled to the 640x480 wire here before
JPEG encoding, and depth always renders at the pointcloud's native 160x120
(the cloud was a 4x subsample anyway; the node upscales the wire image).

Two interfaces, two kinds of consumers:

- driver RPC (--port): sensing/actuation for the robot adapter (node.py),
  robot-shaped and rate-limited like real hardware. Framing: 4-byte
  big-endian length | JSON request; responses are one JSON frame, followed
  by one binary frame iff the JSON says "blob": <nbytes>.
- observer state stream (--state-port): a WebSocket broadcasting ground
  truth ({t, wall, pose, joints}) after every physics slice, for things
  that WATCH the world rather than run in it -- the webapp's 3D view
  (proxied at /worldstate), future challenge UIs/graders. Robot software
  must never consume this; fidelity lives on the RPC/topic path.

No ROS. Beyond VirtualMars' own deps (mujoco/numpy/PIL) only `websockets`
(already a sim-project and webapp-proxy dependency); if it's missing the
stream is disabled and everything else still runs.

Binds 127.0.0.1 only. GL contexts are macOS-main-thread-sensitive, so all
render work runs on the main thread via a job queue; state reads/writes
take the physics lock directly from connection threads.
"""

import argparse
import json
import socket
import struct
import threading
import time

import numpy as np
from PIL import Image as PILImage

try:
    from websockets.sync.server import serve as ws_serve
except ImportError:  # view-only feature; the sim must not die without it
    ws_serve = None

from .core import CAMERA_HEIGHT, CAMERA_WIDTH, VirtualMars, encode_jpeg

# Depth renders at the pointcloud grid (640/4 x 480/4): the published cloud
# is identical, and the fill cost is 16x less -- see node.py's publish path.
DEPTH_WH = (CAMERA_WIDTH // 4, CAMERA_HEIGHT // 4)


def _read_frame(conn: socket.socket) -> bytes | None:
    header = b""
    while len(header) < 4:
        chunk = conn.recv(4 - len(header))
        if not chunk:
            return None
        header += chunk
    (length,) = struct.unpack(">I", header)
    payload = bytearray()
    while len(payload) < length:
        chunk = conn.recv(min(1 << 16, length - len(payload)))
        if not chunk:
            return None
        payload += chunk
    return bytes(payload)


def _send_frame(conn: socket.socket, payload: bytes) -> None:
    conn.sendall(struct.pack(">I", len(payload)) + payload)


# A camera/depth product keeps rendering this long after its last request,
# so the pump stays lazy (unwatched cameras cost nothing).
PRODUCT_TTL_S = 3.0
PRODUCTS = ("jpeg:main", "jpeg:wrist", "depth:main")


class WorldServer:
    def __init__(self, sim: VirtualMars):
        self.sim = sim
        self.lock = threading.Lock()
        # Advertised in ping replies once the observer stream is listening,
        # so the launcher can tell a current server from a stale one (a
        # pre-stream process would otherwise be reused and the 3D view
        # starves with no diagnosis).
        self.state_port: int | None = None
        # Latest rendered frame per product + when it was last requested.
        # RPCs return the freshest frame instead of triggering a render:
        # sporadic offscreen GL from a background process can stall for
        # minutes on macOS under GPU load (browser contention), while a
        # continuously rendering context stays healthy -- so the pump
        # free-runs and a GL stall degrades freshness, never liveness.
        self.latest: dict[str, tuple[dict, bytes]] = {}
        self.requested_at: dict[str, float] = {}
        self.frame_ready = threading.Condition()
        # Observer state stream: newest ground-truth snapshot + a seq,
        # broadcast to every connected WebSocket (see serve_state).
        self.state_payload = "{}"
        self.state_seq = 0
        self.state_cond = threading.Condition()

    # --- physics (side thread; MuJoCo stepping is pure CPU) ---

    def physics_loop(self) -> None:
        start_wall = time.perf_counter()
        start_sim = self.sim.data.time
        while True:
            target = start_sim + (time.perf_counter() - start_wall)
            stepped = False
            with self.lock:
                behind = target - self.sim.data.time
                if behind > 0.5:
                    # A long stall (App Nap, machine sleep, CPU squeeze) left a
                    # big backlog. Catching up would monopolize the lock for
                    # minutes and starve every render; drop the lost time.
                    print(f"[world-server] dropping {behind:.1f}s of physics backlog after a stall", flush=True)
                    start_wall = time.perf_counter()
                    start_sim = self.sim.data.time
                elif behind > 0:
                    self.sim.step(min(behind, 0.1))
                    stepped = True
            if stepped:
                self.publish_state()
            time.sleep(0.01)

    # --- observer state stream (ground truth for viewers, never for robot code) ---

    def publish_state(self) -> None:
        with self.lock:
            x, y, yaw = self.sim.pose()
            joints = self.sim.joint_positions()
            sim_time = float(self.sim.data.time)
        # `wall` shares the browser's physical clock, so viewers can measure
        # true delivery lag; `t` is the sim clock the playback interpolates on.
        payload = json.dumps({"t": sim_time, "wall": time.time(), "pose": [x, y, yaw], "joints": joints})
        with self.state_cond:
            self.state_payload = payload
            self.state_seq += 1
            self.state_cond.notify_all()

    def serve_state(self, ws) -> None:
        """One observer connection: push each new state as it is produced,
        latest-wins -- a slow client skips intermediate states instead of
        queueing them (pose is state, not a stream)."""
        last_seq = -1
        try:
            while True:
                with self.state_cond:
                    self.state_cond.wait_for(lambda: self.state_seq != last_seq)
                    payload, last_seq = self.state_payload, self.state_seq
                ws.send(payload)
        except Exception:  # noqa: BLE001,S110 -- client gone; the stream just ends
            pass

    # --- renders (main thread only: macOS GL is main-thread-sensitive) ---

    def _render_product(self, product: str) -> None:
        kind, camera = product.split(":")
        if kind == "jpeg":
            with self.lock:
                self.sim.update_camera(camera)
            rgb = self.sim.read_rgb()
            if rgb.shape[0] != CAMERA_HEIGHT:  # scaled render -> wire res
                rgb = np.asarray(PILImage.fromarray(rgb).resize((CAMERA_WIDTH, CAMERA_HEIGHT), PILImage.BILINEAR))
            frame = ({"ok": True}, encode_jpeg(rgb))
        else:  # depth
            with self.lock:
                self.sim.update_depth(camera)
            depth = self.sim.read_depth().astype(np.float32)
            frame = ({"ok": True, "shape": list(depth.shape), "dtype": "float32"}, depth.tobytes())
        with self.frame_ready:
            self.latest[product] = frame
            self.frame_ready.notify_all()

    def render_loop(self) -> None:
        """Free-running frame pump: renders every recently-requested product
        round-robin, as fast as the GPU allows (idle-capped)."""
        while True:
            now = time.monotonic()
            active = [p for p in PRODUCTS if now - self.requested_at.get(p, -1e9) < PRODUCT_TTL_S]
            if not active:
                # Keep the GL channel warm even with no clients: macOS parks
                # an idle offscreen context of a background process, and
                # re-acquiring the GPU under load (busy browser) can stall
                # for minutes with the GIL held. A 5Hz heartbeat render
                # prevents the parking outright.
                try:
                    self._render_product("jpeg:main")
                except Exception:  # noqa: BLE001,S110 -- heartbeat is best-effort
                    pass
                time.sleep(0.2)
                continue
            for product in active:
                try:
                    self._render_product(product)
                except Exception as exc:  # noqa: BLE001 -- the pump must never die
                    with self.frame_ready:
                        self.latest[product] = ({"ok": False, "error": repr(exc)}, None)
                        self.frame_ready.notify_all()
            time.sleep(0.02)  # idle cap; under load the renders self-pace

    def render(self, camera: str, kind: str) -> tuple[dict, bytes | None]:
        """Return the freshest frame for this product (waits only for the
        very first one after activation)."""
        product = f"{kind}:{camera}"
        self.requested_at[product] = time.monotonic()
        with self.frame_ready:
            if product not in self.latest:
                self.frame_ready.wait_for(lambda: product in self.latest, timeout=10.0)
            if product not in self.latest:
                return {"ok": False, "error": "render timeout (no frame produced)"}, None
            meta, blob = self.latest[product]
        return meta, blob

    # --- request handling (one thread per connection) ---

    def handle(self, req: dict) -> tuple[dict, bytes | None]:
        op = req.get("op")
        if op == "ping":
            return {"ok": True, "state_port": self.state_port}, None
        if op == "state":
            with self.lock:
                x, y, yaw = self.sim.pose()
                vx, vy, wz = self.sim.velocity()
                joints = self.sim.joint_positions()
                targets = self.sim.joint_targets()
                sim_time = float(self.sim.data.time)
            return {
                "ok": True,
                "time": sim_time,
                "pose": [x, y, yaw],
                "vel": [vx, vy, wz],
                "joints": joints,
                "targets": targets,
            }, None
        if op == "cmd_vel":
            with self.lock:
                self.sim.set_cmd_vel(float(req["vx"]), float(req["wz"]))
            return {"ok": True}, None
        if op == "joint_targets":
            with self.lock:
                for name, value in req["targets"].items():
                    self.sim.set_joint_target(name, float(value))
            return {"ok": True}, None
        if op == "reset":
            with self.lock:
                self.sim.reset()
            return {"ok": True}, None
        if op == "lidar":
            with self.lock:
                ranges = self.sim.lidar_scan(int(req["n_rays"]), float(req["max_range"]))
            blob = np.asarray(ranges, dtype=np.float32).tobytes()
            return {"ok": True, "dtype": "float32"}, blob
        if op == "render_jpeg":
            return self.render(str(req["camera"]), "jpeg")
        if op == "render_depth":
            return self.render(str(req["camera"]), "depth")
        if op == "shutdown":  # used by the launcher for a clean stop
            return {"ok": True}, None
        return {"ok": False, "error": f"unknown op {op!r}"}, None

    def serve_connection(self, conn: socket.socket) -> None:
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            while True:
                payload = _read_frame(conn)
                if payload is None:
                    return
                try:
                    meta, blob = self.handle(json.loads(payload))
                except Exception as exc:  # noqa: BLE001 -- bad request must not kill the server
                    meta, blob = {"ok": False, "error": repr(exc)}, None
                if blob is not None:
                    meta["blob"] = len(blob)
                _send_frame(conn, json.dumps(meta).encode())
                if blob is not None:
                    _send_frame(conn, blob)
        finally:
            conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument(
        "--state-port",
        type=int,
        default=8800,
        help="Observer state-stream WebSocket port (the webapp proxy's /worldstate assumes this default)",
    )
    parser.add_argument(
        "--render-scale",
        type=int,
        default=1,
        help="Divide the RGB render resolution by N (software-GL mitigation; the wire stays 640x480)",
    )
    args = parser.parse_args()

    print(f"[world-server] loading VirtualMars (render scale {args.render_scale})...", flush=True)
    render_wh = (CAMERA_WIDTH // args.render_scale, CAMERA_HEIGHT // args.render_scale)
    server = WorldServer(VirtualMars(render_wh=render_wh, depth_render_wh=DEPTH_WH))
    server.sim.step(0.5)  # settle from the spawn drop before clients look

    # Boot self-test: prove GL works before accepting clients, and report the
    # per-frame cost so placement decisions are visible in the log.
    t0 = time.perf_counter()
    server.sim.render_rgb("main")
    first_ms = (time.perf_counter() - t0) * 1000
    t1 = time.perf_counter()
    server.sim.render_rgb("main")
    steady_ms = (time.perf_counter() - t1) * 1000
    print(f"[world-server] GL self-test: {steady_ms:.0f} ms/frame (first frame {first_ms:.0f} ms)", flush=True)

    listener = socket.create_server(("127.0.0.1", args.port))
    print(f"[world-server] ready on 127.0.0.1:{args.port}", flush=True)

    server.publish_state()  # observers get a frame before the first physics slice
    if ws_serve is None:
        print("[world-server] `websockets` not installed -- observer state stream disabled", flush=True)
    else:
        state_server = ws_serve(server.serve_state, "127.0.0.1", args.state_port)
        threading.Thread(target=state_server.serve_forever, daemon=True).start()
        server.state_port = args.state_port
        print(f"[world-server] observer state stream on ws://127.0.0.1:{args.state_port}", flush=True)

    threading.Thread(target=server.physics_loop, daemon=True).start()

    def accept_loop() -> None:
        while True:
            conn, _addr = listener.accept()
            threading.Thread(target=server.serve_connection, args=(conn,), daemon=True).start()

    threading.Thread(target=accept_loop, daemon=True).start()
    server.render_loop()  # main thread owns the GL context


if __name__ == "__main__":
    main()
