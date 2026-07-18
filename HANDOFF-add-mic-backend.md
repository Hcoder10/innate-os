# Handoff — inbound phone-mic → ROS topic (backend, `feat/add-mic`)

Backend half of the operator-microphone feature: receive the browser/phone mic over the existing WebRTC
peer and republish it as a ROS topic. Companion to [HANDOFF-add-mic.md](HANDOFF-add-mic.md) (which is
frontend-first and covers the full feature) — this doc focuses on the robot-side receive path so it can be
understood and tested **without the webapp**.

Scope: **WebRTC → ROS topic only.** The robot decodes the inbound opus track and publishes PCM; playing it
out a physical speaker is future work (no consumer node yet).

Data path:

```
browser/phone mic → WebRTC (opus) → mars_cam webrtc_streamer_node
  → webrtcbin pad-added → rtpopusdepay → opusdec → audioconvert → audioresample
  → S16LE/48k/mono → appsink → publish innate_audio/msg/Audio on /audio/remote_mic
```

## Status

- **Committed** on `feat/add-mic` (`a1c078be feat(mars_cam): inbound phone-mic path`). Working tree is clean.
- Compiles clean (`innate_audio` + `mars_cam`); the `mars_cam` unit tests pass.
- **Not runtime-verified end to end** — no physical robot in the loop. The robot's running binary predates
  this change, so it **must be rebuilt** with `innate_audio` present.

## The node: `mars_cam::WebRTCStreamer` (GStreamer `webrtcbin`, robot is always the offerer)

This is the same node the webapp signals over `/webrtc/*` (rosbridge). It already streamed video +
**robot-mic → browser** (sendonly opus, PT 98). This change adds the reverse: **browser → robot** audio.

Key mechanics:

- **Opt-in per peer.** `/webrtc/start` gains a boolean `mic`. On `mic:true` the peer's transport gets a
  **recvonly opus transceiver** (`add-transceiver`, `GST_WEBRTC_RTP_TRANSCEIVER_DIRECTION_RECVONLY`), added
  as the **last** m-line (after all video + the send-audio m-line).
- **PT 110** for the recv m-line — deliberately distinct from the send-audio PT 98 and every camera PT
  (96,97,99,100,…), because all m-lines share one bundle (`max-bundle`) where the PT must be unique.
  Constants live in `webrtc_internal.hpp` (`kAudioSendPayloadType`, `kMicRecvPayloadType`).
- **Renegotiates.** Unlike the send-audio m-line (negotiated up front → reneg-free toggle), the mic m-line
  is only present when requested, so toggling `mic` changes m-line topology and forces a reconnect. The
  reneg-free fast path in `on_start` is gated on `with_mic == negotiate_mic` as well as audio.
- **Receive → publish.** When RTP arrives, `webrtcbin` fires `pad-added`; `on_incoming_pad` builds the
  decode branch into that peer's transport pipeline and links the pad. `on_mic_sample` maps each decoded
  buffer to `int16[]` and publishes `innate_audio/Audio` (`seq++`, `rate=48000`, `channels=1`,
  `frame_id="remote"`). Runs on a GStreamer streaming thread; does **not** take `peers_mutex_`, so it can't
  deadlock against `~Peer`.
- **SDP/ICE accounting** updated for the extra audio line: the offer/answer media-count guards
  (`expected_audio_lines`) and the ICE `max_mline` range now count both audio m-lines.

## The message: `innate_audio/msg/Audio` (new package)

```
std_msgs/Header header   # stamp = publish time; frame_id = "remote"
uint32  seq              # per-stream, wraps; gaps = drops
uint32  rate             # 48000
uint8   channels         # 1
int16[] samples          # S16LE mono PCM
```

## Parameters (on `webrtc_streamer_node`)

- `remote_mic_topic` (default `/audio/remote_mic`) — where decoded PCM is published.
- `enable_audio` (default `true`) — gates the existing robot-mic send path; the inbound mic path is
  independent of it and keyed purely on the per-peer `mic` START flag.

## Files

**New package** `ros2_ws/src/innate_audio/` — `msg/Audio.msg`, `CMakeLists.txt`, `package.xml`.

**`ros2_ws/src/mars_bot/mars_cam/`**
- `include/mars_cam/webrtc_streamer.hpp` — `Peer::with_mic`; `remote_mic_pub_`, `remote_mic_seq_`,
  `remote_mic_topic_`; `on_incoming_pad`/`on_mic_sample` decls; `create_peer_transport` gains `with_mic`.
- `include/mars_cam/webrtc_internal.hpp` — `kAudioSendPayloadType`/`kMicRecvPayloadType`;
  `OfferContext::expected_mic`.
- `mars_cam/webrtc_transport.cpp` — recvonly transceiver on `mic:true`; `pad-added` wiring;
  `on_incoming_pad` (decode branch) + `on_mic_sample` (publish); expected-media tags.
- `mars_cam/webrtc_signaling.cpp` — parse `mic`; reneg-free gate on `with_mic`; offer/answer media-count
  guards; ICE `max_mline`.
- `mars_cam/webrtc_streamer.cpp` — declare/read `remote_mic_topic`; create `remote_mic_pub_`
  (SensorData/best-effort QoS); startup log.
- `package.xml` / `CMakeLists.txt` — depend on `innate_audio`.

## Build & test

```bash
# In the robot's ros2_ws (innate_audio must be present):
colcon build --packages-select innate_audio mars_cam
source install/setup.bash
# webrtc_streamer_node comes up via the normal camera/webrtc bringup.
```

**Testing without the webapp** (this backend can be exercised standalone): drive the signaling directly —
publish a `/webrtc/start` (`std_msgs/String` JSON) with `"mic": true` and a `client_id`, answer the offer
from any WebRTC client that adds an opus **send** track (a small `gst`/`aiortc`/browser-devtools sender),
then:

```bash
ros2 topic hz  /audio/remote_mic
ros2 topic echo /audio/remote_mic --field seq
```

The node logs `Inbound phone-mic connected for '<client_id>' -> publishing /audio/remote_mic` when the pad
links. (For the full app-driven flow — mic-toggle button, permission prompt, reconnect behavior — see the
frontend handoff.)

## Known limitations / notes

- **No speaker playout** — `/audio/remote_mic` is published but nothing plays it on the robot.
- **`seq` is node-wide.** A single active sender is the tested case; concurrent senders would interleave on
  the one topic.
- **Toggling re-handshakes** (adds/removes the m-line) → brief video freeze-frame, inherent to changing
  m-line topology.
- **Sim.** The sim path uses rosbridge, not `WebRtcSession`; exercising this needs
  `webrtc_streamer.sim.launch.py` running and a WebRTC (not sim) client.
- `phntm_bridge_client/` and `phntm_interfaces/` in the tree are a **separate** WebRTC stack, unrelated to
  this feature.
