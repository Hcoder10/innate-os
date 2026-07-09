# Drive MARS yourself over each transport

Leader arm plugged into your Mac → chosen transport → robot's production
teleop input (UDP `:9999` → `udp_leader_receiver` → arm follows). A live HUD
shows RTT while you drive, so you can *feel* each transport and see its
numbers at the same time.

```
leader arm ──USB──> leader_link.py ──[transport]──> follower_bridge.py ──UDP :9999──> arm
   (Mac)                                (robot)
```

## 0. Safety checklist (once)

1. Clear space around the arm; be ready to Ctrl-C (robot then holds pose).
2. Arm torque must be ON for it to follow:
   `ros2 service call /mars/arm/torque_on std_srvs/srv/Trigger` (or via app).
   If a servo faults from a collision: `/mars/arm/fix_error`.
3. The bridge only ever forwards the **newest** packet (backlogs are
   collapsed, never replayed) and stops forwarding after a 300ms stall.
4. First time: verify your leader mapping WITHOUT the arm moving — run the
   bridge with `--forward-port 9995` (a dead port) and watch the HUD/logs.

## 1. Robot side — start the bridge (pick ONE transport)

```bash
ssh jetson1@mars-the-27th.local   # then: cd ~/teleop_bench
VM=35.233.134.107

# LAN UDP baseline
.venv/bin/python follower_bridge.py --transport udp --port 9996

# RFC path: WebRTC DataChannel (ICE direct; add --ice-mode relay for TURN)
.venv/bin/python follower_bridge.py --transport webrtc \
    --relay ws://$VM:8765 --session drive --turn turn:$VM:3478

# Cloud WS relay (innate-cloud proxy pattern)
.venv/bin/python follower_bridge.py --transport ws --relay ws://$VM:8765 --session drive

# Zenoh via GCP router
.venv/bin/python follower_bridge.py --transport zenoh --connect tcp/$VM:7447

# LiveKit SFU  (expect drops at high rate on bad networks — see benchmarks)
.venv/bin/python follower_bridge.py --transport livekit --lk-url ws://$VM:7880

# Adamo's hosted network
ADAMO_API_KEY=ak_... .venv/bin/python follower_bridge.py --transport adamo
```

## 2. Mac side — plug in the leader arm and drive

From `tools/teleop_bench/` (same `--transport` + matching options):

```bash
# LAN UDP
.venv/bin/python leader_link.py --source arm --transport udp --host mars-the-27th.local --port 9996

# WebRTC (direct → feels like LAN; then try --ice-mode relay to feel TURN)
.venv/bin/python leader_link.py --source arm --transport webrtc \
    --relay ws://$VM:8765 --session drive --turn turn:$VM:3478

# WS relay / zenoh / livekit / adamo — same flags as the bridge side
```

The leader device is auto-detected (`/dev/tty.usbmodem*`); override with
`--device`. Servo ids default to `1,2,3,4,5,6` (`--ids` to change), 1M baud,
100Hz (`--rate 200` if your bus keeps up).

No leader arm? `--source sine` wiggles the wrist/gripper around **neutral
pose** (arm moves to neutral first!) so you can still feel each transport.

## 3. What to compare

- **udp on LAN** vs **webrtc --ice-mode relay**: today's feel vs the
  remote worst case (~+70ms RTT via us-west1).
- **webrtc (ice=all)** from a phone-hotspot Mac: the real cross-network
  experience the RFC proposes — ICE will hole-punch or fall back to TURN.
- **adamo**: best relay placement measured (~40ms RTT) — the target to beat
  with an edge-placed coturn.
- Watch the HUD: p50 is "how it feels", p95/stale% is "how often it stutters"
  — WiFi spikes hit every transport equally.

For the operator's *eyes*, open the robot webapp video (or the controller
app) alongside — video rides the existing WebRTC pipeline independently of
the control path you're testing.
