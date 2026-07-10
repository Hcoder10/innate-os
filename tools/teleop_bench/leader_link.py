"""Operator-side LIVE teleop: read the leader arm plugged into this
computer and stream it to MARS over any benchmark transport, with a live
RTT HUD. Pair with follower_bridge.py on the robot.

  # LAN UDP (baseline feel)
  python leader_link.py --source arm --transport udp --host mars-the-27th.local

  # the RFC path (WebRTC DC via cloud signaling; add --ice-mode relay to
  # feel the forced-TURN worst case)
  python leader_link.py --source arm --transport webrtc \
      --relay ws://VM:8765 --turn turn:VM:3478

  # Adamo's network
  ADAMO_API_KEY=ak_... python leader_link.py --source arm --transport adamo

No leader arm handy? `--source sine` moves the arm gently around the
NEUTRAL pose (it will first move to neutral!) so you can still feel latency.

Leader arm: Dynamixel bus (ids 1-6, 1M baud), torque off, positions read
raw (0..4096) at --rate Hz and sent in the production 38-byte packet.
"""

import argparse
import asyncio
import math
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from common import PACKET_SIZE, pack_control, unpack_control  # noqa: E402
from transports import add_link_args, make_link  # noqa: E402

POSITION_ADDR = 132
POSITION_LEN = 4


class LeaderArm:
    def __init__(self, device, baud, ids):
        from dynamixel_sdk import (COMM_SUCCESS, GroupSyncRead,  # noqa: F401
                                   PacketHandler, PortHandler)
        self.ids = ids
        self.port = PortHandler(device)
        self.packet = PacketHandler(2.0)
        if not self.port.openPort():
            raise RuntimeError(f"cannot open {device}")
        if not self.port.setBaudRate(baud):
            raise RuntimeError(f"cannot set baud {baud}")
        self.reader = GroupSyncRead(self.port, self.packet,
                                    POSITION_ADDR, POSITION_LEN)
        for i in ids:
            self.reader.addParam(i)
        self._comm_success = COMM_SUCCESS

    def missing_servos(self):
        """Ping each servo; returns the list of ids that don't answer."""
        missing = []
        for i in self.ids:
            _, rc, _ = self.packet.ping(self.port, i)
            if rc != self._comm_success:
                missing.append(i)
        return missing

    def read_positions(self):
        if self.reader.txRxPacket() != self._comm_success:
            return None
        out = []
        for i in self.ids:
            if not self.reader.isAvailable(i, POSITION_ADDR, POSITION_LEN):
                return None
            v = self.reader.getData(i, POSITION_ADDR, POSITION_LEN)
            if v > 0x7FFFFFFF:  # sign
                v -= 0x100000000
            out.append(int(v))
        return out


def autodetect_device():
    import glob
    for pat in ("/dev/tty.usbmodem*", "/dev/tty.usbserial*", "/dev/ttyACM*",
                "/dev/ttyUSB*"):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[0]
    return None


async def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_link_args(ap)
    ap.add_argument("--source", default="arm", choices=["arm", "sine"])
    ap.add_argument("--device", default=None,
                    help="leader serial device (auto-detect if omitted)")
    ap.add_argument("--baud", type=int, default=1000000)
    ap.add_argument("--ids", default="1,2,3,4,5,6")
    ap.add_argument("--rate", type=float, default=100.0)
    ap.add_argument("--sine-amp", type=float, default=150.0,
                    help="sine mode: amplitude in raw units on the wrist")
    args = ap.parse_args()

    ids = [int(x) for x in args.ids.split(",")]
    arm = None
    if args.source == "arm":
        device = args.device or autodetect_device()
        if not device:
            sys.exit("no serial device found; pass --device")
        print(f"[leader] reading {ids} on {device} @ {args.baud}")
        arm = LeaderArm(device, args.baud, ids)
        first = arm.read_positions()
        if first is None:
            sys.exit("leader arm not responding (check power/cable/ids)")
        print(f"[leader] initial pose: {first}")
    else:
        print("[leader] SINE mode: arm will move gently around NEUTRAL "
              "(2048^6). Ctrl-C to stop.")

    rtts = []
    pending = {}

    def on_packet(data: bytes):
        if len(data) < PACKET_SIZE:
            return
        try:
            seq, _, _ = unpack_control(data)
        except ValueError:
            return
        t0 = pending.pop(seq, None)
        if t0 is not None:
            rtts.append((time.perf_counter_ns() - t0) / 1e6)

    link = make_link(args, role="operator")
    await link.start(on_packet)
    print(f"[leader] link up ({args.transport}); streaming at {args.rate}Hz. "
          "Move the leader arm!")

    interval = 1.0 / args.rate
    seq = 0
    next_t = time.perf_counter()
    last_hud = time.perf_counter()
    t0 = time.perf_counter()
    sent_window = 0
    try:
        while True:
            if arm:
                positions = arm.read_positions()
                if positions is None:
                    await asyncio.sleep(0.02)
                    continue
            else:
                t = time.perf_counter() - t0
                positions = [2048] * 6
                positions[4] = int(2048 + args.sine_amp * math.sin(2 * math.pi * 0.25 * t))
                positions[5] = int(2048 + args.sine_amp * math.sin(2 * math.pi * 0.15 * t))
            pending[seq] = time.perf_counter_ns()
            await link.send(pack_control(seq, positions))
            seq += 1
            sent_window += 1

            now = time.perf_counter()
            if now - last_hud >= 1.0:
                window = rtts[-500:]
                if window:
                    w = sorted(window)
                    p50 = w[len(w) // 2]
                    p95 = w[int(len(w) * 0.95) - 1]
                    stale = 100.0 * sum(1 for x in w if x > 60) / len(w)
                    print(f"[hud] {sent_window / (now - last_hud):5.0f}Hz out | "
                          f"RTT p50 {p50:6.1f}ms p95 {p95:6.1f}ms | "
                          f">60ms {stale:4.1f}% | echoes {len(rtts)}")
                else:
                    print(f"[hud] {sent_window / (now - last_hud):5.0f}Hz out | "
                          "no echoes yet (bridge running?)")
                rtts[:] = rtts[-500:]
                for k in [k for k in pending if k < seq - 1000]:
                    pending.pop(k, None)
                sent_window = 0
                last_hud = now

            next_t += interval
            d = next_t - time.perf_counter()
            if d > 0:
                await asyncio.sleep(d)
            else:
                next_t = time.perf_counter()
    finally:
        await link.close()
        print("\n[leader] stopped — robot holds last pose.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
