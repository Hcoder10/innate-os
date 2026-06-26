# WebRTC `client_id` migration (mobile app)

The `webrtc_streamer` node now supports **multiple independent WebRTC peers at once**
(fan-out: each camera is encoded once and sent to every viewer). To be one of those
independent peers, a client tags itself with a **`client_id`** and signals on the
**`*_id`** topics. The mobile app currently uses the **legacy bare** topics and must
migrate, or it will not connect on this branch.

> The node is the **offerer** (it creates the SDP offer; the client answers). That has
> not changed.

## Two signaling paths

| | Legacy (bare) | Independent peer (this migration) |
|---|---|---|
| Selected by | START payload has **no** `client_id` | START payload **has** `client_id` |
| Concurrency | one peer (a new START replaces it) | many peers concurrently |
| Offer (node→client) | `/webrtc/offer` — raw SDP string | `/webrtc/offer_id` — `{client_id, sdp}` |
| Answer (client→node) | `/webrtc/answer` — raw SDP string | `/webrtc/answer_id` — `{client_id, sdp}` |
| ICE node→client | `/webrtc/ice_out` — `{candidate,…}` | `/webrtc/ice_out_id` — `{client_id, candidate,…}` |
| ICE client→node | `/webrtc/ice_in` — `{candidate,…}` | `/webrtc/ice_in_id` — `{client_id, candidate,…}` |
| START | `/webrtc/start` (shared) | `/webrtc/start` (shared) — same topic |

`/webrtc/start` is **always** the shared `/webrtc/start`; the `client_id` lives in its
JSON payload, not in the topic name.

All messages are `std_msgs/String` whose `data` is a JSON string.

## What the mobile app must change

1. **Generate a stable per-session `client_id`** (any unique string, e.g. a UUID) once
   at startup and put it in **every** message you send.

2. **START** — publish to `/webrtc/start`:
   ```json
   {"source":"live","audio":false,"client_id":"<your-id>","video":["main"]}
   ```
   - `video` is the **active** camera set to actually stream, e.g. `["main"]`, `["arm"]`,
     or `["main","arm"]`. The node supports **N cameras** (not just main/arm) — the live
     list of configured camera names is published on `/webrtc/active_streams` as
     `{"cameras":[...]}`; pass any of those names. The node always *negotiates every*
     camera for a `client_id` peer (so you can switch instantly later) but only
     **encodes/sends** the ones you list. Omit `video` to get all of them.
   - `audio: true` opts into the robot mic (per-peer; may fall back to video-only if the
     mic is already in use by another peer).
   - Re-send START to **(re)connect** or to **switch which cameras are active** — see
     "Stream switching" below.

3. **Subscribe `/webrtc/offer_id`**, parse `data` as JSON, **ignore it unless
   `client_id` matches yours**, then `setRemoteDescription(offer)`:
   ```js
   const env = JSON.parse(msg.data);
   if (env.client_id !== myClientId) return;   // someone else's peer
   await pc.setRemoteDescription({ type: "offer", sdp: env.sdp });
   ```

4. **Answer** — after `createAnswer()` + `setLocalDescription`, publish to
   `/webrtc/answer_id`:
   ```json
   {"client_id":"<your-id>","sdp":"<answer sdp>"}
   ```

5. **Trickle ICE out** — for each local candidate, publish to `/webrtc/ice_in_id`:
   ```json
   {"client_id":"<your-id>","candidate":"<cand>","sdpMLineIndex":0,"sdpMid":"video0"}
   ```

6. **Subscribe `/webrtc/ice_out_id`**, parse JSON, **ignore unless `client_id` matches**,
   then `addIceCandidate`.

That's the whole change — it's purely the topic names + a `{client_id, …}` envelope and a
client_id filter on the two inbound topics. The offer/answer/ICE *flow* is unchanged.

## Tracks / m-lines

For a `client_id` peer the offer has one video m-line per configured camera, in the order
of the `cameras` array from `/webrtc/active_streams` — i.e. `video<i>` = `cameras[i]`
(audio after, if requested). With the default config that's `video0 = main`, `video1 =
arm`, but read the list rather than hardcoding it. Map incoming tracks by the transceiver
`mid` (`video0`/`video1`/…), not by arrival order. A camera you didn't list in `video`
still arrives as a (silent) track until you activate it.

## Stream switching (no reconnect)

To change which cameras you're viewing, **re-send START with the same `client_id`** and a
new `video` list. The node flips which cameras it pushes **live** — no new offer/answer,
no ICE, no reconnect. Keep your `RTCPeerConnection` open; don't tear it down to switch.

## Connection lifecycle

- The node releases your peer if it stops receiving RTCP for a few seconds (you went away)
  or if you never finish connecting. To disconnect cleanly, send START with `video: []`
  (and `audio:false`) — the node drops your peer immediately.
- No STUN/TURN is needed on the LAN; the node handles the browser's mDNS `.local` ICE
  candidates itself.

## Reference implementations

- `webapp/js/webrtcSession.js` — production teleop client (this exact protocol).
- `webapp/debug/webrtc.html` — minimal standalone tester (connect, switch, multi-tab fan-out).

## Concurrency note

Multiple `client_id` peers stream **concurrently** (equal peers, no preemption at the node).
If the product wants "one viewer at a time," the client decides that policy itself (e.g. the
webapp watches `/webrtc/start` and yields when it sees a START from another `client_id`).
