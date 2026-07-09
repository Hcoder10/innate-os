# teleop_bench — cross-network teleoperation transport benchmark

Benchmarks candidate transports for remote (cross-network) MARS teleoperation
using the **real 38-byte / 200 Hz control packet** from
`udp_leader_receiver.cpp` / `UDPService.ts`, plus a **remote-inference
simulator** (camera frame up → action chunk back, with configurable
simulated GPU compute time).

Transports covered and what they represent:

| `--transport` | Represents | Path |
| --- | --- | --- |
| `udp` | today's LAN teleop (raw UDP :9999) | direct |
| `webrtc` (`--ice-mode all/srflx/relay`) | the REMOTE_TELEOP_DESIGN.md RFC (DataChannel, unordered/no-retransmit) | P2P, NAT-hairpin, or forced TURN |
| `ws` | innate-cloud-style WebSocket proxy (brain socket pattern) | relay |
| `livekit` | LiveKit / LiveKit Portal (SFU) | relay (SFU) |
| `zenoh` | Adamo-class transport (Adamo = Zenoh-over-QUIC) + the rmw_zenoh WAN option | peer or router relay |
| `quic` | Kyber-class transport (QUIC datagrams) | direct (QUIC has no NAT traversal) |
| `wsecho` | RTT floor to the relay VM | direct |

Metrics: RTT mean/p50/p90/p99/max, jitter, loss %, and **stale %** — the
fraction of packets older than `--stale-ms` (default 60 ms), which is what a
deadman/staleness gate would drop.

## Setup

```bash
cd tools/teleop_bench
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt     # on Mac and on the robot
```

## Cloud side (GCP VM) — one command

```bash
gcloud auth login          # once
./deploy/provision_gcp.sh  # creates VM + firewall, installs & starts:
                           # coturn, livekit-server, zenoh router,
                           # WS relay + inference sim, UDP echo
```

Prints the VM IP; every command below uses `$VM`.

## Robot side (reflectors — echo the control stream back)

```bash
ssh jetson1@mars-the-27th.local
cd ~/teleop_bench
# pick the one you're testing:
.venv/bin/python reflector.py --transport udp    --port 9998
.venv/bin/python reflector.py --transport quic   --port 9997
.venv/bin/python reflector.py --transport ws     --relay ws://$VM:8765
.venv/bin/python reflector.py --transport webrtc --relay ws://$VM:8765 \
    --turn turn:$VM:3478 [--ice-mode relay]
.venv/bin/python reflector.py --transport livekit --lk-url ws://$VM:7880
.venv/bin/python reflector.py --transport zenoh  --connect tcp/$VM:7447
```

## Operator side (Mac — drives 200 Hz and measures)

```bash
# LAN baseline (today's path)
.venv/bin/python teleop_operator.py --transport udp --host mars-the-27th.local --port 9998

# RFC path: WebRTC DataChannel — direct, then forced through TURN
.venv/bin/python teleop_operator.py --transport webrtc --relay ws://$VM:8765 \
    --turn turn:$VM:3478 --ice-mode all
.venv/bin/python teleop_operator.py --transport webrtc --relay ws://$VM:8765 \
    --turn turn:$VM:3478 --ice-mode relay

# Cloud WS relay (innate-cloud proxy pattern)
.venv/bin/python teleop_operator.py --transport ws --relay ws://$VM:8765

# LiveKit SFU
.venv/bin/python teleop_operator.py --transport livekit --lk-url ws://$VM:7880

# Zenoh through a WAN router (Adamo-class; also the rmw_zenoh WAN story)
.venv/bin/python teleop_operator.py --transport zenoh --connect tcp/$VM:7447

# add to any run: --duration 30 --json-out results.jsonl --label "my run"
```

## Remote-inference simulation (robot → cloud → robot)

```bash
# on the robot; server sleeps --compute-ms to simulate the GPU forward pass
.venv/bin/python inference_client.py --relay ws://$VM:8765 \
    --frame-kb 60 --compute-ms 100 --chunk 50 --exec-hz 50

# pipelined mode ~ video-transport proxy (30 fps frames, no compute)
.venv/bin/python inference_client.py --relay ws://$VM:8765 \
    --rate 30 --compute-ms 0
```

The output includes the **action-chunk check**: whether the obs→action cycle
(p99) fits inside one chunk duration, i.e. whether streaming cloud inference
sustains control without starvation.

## Degraded-network runs

`netem_scoped.sh` impairs **only traffic toward one peer IP** (other traffic
untouched). Note: **MARS's Tegra kernel (5.15.148-tegra) ships without
sch_netem/sch_prio, so it cannot run on the robot.** Run it on the GCP relay
VM instead — impairing the relay's egress on both legs is the right place to
emulate a bad WAN for every relayed transport:

```bash
# on the VM (standard Ubuntu kernel):
sudo ./netem_scoped.sh apply <robot-public-ip> 40 8 1   # 40ms +/-8ms, 1% loss
sudo ./netem_scoped.sh apply <operator-public-ip> 40 8 1 
sudo ./netem_scoped.sh clear
```

## Notes / caveats

- Python-side WebRTC uses aiortc (pure-Python SCTP): fine for RTT
  measurement; production would use GStreamer `webrtcbin` (already on the
  robot) or libdatachannel, which only lowers CPU cost per message.
- `--ice-mode relay` filters remote SDP candidates to `relay` type, forcing
  at least one TURN leg into the path (both peers on one LAN would otherwise
  shortcut via host candidates).
- LiveKit dev mode uses devkey/secret — benchmarking only.
- All robot WiFi results include the robot's `wlP1p1s0` link; on this link
  the WiFi tail (50–100 ms spikes) dominates every transport's p90/p99.
