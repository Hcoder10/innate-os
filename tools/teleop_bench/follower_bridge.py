"""Robot-side bridge for LIVE teleop over any benchmark transport.

Receives 38-byte leader packets over the chosen transport and forwards them
to the production UDP leader receiver on 127.0.0.1:9999 — so the real arm
follows, exactly as if the phone app were driving. Every packet is also
echoed back so the operator side displays live RTT.

Safety:
  * bursts are collapsed — only the NEWEST packet is forwarded (a stale
    backlog can never replay into the arm)
  * a reset packet (0xAA56) is sent to :9999 on startup so sequence
    tracking starts clean
  * on stream stall > --stall-ms nothing is forwarded (receiver holds pose;
    the existing behavior) and a warning is logged

Run on the robot, e.g.:
  .venv/bin/python follower_bridge.py --transport webrtc \
      --relay ws://VM:8765 --turn turn:VM:3478
"""

import argparse
import asyncio
import socket
import struct
import time

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))
from common import MAGIC, PACKET_SIZE, unpack_control  # noqa: E402
from transports import add_link_args, make_link  # noqa: E402

RESET_MAGIC = 0xAA56


async def main():
    ap = argparse.ArgumentParser(description=__doc__)
    add_link_args(ap)
    ap.add_argument("--forward-host", default="127.0.0.1")
    ap.add_argument("--forward-port", type=int, default=9999)
    ap.add_argument("--stall-ms", type=float, default=300.0)
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dest = (args.forward_host, args.forward_port)
    sock.sendto(struct.pack("<HI", RESET_MAGIC, 0), dest)
    print(f"[bridge] reset sent to {dest}")

    latest = {"seq": -1, "data": None, "t": 0.0}
    counters = {"rx": 0, "fwd": 0, "collapsed": 0}
    link = make_link(args, role="robot")

    def on_packet(data: bytes):
        # echo everything back for operator-side RTT display
        asyncio.ensure_future(link.send(data))
        if len(data) < PACKET_SIZE:
            if len(data) >= 6 and struct.unpack_from("<H", data)[0] == RESET_MAGIC:
                sock.sendto(data, dest)
            return
        try:
            seq, _, _ = unpack_control(data)
        except ValueError:
            return
        counters["rx"] += 1
        if seq > latest["seq"]:
            latest.update(seq=seq, data=data, t=time.monotonic())

    await link.start(on_packet)
    print(f"[bridge] link up ({args.transport}); forwarding newest packets "
          f"to {dest}, stall gate {args.stall_ms}ms")

    forwarded_seq = -1
    stalled = False
    last_report = time.monotonic()
    while True:
        await asyncio.sleep(0.0025)  # 400Hz forward loop
        now = time.monotonic()
        if latest["seq"] > forwarded_seq:
            age_ms = (now - latest["t"]) * 1000.0
            if age_ms <= args.stall_ms:
                skipped = latest["seq"] - forwarded_seq - 1
                if forwarded_seq >= 0 and skipped > 0:
                    counters["collapsed"] += skipped
                sock.sendto(latest["data"], dest)
                forwarded_seq = latest["seq"]
                counters["fwd"] += 1
                stalled = False
        elif latest["seq"] >= 0 and not stalled and \
                (now - latest["t"]) * 1000.0 > args.stall_ms:
            stalled = True
            print(f"[bridge] STALL >{args.stall_ms:.0f}ms — holding pose")
        if now - last_report > 5.0:
            print(f"[bridge] rx={counters['rx']} fwd={counters['fwd']} "
                  f"collapsed={counters['collapsed']}")
            last_report = now


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
