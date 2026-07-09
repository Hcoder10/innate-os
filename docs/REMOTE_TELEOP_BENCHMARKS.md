# Remote teleoperation: solution survey + transport benchmarks

**Date:** 2026-07-09
**Companion to:** [REMOTE_TELEOP_DESIGN.md](REMOTE_TELEOP_DESIGN.md) (the WebRTC RFC)
**Harness:** [`tools/teleop_bench/`](../tools/teleop_bench/README.md) — all numbers below are reproducible with one command per row.

Goal: pick the transport stack for teleoperating MARS from a different
network with (a) lowest achievable control latency on imperfect networks,
(b) training-grade data collection, and (c) a path that also serves remote
inference (cloud GPU → action chunks → robot).

---

## 1. Solution survey (verified, July 2026)

### The three candidates you named

| | **Adamo** (adamohq.com) | **LiveKit Portal** | **Kyber** (gitlab.com/kyber) |
| --- | --- | --- | --- |
| What it is | Teleop SDK + managed teleoperator workforce | OSS teleop/inference transport layer on LiveKit rooms | OSS very-low-latency machine control (VLC founder, $5M Lightspeed) |
| Transport | **Zenoh over QUIC** (their hosted routers) + MoQ fork for browser video — "built from scratch" is marketing | WebRTC via SFU; lossy/reliable data channels, new "data tracks" | **QUIC only** (WebTransport in browser); RaptorQ FEC; explicitly rejected WebRTC |
| NAT traversal | Relay via their routers (outbound QUIC/443); P2P only Chrome+same-LAN/Tailscale; **no CGNAT hole-punch** | Full ICE/TURN (embedded TURN in server); SFU relay always works | **None** — needs VPN or port-forward (deliberate omission) |
| Latency claims | "sub-40ms" marketing; their own lossy-net test: 83ms at 50ms RTT (vs LiveKit 100ms) | ~90–160ms glass-to-glass measured by third parties; sub-100ms with tuning | **8–16ms glass-to-glass measured** (LAN, 60–240Hz displays) — best-in-class numbers |
| Self-host | **No** (core is closed; metered even P2P) | **Yes** (Apache-2.0, single Go binary + Rust/Python portal) | Yes (AGPLv3 or commercial) |
| Jetson/ARM64 | Yes (aarch64 wheels, NVENC auto) | Yes (livekit aarch64 wheels; portal wheels are py3.12-only, buildable) | **No** — x86 desktop-capture shaped today; a porting project |
| Data collection for IL | Recording + PyTorch dataset API; **clock-sync method undocumented** | **Best-in-class design**: sender-monotonic stamps + SyncBuffer joins video+state into policy-ready Observations; recorder participant captures actions labeled by sender; LeRobot plugins | Not a data-collection product |
| Remote inference | Nothing documented (DIY on their pub/sub) | **First-class**: policy joins room as operator; action chunks with `in_reply_to_ts_us`; shadow-eval mode | Not applicable |
| Try today | Free tier: 1 robot, 10h, self-serve signup | Free (self-host) / LiveKit Cloud free tier | Build from source on x86/Mac |
| Risk | Closed core, startup, lock-in, metered minutes | Portal is ~3 months old (17 stars) | Pre-1.0, AGPL, no ARM server, no NAT story |

### Broader landscape (what you didn't list)

- **phntm bridge** (MIT, C++/libdatachannel): the closest existing
  implementation of our RFC — ROS2 topics over WebRTC data channels + H.264,
  cloud signaling + TURN, Jetson-supported, self-hostable. Weakness: operator
  is their web UI; no clean custom-client API.
- **Zenoh WAN routing** (what the robot already speaks via rmw_zenoh):
  config-only multi-site — a `zenohd` on a VPS, robot connects outbound.
  No NAT traversal (always relay), but zenoh ≥1.9 adds **unreliable QUIC
  datagram links** (escape from TCP head-of-line blocking). This is exactly
  Adamo's architecture, self-hosted.
- **Overlay VPNs**: Tailscale (best diagnostics; DERP fallback is TCP —
  bad for 200Hz; new UDP peer relays fix this), Husarnet (robotics-specific,
  UDP relay fallback, no published numbers), plain WireGuard+VPS (the
  deterministic control group, kernel-space on Orin).
- **MoQ / WebTransport**: maturing fast (Safari 26.4 ships WebTransport;
  Cloudflare runs free MoQ relays in 330+ cities) but **no P2P story at all**
  and no React Native API — not ready to be *the* transport for our app.
- **Robot-learning industry practice**: nobody publishes teleop transports;
  all open rigs (ALOHA, GELLO, Open-Teach, LeKiwi) are LAN-only ZMQ/USB. Our
  200Hz/38B stream is already faster than all of them. Data-sync universal
  pattern: **record on-robot, align by timestamp** — don't sync at transport
  level.
- **Remote inference precedents**: Physical Intelligence serves pi0 over a
  **plain WebSocket + msgpack** (measured: 97ms model + 7–21ms network);
  LeRobot async inference uses gRPC with chunk merging; PI's Real-Time
  Chunking shows action-chunked policies tolerate **+200–300ms** injected
  latency with no degradation. Remote inference does not need an exotic
  transport; it needs correct chunk-switching.

Key published latency context:
- In-region TURN adds ~0–10ms over direct P2P; badly-placed TURN doubles RTT
  (Whereby global measurements).
- Camera pipeline (capture+encode+decode) is ~100–120ms of any glass-to-glass
  video number (Transitive breakdown) — **the network is usually not the
  bottleneck for video; it is for control.**

---

## 2. Measured results (LAN + hairpin, 2026-07-09)

Setup: MacBook (operator, WiFi) → MARS `mars-the-27th.local` (Jetson Orin,
WiFi `wlP1p1s0`, production ROS stack running). 200Hz × 38-byte control
packets, 15s runs, echo RTT. "stale" = RTT > 60ms (what a deadman would drop).

| Transport (path) | p50 | p90 | p99 | loss | stale>60ms |
| --- | --- | --- | --- | --- | --- |
| **UDP direct** (today's teleop) | **4.8ms** | 50.4ms | 95.9ms | 0.03% | 8.1% |
| Zenoh peer-to-peer | 5.5ms | 53.7ms | 100.7ms | 0% | 8.8% |
| QUIC datagrams (Kyber-class) | 6.3ms | 72.8ms | 99.6ms | 0% | 13.9% |
| WebRTC DataChannel P2P (RFC path) | 6.8ms | 54.6ms | 97.5ms | 0% | 9.0% |
| WS relay on Mac (cloud-proxy pattern) | 7.3ms | 56.9ms | 100.1ms | 0% | 9.4% |
| LiveKit SFU on Mac | 11.2ms | 81.7ms | 103.4ms | 0% | 17.7% |

**The headline so far: the robot's WiFi dominates everything.** Every
transport shows the same ~50–100ms p90/p99 tail because `wlP1p1s0` (plus a
busy 2.4/5GHz channel and a loaded CPU) injects 50–100ms spikes into ~8–18%
of packets. Transport choice moves p50 by a few ms; **the WiFi link decides
whether remote teleop feels good.** Practical consequences:

1. A latency-aware deadman (RFC §9) is mandatory regardless of transport —
   8%+ of packets are already stale on the *local* path today.
2. Any remote target ("<X ms p99") must be stated as *delta over the WiFi
   floor*, or measured wired/5GHz-clean.
3. LiveKit's extra ~6ms p50 + fatter tail is the SFU hop + per-message
   overhead; WebRTC DC and Zenoh sit within ~2ms of raw UDP.

Two additional hands-on findings:

- **The home router (Comcast) does not hairpin NAT**: a WebRTC connection
  restricted to srflx (public-IP) candidates never connects between two
  devices on this LAN. Consequence for the RFC: on real consumer networks,
  STUN-only P2P will fail for a meaningful fraction of NAT pairs — **TURN is
  load-bearing, not a corner-case fallback.**
- **MARS's Tegra kernel has no netem/prio qdiscs** (`sch_netem` absent from
  `5.15.148-tegra`), so degraded-network emulation must run on the relay VM
  (standard Ubuntu kernel), which is also methodologically cleaner.

Remote-inference simulator (robot → server, 60KB frame up, 100ms simulated
compute, action chunk back; LAN server): p50 cycle 119ms ≈ compute + ~19ms
network share. Action-chunk check: 50 steps @ 50Hz = 1000ms budget → **cloud
inference sustains control with 8× headroom even before WAN RTT is added.**

### WAN results — pending

GCP auth expired mid-session (`gcloud auth login` needed). Once re-authed:
`tools/teleop_bench/deploy/provision_gcp.sh` stands up the full cloud side
(coturn, LiveKit, zenoh router, WS relay + inference sim) in one command, and
the WAN matrix (direct vs forced-TURN vs SFU vs zenoh-router vs WS-relay,
plus robot→cloud inference cycles) reruns with `--relay ws://$VM:8765 ...`.
Robot → Google edge TCP RTT measured at ~19ms, so expect ~20–30ms floors for
in-region relayed paths.

---

## 3. Read on the RFC (what changes, what stands)

**Stands:** one ICE-negotiated peer connection carrying video + unordered
no-retransmit DataChannel, cloud signaling, coturn, deadman watchdog. This is
also what phntm/Formant/Transitive/1X converge on, and our LAN numbers show
WebRTC DC within ~2ms of raw UDP.

**Adjustments suggested by research + measurements:**
1. **Plan for TURN-as-default, not fallback** (CGNAT both sides is the common
   consumer case). Place coturn in-region; budget +25–40ms.
2. **Don't use aiortc-class userspace SCTP in production** — put the control
   DataChannel in the existing GStreamer `webrtcbin` (it supports
   `ordered=false, max-retransmits=0`), as the RFC already planned.
3. **The robot→cloud leg doesn't need WebRTC.** For remote inference and
   data upload, a zenoh client (already in the stack via rmw_zenoh) or plain
   WS to Cloud Run is sufficient — chunked policies tolerate 300ms.
4. **Data collection: record on-robot, align by timestamp** (industry
   consensus); don't try to sync at transport level. If transport-level
   obs↔action sync becomes a requirement (e.g. HITL shadow eval), LiveKit
   Portal is the only shelf option and is worth a spike.
5. **WiFi first.** Before optimizing WAN transport, characterize/tune the
   robot's WiFi (5GHz-only, power-save off, QoS/WMM for the control port) —
   it is the dominant latency term today.

## 4. Runnable examples

See [`tools/teleop_bench/README.md`](../tools/teleop_bench/README.md) — every
row above is one `teleop_operator.py` command; reflectors run on the robot in
`~/teleop_bench` (already deployed on mars-the-27th).
