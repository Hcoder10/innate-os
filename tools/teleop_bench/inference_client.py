"""Remote-inference simulator client (runs on the robot).

Simulates the observation->action cycle of cloud policy inference:
sends a camera-frame-sized payload + robot state to the /infer endpoint on
the relay server, which sleeps `--compute-ms` (simulated GPU forward pass)
and returns an action chunk (`--chunk` steps x `--dof` floats).

Reports the full cycle latency distribution and, given the execution rate,
whether action-chunked control (ACT-style) can run without starvation:
a chunk of H steps executed at exec_hz lasts H/exec_hz seconds; streaming
inference works if cycle_latency < chunk_duration (the next chunk arrives
before the current one runs out).

Examples:
  # 60KB frames (~720p JPEG), 100ms simulated compute, 50-step chunks
  python inference_client.py --relay ws://VM_IP:8765 --compute-ms 100

  # video-transport proxy: 30fps frames, no compute
  python inference_client.py --relay ws://VM_IP:8765 --compute-ms 0 \
      --rate 30 --frame-kb 60
"""

import argparse
import asyncio
import json
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from common import RttStats, print_summary, save_result  # noqa: E402


async def run(args):
    import websockets

    frame = os.urandom(args.frame_kb * 1024)  # sim server never decodes it
    stats = RttStats(stale_ms=args.stale_ms)
    url = f"{args.relay}/infer"
    sent = 0
    t_start = time.perf_counter()

    async with websockets.connect(url, compression=None,
                                  max_size=16 * 1024 * 1024) as ws:
        interval = 1.0 / args.rate if args.rate > 0 else 0.0
        pipelined = args.rate > 0

        async def send_one(seq):
            meta = json.dumps({
                "seq": seq,
                "t_send_ns": time.perf_counter_ns(),
                "compute_ms": args.compute_ms,
                "chunk": args.chunk,
                "dof": args.dof,
            }).encode()
            await ws.send(struct.pack("<I", len(meta)) + meta + frame)

        async def recv_one():
            msg = await ws.recv()
            (meta_len,) = struct.unpack_from("<I", msg, 0)
            meta = json.loads(msg[4:4 + meta_len])
            stats.add((time.perf_counter_ns() - meta["t_send_ns"]) / 1e6)

        if pipelined:
            # fixed-rate pipelined sends (video-proxy / continuous mode)
            async def recv_loop():
                while True:
                    await recv_one()

            recv_task = asyncio.create_task(recv_loop())
            next_t = time.perf_counter()
            end = next_t + args.duration
            while time.perf_counter() < end:
                await send_one(sent)
                sent += 1
                next_t += interval
                d = next_t - time.perf_counter()
                if d > 0:
                    await asyncio.sleep(d)
            await asyncio.sleep(args.drain)
            recv_task.cancel()
        else:
            # closed-loop: send next observation as soon as actions arrive
            end = time.perf_counter() + args.duration
            while time.perf_counter() < end:
                await send_one(sent)
                sent += 1
                await recv_one()

    dur = time.perf_counter() - t_start
    s = stats.summary(sent, dur)
    s["transport"] = "ws-infer"
    s["mode"] = "pipelined" if args.rate > 0 else "closed-loop"
    s["frame_kb"] = args.frame_kb
    s["compute_ms"] = args.compute_ms
    s["chunk"] = args.chunk
    label = (f"inference {args.frame_kb}KB frame, {args.compute_ms}ms compute, "
             f"{s['mode']}")
    print_summary(label, s)

    if s["rtt_ms"]:
        chunk_ms = args.chunk / args.exec_hz * 1000.0
        p99 = s["rtt_ms"]["p99"]
        s["chunk_duration_ms"] = chunk_ms
        s["chunk_ok_p99"] = p99 < chunk_ms
        print(f"  action-chunk check: {args.chunk} steps @ {args.exec_hz}Hz "
              f"= {chunk_ms:.0f}ms per chunk; cycle p99={p99:.0f}ms -> "
              f"{'OK: streaming inference sustains control' if p99 < chunk_ms else 'STARVED: chunk shorter than inference cycle'}")
        net_ms = s["rtt_ms"]["p50"] - args.compute_ms
        print(f"  network share of cycle (p50 - compute): ~{net_ms:.0f}ms")

    if args.json_out:
        save_result(args.json_out, s)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--relay", default="ws://localhost:8765")
    ap.add_argument("--frame-kb", type=int, default=60,
                    help="observation payload size (60 ~ 720p JPEG)")
    ap.add_argument("--compute-ms", type=float, default=100.0,
                    help="simulated policy forward-pass time on the server")
    ap.add_argument("--chunk", type=int, default=50,
                    help="action chunk length returned per inference")
    ap.add_argument("--dof", type=int, default=7)
    ap.add_argument("--exec-hz", type=float, default=50.0,
                    help="rate at which the robot executes chunk steps")
    ap.add_argument("--rate", type=float, default=0.0,
                    help="fixed send rate (fps). 0 = closed-loop mode")
    ap.add_argument("--duration", type=float, default=20.0)
    ap.add_argument("--drain", type=float, default=2.0)
    ap.add_argument("--stale-ms", type=float, default=500.0)
    ap.add_argument("--json-out", default=None)
    args = ap.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
