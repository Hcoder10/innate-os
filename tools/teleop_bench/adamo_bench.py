"""Adamo transport benchmark (their hosted Zenoh-over-QUIC network).

Same 38-byte/200Hz echo protocol as the rest of teleop_bench, but over
Adamo's cloud routers, so it measures their real relay path end-to-end.

Requires: pip install adamo, and ADAMO_API_KEY in the environment.

  # robot side
  ADAMO_API_KEY=ak_... python adamo_bench.py reflect [--mode client|peer]

  # operator side (Mac)
  ADAMO_API_KEY=ak_... python adamo_bench.py drive [--mode client|peer] \
      [--rate 200 --duration 20 --json-out results.jsonl]

Control packets use priority 10 / express / best-effort (per Adamo's own
control-channel guidance); the echo path uses the same settings.
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from common import RttStats, pack_control, print_summary, save_result, unpack_control  # noqa: E402

import adamo  # noqa: E402

CTRL = "bench/ctrl"
ECHO = "bench/echo"
PUB_OPTS = dict(raw=True, priority=10, express=True, reliable=False)


def connect(args):
    session = adamo.connect(api_key=os.environ["ADAMO_API_KEY"],
                            protocol=args.protocol, mode=args.mode)
    time.sleep(2.0)  # discovery/admission settle
    rtt = session.relay_rtt()
    print(f"[adamo] connected mode={args.mode} protocol={args.protocol} "
          f"relay_rtt={rtt * 1000:.1f}ms" if rtt else
          f"[adamo] connected mode={args.mode} protocol={args.protocol}")
    return session


def reflect(args):
    session = connect(args)
    pub = session.publisher(ECHO, **PUB_OPTS)
    n = 0

    def on_sample(sample):
        nonlocal n
        pub.put(bytes(sample.payload))
        n += 1
        if n % 1000 == 0:
            print(f"[adamo] echoed {n}")

    session.subscribe(CTRL, raw=True, callback=on_sample)
    print(f"[adamo] echoing {CTRL} -> {ECHO}")
    while True:
        time.sleep(3600)


def drive(args):
    session = connect(args)
    stats = RttStats(stale_ms=args.stale_ms)
    pending = {}
    pub = session.publisher(CTRL, **PUB_OPTS)

    def on_sample(sample):
        try:
            seq, _, _ = unpack_control(bytes(sample.payload))
        except Exception:
            return
        t0 = pending.pop(seq, None)
        if t0 is not None:
            stats.add((time.perf_counter_ns() - t0) / 1e6)

    session.subscribe(ECHO, raw=True, callback=on_sample)
    time.sleep(1.0)

    interval = 1.0 / args.rate
    start = time.perf_counter()
    next_t = start
    end = start + args.duration
    seq = 0
    while time.perf_counter() < end:
        pending[seq] = time.perf_counter_ns()
        pub.put(pack_control(seq))
        seq += 1
        next_t += interval
        d = next_t - time.perf_counter()
        if d > 0:
            time.sleep(d)
    time.sleep(args.drain)

    s = stats.summary(seq, args.duration)
    s["transport"] = args.label or f"adamo {args.mode}/{args.protocol}"
    s["rate_hz"] = args.rate
    rtt = session.relay_rtt()
    if rtt:
        s["relay_rtt_ms"] = round(rtt * 1000, 2)
    print_summary(s["transport"], s)
    if rtt:
        print(f"  relay RTT (SDK-reported): {rtt * 1000:.1f}ms")
    if args.json_out:
        save_result(args.json_out, s)
    session.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("role", choices=["reflect", "drive"])
    ap.add_argument("--mode", default="client", choices=["client", "peer"],
                    help="client = through Adamo cloud router (WAN path)")
    ap.add_argument("--protocol", default="quic",
                    choices=["quic", "udp", "tcp"])
    ap.add_argument("--rate", type=float, default=200.0)
    ap.add_argument("--duration", type=float, default=20.0)
    ap.add_argument("--drain", type=float, default=1.0)
    ap.add_argument("--stale-ms", type=float, default=60.0)
    ap.add_argument("--json-out", default=None)
    ap.add_argument("--label", default=None)
    args = ap.parse_args()
    if "ADAMO_API_KEY" not in os.environ:
        sys.exit("set ADAMO_API_KEY")
    if args.role == "reflect":
        reflect(args)
    else:
        drive(args)


if __name__ == "__main__":
    main()
