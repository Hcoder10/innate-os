# Remote teleoperation design (RFC)

**Status:** Draft for review
**Date:** 2026-06-26
**Scope:** The transport / media / control path for teleoperating a robot from a
different network (over the internet) with low latency. Operator authentication
and robot pairing are explicitly **deferred to a separate workstream** (see
§11) — this document assumes a session can be authorized and focuses on getting
bytes between phone and robot.

---

## 1. Problem

Today teleoperation only works when the operator's phone and the robot are on
the **same WiFi network**. The phone addresses the robot by its LAN IP across
three independent paths:

- **Control:** 200 Hz servo stream over raw UDP to `robotIP:9999`.
- **Commands / signaling:** rosbridge WebSocket at `ws://robotIP:9090`.
- **Video / audio:** WebRTC, negotiated over the rosbridge connection.

The moment phone and robot are on different networks, both sit behind NAT and
neither can address the other. We want remote teleop that keeps latency low
enough to drive the arm and base by hand.

## 2. Current state — we are most of the way there

The important finding: **a remote-capable WebRTC media stack already exists.**

| Piece | Where | State |
| --- | --- | --- |
| Robot WebRTC server | `innate-os/ros2_ws/src/mars_bot/mars_cam/mars_cam/webrtc_streamer.cpp` | GStreamer `webrtcbin`; dual video (main + arm) + audio; offer/answer/ICE; playout-delay tuning for low latency. |
| Browser cockpit | `innate-os/webapp/js/webrtcSession.js` | Working WebRTC client: handshake, ICE trickle, reconnect watchdog. |
| Mobile client | `innate-controller-app/src/hooks/useWebRTC.ts` | React Native twin of the above. |
| Control send | `innate-controller-app/src/services/UDPService.ts` | 38-byte binary servo packets, UDP `:9999`. |
| Control receive | `innate-os/ros2_ws/src/mars_bot/mars_control/mars_control/udp_leader_receiver.cpp` | Parses packets → publishes `/mars/arm/commands`. |
| Cloud backend | `innate-cloud/` | FastAPI monorepo (`apps/*`) on Cloud Run; JWT auth (`innate-auth`, EdDSA); robots connect **outbound only**. |

What is missing for this to work for a remote consumer:

1. **No TURN relay.** Both clients hardcode only Google STUN
   (`useWebRTC.ts:49`, `webrtcSession.js`). STUN alone fails on symmetric NAT /
   CGNAT — exactly the common case on cellular and many home routers.
2. **Signaling rides the robot's rosbridge.** Today the only way to reach it
   remotely is to expose the robot's rosbridge to the internet (HTTPS proxy /
   tunnel). That puts the robot's entire ROS graph on the open internet — fine
   for a demo, unacceptable as a product.
3. **Control has no NAT traversal at all** (raw UDP `:9999`).
4. **No deadman on the control receiver** (see §9) — tolerable on LAN, unsafe
   over a lossy internet link.

So this RFC is mostly *productizing* an existing media path, not building one
from scratch.

## 3. Goals / non-goals

**Goals**
- Drive arm + base from a phone on any network, video + control.
- Low latency: direct peer-to-peer whenever the NAT pair allows; relay only as
  fallback.
- No inbound ports on the robot, and no exposing rosbridge to the internet.
- Reuse the existing WebRTC media stack and the existing control packet format.

**Non-goals (this RFC)**
- Operator accounts, robot ownership/pairing, session authorization — deferred
  (§11). The design leaves a clean seam for it (the signaling relay is the
  enforcement point).
- Multi-operator / spectator support beyond the single-consumer model that
  already exists.
- Replacing the brain (cloud-agent) path.

## 4. Target architecture

One ICE-negotiated peer connection carries everything:

```
                        ┌─────────────────────────────┐
                        │   innate-cloud (Cloud Run)   │
   Operator phone       │                              │        Robot
 ┌───────────────┐  WS  │  apps/signaling (JWT-gated)  │  WS  ┌───────────────┐
 │ useWebRTC.ts  │◄────►│   relays SDP offer/answer    │◄────►│ signaling     │
 │ + DataChannel │      │   + ICE candidates           │      │ bridge node   │
 └──────┬────────┘      │                              │      └──────┬────────┘
        │               │  coturn (TURN/STUN)          │             │ /webrtc/*
        │               └─────────────┬────────────────┘             │ topics
        │                             │ relay fallback               │
        │                             ▼                       webrtc_streamer.cpp
        └───────────  direct P2P (video + audio + control)  ──────────┘
                      ICE picks: host (LAN) ▸ srflx (hole-punch) ▸ relay (TURN)
```

**Why ICE makes this simpler than it looks:** ICE already does
"use the direct path when possible, relay only when forced." On the same LAN it
selects the host candidate (≈ LAN latency); across networks it hole-punches
(srflx); only when that fails does it fall back to TURN. We therefore build
**one** path that automatically degrades, not separate "local" and "remote"
modes. This is the same machinery the video already uses — we extend it to
control.

## 5. Components

### 5.1 Cloud signaling relay — `innate-cloud/apps/signaling`

A new FastAPI app under `apps/`, modeled on the existing authenticated
WebSocket proxy in `apps/service-proxy/proxy/main.py` (which already validates a
JWT *before* the WS upgrade). Responsibilities:

- Accept an authenticated WS from the **phone** (operator) and from the
  **robot** (see §5.2).
- Pair them into a **session** and relay opaque signaling messages
  (`offer` / `answer` / `ice`) between the two. It does not parse SDP.
- Mint **short-lived TURN credentials** for the session (§5.3) and hand them to
  both peers in the session-start message.
- Be the single **authorization choke point** — the deferred auth workstream
  plugs in here without touching robot or client media code.

Deploys like every other app: Cloud Run + Terraform + GitHub Actions, JWT via
`innate-auth-verifier`.

### 5.2 Robot → cloud signaling socket

innate-cloud has **no inbound path to a robot** — robots only connect outbound
(the brain socket to `wss://agent-v1.innate.bot`). So the robot must hold a
second persistent **outbound** WS to `apps/signaling`, mirroring how
`brain_client` already connects.

Add a small ROS node — `teleop_signaling_bridge` — that:

- Maintains the outbound WS (auth with the robot's `innate_service_key` JWT,
  reusing the `innate-auth` flow `brain_client` already uses).
- Bridges signaling to the **existing** `/webrtc/*` topics: messages from the
  cloud are republished onto `/webrtc/start`, `/webrtc/answer`, `/webrtc/ice_in`;
  `/webrtc/offer` and `/webrtc/ice_out` are forwarded back up the socket.

Consequence: **`webrtc_streamer.cpp`'s signaling is unchanged** — it keeps
speaking `/webrtc/*` topics; we only change how those messages reach the phone.

> Alternative considered: multiplex signaling over the **existing** brain socket
> instead of a second connection. Fewer sockets, but couples teleop availability
> to the brain service and complicates that protocol. Preference: dedicated
> socket for clean separation; revisit if connection count becomes a problem.

### 5.3 TURN — coturn

Stand up coturn (container + Terraform, alongside innate-cloud infra). Add it to
the ICE server list on **both** clients and the robot's `webrtcbin`, replacing
the lone Google STUN entry. Use **short-lived, per-session credentials** minted
by the signaling relay (TURN REST API / time-limited HMAC), never static
secrets shipped in the app. This single addition makes both video **and** the
control DataChannel relay-capable on restrictive NATs.

### 5.4 Control over a DataChannel (replaces UDP :9999)

Move the 200 Hz servo stream onto an `RTCDataChannel` on the same peer
connection as the video.

- **Channel config:** `{ ordered: false, maxRetransmits: 0 }` — UDP-like
  semantics. Newest servo target wins; never retransmit a stale one. (For
  teleop, an old position arriving late is worse than a dropped one.)
- **Payload:** the **exact same** 38-byte little-endian packet that
  `UDPService.ts` already builds (magic `0xAA55`, seq, timestamp, 6×int32). No
  format change — see Appendix B.
- **Robot side:** `webrtc_streamer.cpp` (which owns the peer connection) gains a
  DataChannel handler that runs the **same** conversion
  `radians = (pos − 2048) · 2π/4096` and publishes `/mars/arm/commands` — i.e.
  lift the body of `udp_leader_receiver.cpp::process_packet` (lines 304–312) into
  the handler. The existing sequence-drop logic (`is_out_of_order`, lines
  358–366) already tolerates reordering, so an unordered channel needs **no new
  dedup code**. The reset packet (`0xAA56`) maps to a DataChannel reset message
  and reuses `process_reset_packet`.

Raw UDP `:9999` stays as the LAN path until the DataChannel is proven on LAN
too (§10), then it can be retired — ICE will select the host candidate locally,
giving near-identical latency without a second transport to maintain.

### 5.5 Client changes

- `useWebRTC.ts` / `webrtcSession.js`: (a) ICE config gets the TURN entry +
  per-session creds; (b) signaling target switches from direct rosbridge to the
  cloud relay WS; (c) create/manage the control DataChannel.
- `RobotCoreContext` currently wires `udpService.connect(robotIP, 9999)`. In
  remote mode it instead routes servo positions to the DataChannel. A thin
  transport interface (`sendServoPositions`) lets the call sites stay the same.

## 6. Signaling protocol & session lifecycle

Messages are small JSON envelopes over the relay WS. The relay never inspects
SDP/ICE bodies.

```
1. Robot boots → teleop_signaling_bridge connects to apps/signaling,
   authenticates, registers as available (idle).
2. Operator opens teleop → phone connects to apps/signaling, authenticates,
   requests session for robot R.
3. Relay authorizes (deferred workstream), creates session S, mints TURN creds,
   sends `session_start{ iceServers }` to both peers.
4. Relay signals the robot (via the bridge → `/webrtc/start`).
5. Robot publishes `/webrtc/offer` → bridge → relay → phone.
6. Phone `setRemoteDescription`, creates answer + DataChannel → relay → robot.
7. ICE candidates trickle both ways through the relay until a pair connects.
8. Media (video/audio) + control (DataChannel) flow peer-to-peer.
9. Teardown / timeout → relay drops session, robot returns to idle.
```

Envelope (illustrative):
`{ "type": "offer|answer|ice|session_start|bye", "session": "S", "data": {…} }`

## 7. Control transport spec

| Property | Value |
| --- | --- |
| Channel | `RTCDataChannel`, label `teleop-control` |
| Reliability | `ordered: false`, `maxRetransmits: 0` |
| Rate | 200 Hz (unchanged) |
| Packet | 38 bytes, unchanged (Appendix B) |
| Reorder handling | existing seq-number drop on robot (no change) |
| Reconnect | reset packet `0xAA56` resets seq tracking (existing) |

Bandwidth is a non-issue: 38 B × 200 Hz ≈ 7.6 KB/s. The enemy is **jitter and
loss**, which is exactly why the channel is unreliable/unordered.

## 8. Latency budget

The floor is internet RTT between the two networks — unavoidable. Rough numbers:

- Same metro, both on good links, **direct P2P**: one-way ≈ 10–30 ms.
- Cross-country direct P2P: one-way ≈ 30–60 ms.
- **TURN relay** inserts the relay's location into the path; a well-placed relay
  adds little, a poorly-placed one can ~2× the RTT. Hence: P2P-first, relay only
  on fallback, and place TURN regionally.

DataChannel adds DTLS/SCTP framing over UDP — negligible for 38-byte messages.
On LAN, ICE picks the host candidate, so DataChannel latency ≈ current raw-UDP
latency.

## 9. Safety — latency-aware deadman (new)

**Current gap:** `udp_leader_receiver.cpp` has no timeout. On packet loss it
simply stops publishing `/mars/arm/commands`, so the arm holds its last
commanded pose. On LAN that is an acceptable failsafe; over a lossy/droppable
internet link it is not sufficient, and there is no equivalent guard for base
motion.

**Required for remote:**
- **Stall watchdog** on the robot: if no valid control message arrives within
  `T_stall` (start ~200–300 ms), latch to a safe state — hold the arm and
  publish zero `/cmd_vel` — until a fresh stream resumes (preceded by a reset).
- Use **arrival-time** staleness (`steady_clock` since last valid packet), not
  the packet's wall-clock timestamp — phone and robot clocks are not synced.
- On reconnect, require a reset (`0xAA56`) before resuming, so a burst of queued
  stale positions can't snap the arm. (Unreliable channel already minimizes
  this; the reset makes it explicit.)
- Surface link quality (RTT / loss) to the operator UI so they can stop driving
  when the link degrades.

This watchdog should live where control is consumed, so it protects both the new
DataChannel path and the legacy UDP path during the transition.

## 10. Single-consumer / session model

The robot pipeline serves **one consumer at a time** — `/webrtc/offer` is
broadcast, and `useWebRTC.ts` already has preemption handling (`preempted`,
`useWebRTC.ts:34–37`) for when another device grabs the camera. The cloud relay
should make this explicit: one active teleop session per robot, with takeover
semantics decided by the (deferred) auth/ownership layer. No new media work
needed for v1.

## 11. Deferred: auth & pairing workstream

innate-cloud's identity model today is **robot-only**: one `innate_service_key`
= one robot = one row in `users`. There is no human-operator identity and no
"this person may control that robot" relationship. Consumer remote teleop needs:

- An **operator identity** (phone login).
- A **pairing / ownership** link (operator ↔ robot).
- **Per-session authorization** + short-lived TURN creds.

Per decision, this is tracked separately. The architecture keeps the seam clean:
**all authorization happens in `apps/signaling`** at session-create time —
no robot-side or media-side code depends on the outcome. Until that lands,
sessions can be gated by the robot's existing `innate-auth` JWT plus a trivial
pairing claim for internal testing.

## 12. Phased rollout

| Phase | Repos | Deliverable | Proves |
| --- | --- | --- | --- |
| 1 | innate-cloud | `apps/signaling` relay + coturn (Terraform) + robot `teleop_signaling_bridge` node. Validate with the **existing webapp** client over a cellular hotspot. | NAT traversal + relay path end-to-end, no app/control-loop changes. |
| 2 | controller-app, webapp | Point client signaling at the relay; add TURN to ICE config. | Video/audio works off-LAN on the real clients. |
| 3 | controller-app, innate-os | Control DataChannel: app send + `webrtc_streamer.cpp` receive→publish, reusing the packet format + conversion. | Remote driving of arm/base. |
| 4 | innate-os, innate-cloud | Deadman watchdog (§9); auth/pairing (§11). | Safe + authorized. |
| 5 | both | Retire UDP `:9999` once DataChannel is proven on LAN. | One transport. |

**Start at Phase 1** — it is the maximally de-risking slice: stand up signaling
+ TURN and prove a restrictive-NAT connection using the existing browser client,
touching neither the mobile app nor the robot control loop. If video survives
cellular, the rest of the architecture is validated.

## 13. Risks

- **TURN cost/scale:** relayed sessions consume server bandwidth (video is the
  cost driver). Mitigate by maximizing P2P success (good STUN, regional TURN,
  proper ICE) so relay is the exception.
- **Cloud Run + long-lived WS:** the robot's persistent signaling socket must
  tolerate Cloud Run timeouts/scaling — keepalive + reconnect, same discipline
  as `brain_client`.
- **Safety over lossy links:** the deadman (§9) is load-bearing; it must land
  before remote control of physical hardware is enabled for anyone.
- **Single-pipeline contention:** concurrent teleop + autonomous/brain camera
  use already contends for the one pipeline; the relay's one-session rule must
  be enforced, not advisory.

---

## Appendix A — file-level change map

| Area | File | Change |
| --- | --- | --- |
| Signaling relay | `innate-cloud/apps/signaling/**` | **New** FastAPI app (model on `apps/service-proxy`). |
| TURN | `innate-cloud/terraform/**`, compose | **New** coturn service + creds endpoint. |
| Robot bridge | `innate-os/ros2_ws/src/.../teleop_signaling_bridge` | **New** node: outbound WS ↔ `/webrtc/*` topics. |
| Robot ICE | `innate-os/.../mars_cam/launch/webrtc_streamer.launch.py` | Add TURN ICE servers (param). |
| Robot control | `innate-os/.../mars_cam/mars_cam/webrtc_streamer.cpp` | Add `teleop-control` DataChannel → `/mars/arm/commands` (reuse conversion). |
| Deadman | `innate-os/.../mars_control/**` | Stall watchdog: hold arm + zero `/cmd_vel`. |
| App ICE/signaling | `innate-controller-app/src/hooks/useWebRTC.ts` | TURN creds; relay signaling; DataChannel. |
| App control | `innate-controller-app/src/services/UDPService.ts`, `contexts/RobotCoreContext.tsx` | Route servo packets to DataChannel in remote mode. |
| Web client | `innate-os/webapp/js/webrtcSession.js` | TURN + relay signaling (Phase 1 validation client). |

## Appendix B — control packet format (reference, unchanged)

Source of truth: `udp_leader_receiver.cpp:46–66`, `UDPService.ts:13–75`.

```
Data packet (38 bytes, little-endian):
  [0:2]   uint16  magic   = 0xAA55
  [2:6]   uint32  sequence
  [6:14]  double  timestamp (ms since epoch; log/diagnostic only)
  [14:38] int32×6 servo positions (raw units; radians = (pos−2048)·2π/4096)

Reset packet (6 bytes, little-endian):
  [0:2]   uint16  magic   = 0xAA56
  [2:6]   uint32  sequence
```
