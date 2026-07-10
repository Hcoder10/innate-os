"""Shared packet format + statistics for teleop transport benchmarks.

The control packet mirrors the production 38-byte UDP leader packet
(`udp_leader_receiver.cpp` / `UDPService.ts`):

    [0:2]   uint16  magic = 0xAA55
    [2:6]   uint32  sequence
    [6:14]  double  timestamp (ms since epoch)
    [14:38] int32x6 servo positions

so every transport is benchmarked with exactly the payload the real
teleop stream uses.
"""

import json
import struct
import time

MAGIC = 0xAA55
PACKET_FMT = "<HId6i"
PACKET_SIZE = struct.calcsize(PACKET_FMT)  # 38 bytes
NEUTRAL = (2048, 2048, 2048, 2048, 2048, 2048)


def pack_control(seq: int, positions=NEUTRAL) -> bytes:
    return struct.pack(PACKET_FMT, MAGIC, seq & 0xFFFFFFFF, time.time() * 1000.0, *positions)


def unpack_control(data: bytes):
    magic, seq, ts_ms, *pos = struct.unpack(PACKET_FMT, data[:PACKET_SIZE])
    if magic != MAGIC:
        raise ValueError(f"bad magic 0x{magic:04X}")
    return seq, ts_ms, pos


# Video frames ride the SAME link as the control stream (robot -> operator),
# chunked so every transport can carry them (UDP datagrams, LiveKit lossy
# data and WebRTC no-retransmit DataChannels all top out near one MTU).
# A lost chunk drops that frame only — exactly how a lossy video channel
# degrades.
VIDEO_MAGIC = 0xAA57
VIDEO_HDR_FMT = "<HIdHH"  # magic, frame seq, robot wall-clock ms, n chunks, chunk idx
VIDEO_HDR_SIZE = struct.calcsize(VIDEO_HDR_FMT)  # 18 bytes
VIDEO_CHUNK_PAYLOAD = 1100

# Control echoes come back as the 38-byte packet + the robot's wall clock
# (ms) — enough for an NTP-style offset estimate, which turns robot frame
# timestamps into one-way video latency on the operator side.
ECHO_TS_FMT = "<d"
ECHO_TS_SIZE = struct.calcsize(ECHO_TS_FMT)


def pack_video_chunks(frame_seq: int, jpeg: bytes) -> list[bytes]:
    total = max(1, (len(jpeg) + VIDEO_CHUNK_PAYLOAD - 1) // VIDEO_CHUNK_PAYLOAD)
    now_ms = time.time() * 1000.0
    return [struct.pack(VIDEO_HDR_FMT, VIDEO_MAGIC, frame_seq & 0xFFFFFFFF,
                        now_ms, total, i)
            + jpeg[i * VIDEO_CHUNK_PAYLOAD:(i + 1) * VIDEO_CHUNK_PAYLOAD]
            for i in range(total)]


def unpack_video_chunk(data: bytes):
    magic, frame_seq, ts_ms, total, idx = struct.unpack_from(VIDEO_HDR_FMT, data)
    if magic != VIDEO_MAGIC:
        raise ValueError(f"bad video magic 0x{magic:04X}")
    return frame_seq, ts_ms, total, idx, data[VIDEO_HDR_SIZE:]


class RttStats:
    """Collects per-packet RTTs and produces a summary dict."""

    def __init__(self, stale_ms: float = 60.0):
        self.rtts_ms: list[float] = []
        self.stale_ms = stale_ms

    def add(self, rtt_ms: float):
        self.rtts_ms.append(rtt_ms)

    @staticmethod
    def _pct(sorted_vals, p):
        if not sorted_vals:
            return None
        k = min(len(sorted_vals) - 1, max(0, round(p / 100 * (len(sorted_vals) - 1))))
        return sorted_vals[k]

    def summary(self, sent: int, duration_s: float) -> dict:
        r = sorted(self.rtts_ms)
        n = len(r)
        jitter = None
        if n > 1:
            # mean absolute consecutive difference, in arrival order
            diffs = [abs(b - a) for a, b in zip(self.rtts_ms, self.rtts_ms[1:])]
            jitter = sum(diffs) / len(diffs)
        return {
            "sent": sent,
            "received": n,
            "loss_pct": round(100.0 * (sent - n) / sent, 3) if sent else None,
            "duration_s": round(duration_s, 2),
            "rtt_ms": None if not n else {
                "mean": round(sum(r) / n, 3),
                "p50": round(self._pct(r, 50), 3),
                "p90": round(self._pct(r, 90), 3),
                "p99": round(self._pct(r, 99), 3),
                "max": round(r[-1], 3),
                "min": round(r[0], 3),
            },
            "jitter_ms": round(jitter, 3) if jitter is not None else None,
            "stale_pct": round(100.0 * sum(1 for x in r if x > self.stale_ms) / n, 3) if n else None,
            "stale_threshold_ms": self.stale_ms,
        }


def print_summary(label: str, s: dict):
    rtt = s.get("rtt_ms")
    print(f"\n=== {label} ===")
    if not rtt:
        print("  no packets received!")
        print(f"  sent={s['sent']}")
        return
    print(f"  sent={s['sent']} recv={s['received']} loss={s['loss_pct']}%")
    print(f"  RTT ms: mean={rtt['mean']} p50={rtt['p50']} p90={rtt['p90']} "
          f"p99={rtt['p99']} max={rtt['max']}")
    print(f"  jitter={s['jitter_ms']}ms  stale(> {s['stale_threshold_ms']}ms)={s['stale_pct']}%")


def save_result(path: str, record: dict):
    record = dict(record)
    record["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")
    print(f"  -> appended to {path}")
