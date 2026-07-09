"""Cloud-side relay + inference simulator (runs on the GCP VM, or anywhere).

Endpoints (single WebSocket server, default port 8765):

  /pair/{session}/{role}   role in {operator, robot}. Forwards every frame
                           (binary or text) to the opposite role. Messages sent
                           before the peer joins are queued. Used both as the
                           "WS relay" transport under test (binary control
                           packets, mimicking the innate-cloud proxy path) and
                           as WebRTC signaling (JSON text).

  /infer                   Remote-inference simulator. Client sends a binary
                           frame:  uint32 meta_len | meta_json | jpeg_bytes
                           meta: {"seq", "t_send_ns", "compute_ms", "chunk", "dof"}
                           Server sleeps compute_ms (simulated GPU forward pass)
                           and replies: uint32 meta_len | meta_json_echo | f32
                           actions (chunk*dof floats).

  /echo                    Echoes binary frames back. Baseline WS RTT to VM.

Dependencies: `pip install websockets` (stdlib otherwise).
"""

import asyncio
import collections
import json
import struct
import time

try:
    from websockets.asyncio.server import serve  # websockets >= 13
except ImportError:  # pragma: no cover - older websockets
    from websockets.server import serve

PORT = 8765

# session -> {"operator": ws|None, "robot": ws|None, "queue": {role: deque}}
_rooms: dict = {}
OTHER = {"operator": "robot", "robot": "operator"}


def _room(session):
    if session not in _rooms:
        _rooms[session] = {"operator": None, "robot": None,
                           "queue": {"operator": collections.deque(),
                                     "robot": collections.deque()}}
    return _rooms[session]


async def _handle_pair(ws, session, role):
    if role not in OTHER:
        await ws.close(code=4000, reason=f"bad role {role}")
        return
    room = _room(session)
    room[role] = ws
    print(f"[pair {session}] {role} joined")
    # flush anything queued for us
    q = room["queue"][role]
    while q:
        await ws.send(q.popleft())
    try:
        async for msg in ws:
            peer = room[OTHER[role]]
            if peer is not None:
                try:
                    await peer.send(msg)
                except Exception:
                    room["queue"][OTHER[role]].append(msg)
            else:
                room["queue"][OTHER[role]].append(msg)
    finally:
        if room[role] is ws:
            room[role] = None
        print(f"[pair {session}] {role} left")


async def _handle_infer(ws):
    print("[infer] client connected")
    async for msg in ws:
        if not isinstance(msg, (bytes, bytearray)):
            continue
        (meta_len,) = struct.unpack_from("<I", msg, 0)
        meta = json.loads(msg[4:4 + meta_len])
        compute_ms = float(meta.get("compute_ms", 0))
        chunk = int(meta.get("chunk", 50))
        dof = int(meta.get("dof", 7))
        if compute_ms > 0:
            await asyncio.sleep(compute_ms / 1000.0)
        reply_meta = json.dumps({
            "seq": meta["seq"],
            "t_send_ns": meta["t_send_ns"],
            "t_server_recv": time.time(),
        }).encode()
        actions = struct.pack(f"<{chunk * dof}f", *([0.0] * (chunk * dof)))
        await ws.send(struct.pack("<I", len(reply_meta)) + reply_meta + actions)
    print("[infer] client left")


async def _handler(ws):
    path = getattr(ws, "path", None)
    if path is None:
        path = ws.request.path
    parts = [p for p in path.split("/") if p]
    try:
        if len(parts) == 3 and parts[0] == "pair":
            await _handle_pair(ws, parts[1], parts[2])
        elif parts == ["infer"]:
            await _handle_infer(ws)
        elif parts == ["echo"]:
            async for msg in ws:
                await ws.send(msg)
        else:
            await ws.close(code=4004, reason=f"unknown path {path}")
    except Exception as e:  # connection drops are routine
        print(f"[{path}] closed: {type(e).__name__}: {e}")


async def main(port=PORT):
    async with serve(_handler, "0.0.0.0", port, max_size=16 * 1024 * 1024,
                     compression=None):
        print(f"relay_server listening on :{port} (pair/infer/echo)")
        await asyncio.Future()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()
    asyncio.run(main(args.port))
