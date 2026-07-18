# Handoff — operator microphone over WebRTC (`feat/add-mic`)

Two-way audio: send the **operator's browser microphone up to the robot** over the existing WebRTC
peer. This is the opposite direction from the pre-existing robot-mic feature (`setAudio`), which
*receives* the robot's mic and plays it in the browser.

Data path: browser mic → WebRTC (opus) → robot `webrtc_streamer_node` → opus decode → PCM published on
`/audio/remote_mic` (`innate_audio/msg/Audio`, S16LE mono 48k). Playing that PCM out a physical speaker
is **not** part of this change — the robot just republishes it as a ROS topic; a downstream consumer/
speaker node is future work.

## Status

- **Webapp**: complete, typechecks clean (`cd webapp && npx tsc --noEmit`).
- **Backend (`mars_cam` C++)**: complete in source. **Must be rebuilt** — the dev container was running a
  binary built *before* these changes (no mic symbols). Needs `innate_audio` present at build time.
- **Not runtime-verified end to end.** No physical robot was available; verified by reading both sides of
  the contract + typecheck. This branch is for **real-robot testing**.

## The wire contract

`/webrtc/start` (browser → robot, `std_msgs/String` JSON) gains one field:

```jsonc
{ "source": "live", "video": [...], "audio": <bool>, "mic": <bool>, "client_id": "...", "renegotiate": <bool> }
```

- `mic: true` → robot adds a **recvonly** opus audio m-line (browser → robot) to its offer, as the **last**
  m-line (after all video + the send-audio m-line). PT 110.
- The mic m-line is **not** negotiated up front (unlike the robot-mic send m-line), so toggling `mic`
  changes m-line topology and forces a **renegotiating reconnect** (brief video freeze-frame). This is why
  every `/webrtc/start` publish must carry the current `mic` value — dropping it tears the m-line down.
- Robot decodes the inbound track and publishes PCM on `/audio/remote_mic`.

Browser is the **answerer**; the robot is always the offerer. The webapp finds the mic m-line in the
offer by its `a=recvonly` marker (unambiguous vs. the `a=sendonly` robot-mic slot), sets that
transceiver `sendonly`, and `replaceTrack`s the local mic onto it before `createAnswer`.

## Files changed

**Webapp** (`webapp/js/`)
- `webrtcSession.js` — `setMic(on)`, `#acquireMic()`/`#releaseMic()`, `#attachMic()`, `micRecvMid()` SDP
  helper; `mic` added to every `/webrtc/start` publish; `micRequested` state + `#micStream`/`#micTrack`.
- `teleop/videoStage.js` — `createMicToggle()` (mic-icon button, mirrors the robot-mic toggle).
- `teleop/main.js` — mounts the toggle on the right rail (`!config.simControls`).
- `types.d.ts` — `micRequested` on `WebRtcState`.

**Backend** (`ros2_ws/src/`)
- `innate_audio/` — **new package**: `Audio.msg` (PCM message). Required dependency.
- `mars_bot/mars_cam/` — recvonly transceiver on `mic:true`; `pad-added` → opus decode branch →
  `on_mic_sample` → publish `/audio/remote_mic`. `package.xml`/`CMakeLists.txt` depend on `innate_audio`.

## Build & test on the real robot

```bash
# In the robot's ros2_ws (needs the innate_audio package present):
colcon build --packages-select innate_audio mars_cam
source install/setup.bash
# webrtc_streamer_node is launched by the normal camera/webrtc bringup (real-robot launch).
```

1. Open the teleop page against the robot (WebRTC path — `simControls` off).
2. **Button location:** right-edge overlay of the video, a **mic icon next to the speaker (robot-mic)
   icon**. Click to toggle; the browser prompts for mic permission on first enable.
3. Verify:
   ```bash
   ros2 topic hz /audio/remote_mic          # ticks while the toggle is on
   ros2 topic echo /audio/remote_mic --field seq
   ```
   Browser devtools console should log `[webrtc] operator mic attached (mid=...)`.
4. Toggling off → `/audio/remote_mic` goes quiet and the mic m-line is dropped on the next handshake.

## Known limitations / notes

- **Not supported in the sim.** The sim webapp uses `SimSession` (Three.js canvas over rosbridge), not
  `WebRtcSession` — no peer connection, no `setMic`, so the toggle is intentionally hidden when
  `config.simControls` is set. Exercising it against sim would require running `webrtc_streamer_node`
  (`webrtc_streamer.sim.launch.py`) *and* forcing the webapp onto `WebRtcSession`.
- **No speaker playout yet** — `/audio/remote_mic` is published but nothing plays it on the robot.
- Toggling the mic re-handshakes (adds/removes the m-line), so it briefly freeze-frames the video. This
  is inherent to changing m-line topology, unlike the reneg-free robot-mic toggle.
- Concurrent senders: the backend seq is node-wide; a single active sender is the tested case.
- `phntm_bridge_client/` and `phntm_interfaces/` in the tree are **unrelated** to this feature and are
  not part of this branch's commits.
