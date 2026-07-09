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

### WAN results (GCP VM `teleop-bench-1`, us-west1, 35.233.134.107)

RTT floors from the home network (Comcast): Mac→VM **36ms p50**, robot→VM
**34ms p50** — so any both-legs-relayed path has a ~70ms floor from this
network to us-west1. Measured (200Hz, 20s runs; "stale" still the 60ms LAN
threshold — a WAN deadman would use 150–300ms):

| Path (real internet) | p50 | p90 | p99 | loss |
| --- | --- | --- | --- | --- |
| **Adamo cloud router (their QUIC/Zenoh net)** | **40.2ms** | 55.7ms | 136.4ms | 0.1% |
| WebRTC DC forced-TURN via GCP (RFC worst case) | 72.0ms | 80.6ms | 269.3ms | 0% |
| WS relay via GCP (innate-cloud proxy pattern) | 73.0ms | 79.3ms | **85.6ms** | 0% |
| LiveKit SFU on GCP | 76.8ms | 127.7ms | 379.2ms | 0.3% |
| Zenoh via GCP router (tcp, client mode) | 100.2ms | 150.5ms | 168.6ms | 1.1% |
| WebRTC DC ice=all (ICE finds the LAN path) | 7.6ms | 76.3ms | 100.2ms | 0% |

With the VM impairing both legs (**netem +40ms ±8ms, 1% loss each way** —
a plausible bad-cellular day):

| Path | p50 | p90 | p99 | loss |
| --- | --- | --- | --- | --- |
| WS relay (TCP) | 165ms | 241ms | 343ms | 0% (retransmits) |
| WebRTC DC forced-TURN (unreliable) | 187ms | 283ms | 452ms | 2.3% |
| Zenoh router (tcp) | 211ms | 271ms | 332ms | 0.6% |
| **LiveKit SFU lossy data** | 155ms | 221ms | 250ms | **30–46%** (!) |

Key takeaways:

1. **Relay placement is the whole game.** Adamo wins the clean-network test
   (p50 40ms) purely because their router is ~17ms from this Comcast link
   (SDK-reported relay RTT 16–19ms from both peers), vs ~35ms to GCP
   us-west1. Their QUIC/Zenoh transport itself adds only ~6ms over 2× relay
   RTT — and our WS relay and coturn *also* land within ~3ms of their
   respective floor (2×36 ≈ 72ms). Same physics, different real estate.
   GCP has no SF-metro region; matching Adamo's number would need an edge
   POP (Cloudflare/Fly/hetzner-SV or the future gcloud edge) — or P2P.
2. **ICE is worth it**: `ice=all` found the direct path automatically and ran
   at LAN latency (7.6ms) while the same code, network and signaling forced
   through TURN costs 72ms. P2P-when-possible + TURN-fallback (the RFC
   design) is strictly better than any always-relay architecture.
3. **LiveKit's lossy data channel sheds catastrophically under loss at
   200Hz**: 30–46% delivery loss on a 1%-loss link (reproduced twice; SFU
   self-hosted, Python SDK `publish_data(reliable=False)`). Until
   investigated (data tracks may behave differently), LiveKit is not
   suitable for the 200Hz control stream on imperfect networks — its
   sweet spot here is video + data *collection*, not the control loop.
4. **TCP relays degrade more gracefully than expected** for control-size
   packets (0% app loss, p99 343ms via retransmits) but head-of-line
   latency makes unreliable-datagram paths (WebRTC DC) preferable for
   control: stale-and-dropped beats late-and-ordered for servo targets.
5. **Zenoh client-through-router underperformed** its pedigree here
   (p50 100ms vs 73ms floor, 1% drops from DROP congestion control at
   200Hz express). Needs tuning (QUIC links, batching off) before judging —
   Adamo proves the protocol family can do better.

### Remote inference over real WAN (robot → GCP us-west1)

60KB frame up + action chunk back, closed-loop:

- 100ms simulated compute: **cycle p50 142ms / p99 153ms** (network share
  ~42ms). A 50-step chunk at 50Hz (1s) leaves **6.5× headroom** — cloud
  policy serving from us-west1 is comfortably feasible, matching the
  Physical Intelligence numbers (their production: ~108–139ms/chunk).
- Pipelined 30fps × 60KB (≈14Mbps uplink): p50 43ms but p90 220ms —
  **the Comcast uplink queues (bufferbloat)**. Observation streams must be
  paced/compressed below the uplink's undulating capacity; adaptive-bitrate
  video (WebRTC) or resolution-capped JPEG (openpi resizes to 224²) both
  solve this.

---

### Adamo hands-on notes

Tested with a free-tier API key via `tools/teleop_bench/adamo_bench.py`
(`pip install adamo` worked out-of-the-box on the Jetson). The SDK is openly
Zenoh: `connect(protocol="quic", mode="client"|"peer", relay=...)`, plus
useful built-ins (`relay_rtt()`, `measure_rtt()`, `watch_latency()`).
Verdict: excellent router placement and a polished SDK, but the transport
result is reproducible with self-hosted infrastructure placed equally well;
the closed core + metered relay is what you'd be paying for (plus their
operator workforce product).

## 3. Read on the RFC (what changes, what stands)

**Stands:** one ICE-negotiated peer connection carrying video + unordered
no-retransmit DataChannel, cloud signaling, coturn, deadman watchdog. This is
also what phntm/Formant/Transitive/1X converge on, and our LAN numbers show
WebRTC DC within ~2ms of raw UDP.

**Adjustments suggested by research + measurements:**
1. **Plan for TURN-as-default, not fallback** (CGNAT both sides is the common
   consumer case; this home router doesn't even hairpin). Measured cost of a
   us-west1 relay from a Bay-Area Comcast link: ~72ms RTT (vs 40ms via
   Adamo's SF-metro router). **Relay placement is the single biggest
   latency lever** — worth an edge POP (Fly/Cloudflare/metal) over a GCP
   region for the TURN tier if sub-50ms relayed RTT matters.
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
