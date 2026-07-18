// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// WebRtcSession — one long-lived RTCPeerConnection signaled over the shared
// RosClient (no second socket).
//
// Handshake (robot is the offerer): build pc → publish /webrtc/start →
// robot sends SDP offer on /webrtc/offer → setRemoteDescription, drain the
// queued robot ICE, createAnswer → publish /webrtc/answer → trickle ICE both
// ways (client → /webrtc/ice_in, robot → /webrtc/ice_out).
//
// Tracks: audio starts disabled (robot mic must never be audible before the
// operator opts in); video dispatched by transceiver mid with arrival-order
// fallback; only the main camera is exposed (the arm camera is ignored in v1).
// Operator mic (setMic) sends the reverse direction — the browser's mic up to
// the robot on a recvonly audio m-line the robot always offers. #onOffer binds
// that m-line sendonly (with the current mic track, or none) before answering,
// so toggling the mic is a live replaceTrack flip — no renegotiation.
//
// Self-heal: 30 s initial-handshake watchdog, ICE disconnected/failed
// persisting 10 s → re-handshake, and a re-handshake whenever the rosbridge
// link comes back. Audio config changes debounce 700 ms then rebuild the pc —
// the robot rebuilds its whole pipeline on every START, so there is no
// renegotiation path. The old video stream is kept during rebuilds so the
// stage shows a freeze-frame instead of flashing black.

import {
  WEBRTC_START_TOPIC,
  WEBRTC_OFFER_TOPIC,
  WEBRTC_ANSWER_TOPIC,
  WEBRTC_ICE_IN_TOPIC,
  WEBRTC_ICE_OUT_TOPIC,
} from "./constants.js";
import { createLocalPeerConnection, describeIceCandidate, wireDiagnosticDataChannels } from "./webrtcConfig.js";
import { setMicAudioActive } from "./micAudioState.js";

// No SDP offer back this soon after START → the START or its broadcast offer
// was dropped (rws /webrtc/* are fire-and-forget); cheap to just republish.
// Must comfortably exceed the server's worst-case offer latency: the sim's
// aiortc can't trickle ICE, so its offer waits for full candidate gathering
// (~5s when an interface has no route to STUN, e.g. VPN utuns). A timeout at
// ~that latency re-STARTs right as the offer lands, superseding the peer the
// offer belongs to — each cycle stays one offer behind and takes minutes to
// converge instead of seconds.
const OFFER_TIMEOUT_MS = 12_000;
// Offer applied but no media flowing this long → ICE/pipeline is stuck, and
// rebuilding is worth throwing away the in-flight negotiation.
const MEDIA_TIMEOUT_MS = 7_000;
const ICE_DEGRADE_MS = 10_000;
const AUDIO_REBUILD_DEBOUNCE_MS = 700;
const OFFER_GUARD_RESET_MS = 1_000;
// Escalating rebuilds before we surface an error and wait for a manual retry.
// Bounded retries instead of a single long stare-at-black, which is what made
// refreshing feel faster.
const MAX_HANDSHAKE_ATTEMPTS = 3;

/**
 * Mid of the robot's recvonly audio m-line — the phone-mic slot we send into — or null if absent.
 * The robot marks this m-line a=recvonly (it receives); the robot->browser send-audio m-line is
 * a=sendonly, so recvonly uniquely identifies the mic slot regardless of m-line order.
 * @param {string} sdp
 * @returns {string | null}
 */
function micRecvMid(sdp) {
  let inAudio = false;
  let mid = null;
  let recvonly = false;
  for (const line of sdp.split(/\r?\n/)) {
    if (line.startsWith("m=")) {
      if (inAudio && recvonly && mid) return mid; // the section that just ended was the mic slot
      inAudio = line.startsWith("m=audio");
      mid = null;
      recvonly = false;
    } else if (inAudio) {
      if (line.startsWith("a=mid:")) mid = line.slice(6).trim();
      else if (line.trim() === "a=recvonly") recvonly = true;
    }
  }
  return inAudio && recvonly && mid ? mid : null;
}

export class WebRtcSession {
  /** @type {import("./rosClient.js").RosClient} */ #ros;
  /** @type {RTCPeerConnection | null} */ #pc = null;
  /** @type {WebRtcState} */ #state = {
    status: "idle",
    videoStream: null,
    videoStreams: [],
    videoLive: [],
    audioStream: null,
    audioRequested: false,
    micRequested: false,
    iceState: "new",
    stunFallback: false,
  };
  /** @type {Set<(state: WebRtcState) => void>} */ #listeners = new Set();
  /** @type {(() => void)[]} */ #unsubs = [];

  #started = false;
  #builtWithAudio = false;
  // Operator microphone captured for sending to the robot. Kept across pc rebuilds (like the video
  // freeze-frame) so a re-handshake re-binds the same track without re-prompting for permission; only
  // stopped when the operator turns the mic off or the session stops.
  /** @type {MediaStream | null} */ #micStream = null;
  /** @type {MediaStreamTrack | null} */ #micTrack = null;
  // Sender of the always-negotiated mic m-line on the current pc; replaceTrack on it flips the mic live.
  /** @type {RTCRtpSender | null} */ #micSender = null;
  #processingOffer = false;
  #remoteDescriptionSet = false;
  /** @type {RTCIceCandidateInit[]} */ #iceQueue = [];
  #videoTrackCount = 0;
  #handshakeAttempts = 0;
  // Sticky for the session once a connect attempt fails: the next pc is rebuilt with a public STUN server
  // added (see webrtcConfig). Off on the happy path so we never hit a third-party server when the robot's
  // own STUN responder is reachable. Reset only on a full stop().
  #useFallbackStun = false;
  // Multi-camera: the robot negotiates every camera's m-line (video0, video1, …); we keep each track's
  // stream by m-line index. The robot encodes/pushes the ACTIVE set (and only those); of those, one is
  // PRIMARY (the big stage), the rest are live PiP thumbnails. Changing the active set or the primary is a
  // no-reneg START (or no START at all, for promotion within the already-live set), so it's instant.
  /** @type {(MediaStream | null)[]} */ #videoStreams = [];
  /** @type {boolean[]} */ #videoLive = [];
  // Camera names the robot should push (the START `video:` payload). Bootstrap guess until the UI learns
  // the real roster from /webrtc/active_streams and calls setActiveCameras.
  /** @type {string[]} */ #activeCams = ["main"];
  #primaryIndex = 0;
  #primaryName = "main";
  // Unique per page-load; the robot routes our offer/answer/ICE on the *_id topics by this id, so we
  // negotiate as an independent peer (and stream concurrently with any other device).
  #clientId = globalThis.crypto?.randomUUID?.() ?? Math.random().toString(36).slice(2);

  /** @type {number | null} */ #watchdog = null;
  /** @type {number | null} */ #degradeTimer = null;
  /** @type {number | null} */ #audioDebounce = null;

  /** @param {import("./rosClient.js").RosClient} rosClient */
  constructor(rosClient) {
    this.#ros = rosClient;
    this.#unsubs = [
      rosClient.subscribe(WEBRTC_OFFER_TOPIC, (p) => void this.#onOffer(p), undefined, "std_msgs/msg/String"),
      rosClient.subscribe(WEBRTC_ICE_OUT_TOPIC, (p) => void this.#onIceOut(p), undefined, "std_msgs/msg/String"),
      // We're an independent peer (client_id), so we do NOT yield when another device opens the
      // camera — the robot fans out to all viewers concurrently. (No /webrtc/start watch / preemption.)
      rosClient.onStateChange((state) => {
        // The robot may have restarted while we were away; renegotiate.
        if (state === "connected" && this.#started) {
          this.#handshakeAttempts = 0;
          this.#handshake();
        }
      }),
    ];
  }

  /** @returns {WebRtcState} */
  get state() {
    return this.#state;
  }

  /**
   * Live RTCStatsReport for the profiling panel, or null when no pc is up.
   * @returns {Promise<RTCStatsReport | null>}
   */
  getStats() {
    return this.#pc ? this.#pc.getStats() : Promise.resolve(null);
  }

  /**
   * @param {(state: WebRtcState) => void} cb Fires immediately, then on change.
   * @returns {() => void} unsubscribe
   */
  onChange(cb) {
    this.#listeners.add(cb);
    cb(this.#state);
    return () => this.#listeners.delete(cb);
  }

  /** Begin (or manually retry) the video link. */
  start() {
    this.#started = true;
    this.#handshakeAttempts = 0;
    this.#handshake();
  }

  /** Tear down entirely (drops the freeze-frame too). */
  stop() {
    this.#started = false;
    this.#useFallbackStun = false;
    this.#closePc();
    this.#clearAudioDebounce();
    this.#releaseMic();
    // No robot-mic stream once stopped (e.g. leaving the teleop page) — let TTS play.
    setMicAudioActive(false);
    this.#patch({
      status: "idle",
      videoStream: null,
      audioStream: null,
      micRequested: false,
      iceState: "new",
      stunFallback: false,
    });
  }

  destroy() {
    this.stop();
    for (const unsub of this.#unsubs) unsub();
    this.#unsubs = [];
    this.#listeners.clear();
  }

  /**
   * Toggle robot-mic audio. The current track (if any) is flipped instantly;
   * the pipeline rebuild needed to add/remove the audio m-line is debounced
   * so rapid toggling costs one re-handshake, not several.
   * @param {boolean} on
   */
  setAudio(on) {
    if (this.#state.audioRequested === on) return;
    // The robot always negotiates the audio m-line for us, so toggling is a no-reneg START (it starts/
    // stops SENDING audio + opens/closes the mic) — no reconnect. Flip the local <audio> instantly too.
    const track = this.#state.audioStream?.getAudioTracks()[0];
    if (track) track.enabled = on;
    this.#patch({ audioRequested: on });
    // Tell TTS playback to stand down while we're audible via the mic.
    setMicAudioActive(on);
    if (!this.#started || this.#ros.state !== "connected") return;
    if (this.#pc) {
      this.#ros.publish(WEBRTC_START_TOPIC, {
        data: JSON.stringify({
          source: "live",
          audio: on,
          mic: this.#state.micRequested,
          client_id: this.#clientId,
          video: this.#activeCams,
        }),
      });
      console.log("[webrtc] audio toggle ->", on, "(no reconnect)");
    } else {
      // No live peer (map-only, all cameras off) — bring one up so the mic can flow.
      this.#handshake();
    }
  }

  /**
   * Toggle sending the OPERATOR's microphone up to the robot (browser -> robot -> ROS, played out the
   * robot). This is the opposite direction from setAudio, which RECEIVES the robot's mic. The robot
   * always negotiates the recvonly mic m-line and #onOffer binds it sendonly, so toggling is a live
   * replaceTrack flip — no renegotiation, no reconnect. Acquiring the mic here (inside the click gesture)
   * both keeps the permission prompt off the happy path and lets a denial leave us cleanly off.
   * @param {boolean} on
   * @returns {Promise<void>}
   */
  async setMic(on) {
    if (this.#state.micRequested === on) return;
    if (on) {
      if (!(await this.#acquireMic())) return; // permission denied / no device — stay off
    } else {
      this.#releaseMic();
    }
    this.#patch({ micRequested: on });
    if (!this.#started || this.#ros.state !== "connected") return;
    if (this.#pc && this.#micSender) {
      try {
        await this.#micSender.replaceTrack(on ? this.#micTrack : null);
        // Reneg-free START keeps the robot's view of the mic flag current (status/release decisions).
        this.#ros.publish(WEBRTC_START_TOPIC, {
          data: JSON.stringify({
            source: "live",
            audio: this.#state.audioRequested,
            mic: on,
            client_id: this.#clientId,
            video: this.#activeCams,
          }),
        });
        console.log("[webrtc] operator mic ->", on, "(live replaceTrack, no reconnect)");
        return;
      } catch (err) {
        console.warn("[webrtc] mic replaceTrack failed, falling back to re-handshake:", err);
      }
    } else if (this.#pc) {
      console.warn("[webrtc] no mic sender on this pc (old robot offer?) — re-handshaking");
    }
    // No live pc (map-only) or no bound mic sender — a fresh handshake binds the mic in #onOffer.
    this.#handshakeAttempts = 0;
    this.#handshake();
  }

  /** @returns {Promise<boolean>} true once a live mic track is held. */
  async #acquireMic() {
    if (this.#micTrack && this.#micTrack.readyState === "live") return true;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      this.#micStream = stream;
      this.#micTrack = stream.getAudioTracks()[0] ?? null;
      return this.#micTrack != null;
    } catch (err) {
      console.warn("[webrtc] microphone unavailable:", err);
      return false;
    }
  }

  /** Stop and drop the local mic track, releasing the OS microphone. */
  #releaseMic() {
    if (this.#micStream) {
      for (const track of this.#micStream.getTracks()) track.stop();
    }
    this.#micStream = null;
    this.#micTrack = null;
  }

  /**
   * Bind the robot-offered recvonly audio m-line sendonly on this pc — with the current mic track when
   * the mic is on, or trackless otherwise — and remember its sender so setMic can flip the track live
   * (replaceTrack, no renegotiation). The robot always offers this m-line.
   * @param {RTCPeerConnection} pc owning connection
   * @param {string} offerSdp the robot's offer SDP (already applied via setRemoteDescription)
   * @returns {Promise<void>}
   */
  async #attachMic(pc, offerSdp) {
    const mid = micRecvMid(offerSdp);
    if (!mid) {
      if (this.#state.micRequested) console.warn("[webrtc] mic requested but offer has no recvonly audio m-line");
      return;
    }
    const transceiver = pc.getTransceivers().find((t) => t.mid === mid);
    if (!transceiver) return;
    try {
      transceiver.direction = "sendonly";
      const track = this.#state.micRequested ? this.#micTrack : null;
      await transceiver.sender.replaceTrack(track);
      this.#micSender = transceiver.sender;
      console.log("[webrtc] mic m-line bound (mid=" + mid + ", track=" + (track ? "live" : "none") + ")");
    } catch (err) {
      console.warn("[webrtc] failed to bind mic m-line:", err);
    }
  }

  /**
   * Set which cameras the robot should encode and push (the live set: primary + PiP thumbnails). The
   * robot negotiated every camera up front, so switching between non-empty sets is a no-reneg START
   * (instant). Going to an EMPTY set releases the peer entirely (a map-only view costs zero streaming);
   * coming back from empty is a fresh handshake, since the released peer can't be renegotiated.
   * @param {string[]} names camera names the robot keys on, in m-line order
   */
  setActiveCameras(names) {
    const next = [...names];
    if (next.length === this.#activeCams.length && next.every((n, i) => n === this.#activeCams[i])) return;
    const wasEmpty = this.#activeCams.length === 0;
    this.#activeCams = next;
    if (!this.#started || this.#ros.state !== "connected") return;
    // Empty set, returning from one, or no live pc → (re)handshake, which also handles the release case.
    // A normal switch between non-empty sets stays reneg-free.
    if (next.length === 0 || wasEmpty || !this.#pc) {
      this.#handshakeAttempts = 0;
      this.#handshake();
      return;
    }
    this.#ros.publish(WEBRTC_START_TOPIC, {
      data: JSON.stringify({
        source: "live",
        video: next,
        audio: this.#state.audioRequested,
        mic: this.#state.micRequested,
        client_id: this.#clientId,
      }),
    });
    console.log("[webrtc] active cameras ->", next.join("+"), "(no reconnect)");
  }

  /**
   * Choose which active camera fills the big stage. Both old and new primary are already in the active
   * set (already streaming), so this is purely a display swap — no START, instant.
   * @param {number} index m-line index of the camera (video0 -> 0, …)
   * @param {string} name camera name (for diagnostics)
   */
  setPrimaryCamera(index, name) {
    if (this.#primaryIndex === index) return;
    this.#primaryIndex = index;
    this.#primaryName = name;
    // Show the now-primary track immediately; if it isn't live yet the previous frame/overlay holds until
    // its `unmute` lands (showLive patches it then).
    const stream = this.#videoStreams[index] ?? null;
    if (stream) this.#patch({ videoStream: stream, status: this.#videoLive[index] ? "streaming" : "connecting" });
    console.log("[webrtc] primary camera " + name + " (index " + index + ", no reconnect)");
  }

  /** @returns {{ index: number, name: string }} the currently displayed (big) camera */
  get primaryCamera() {
    return { index: this.#primaryIndex, name: this.#primaryName };
  }

  // ---- handshake ----------------------------------------------------------

  #handshake() {
    // Map-only view: no cameras (and no mic) requested. Tear the peer down and stay idle — the robot
    // releases its side on an empty START, and arming a watchdog for media that will never come would
    // just thrash. The map runs over rosbridge, so it's unaffected. (Mic-only still builds a peer below.)
    if (this.#activeCams.length === 0 && !this.#state.audioRequested && !this.#state.micRequested) {
      this.#closePc();
      this.#videoLive = [];
      this.#patch({ status: "idle", videoStream: null, ...this.#videoArrays() });
      if (this.#ros.state === "connected") {
        this.#ros.publish(WEBRTC_START_TOPIC, {
          data: JSON.stringify({ source: "live", video: [], audio: false, mic: false, client_id: this.#clientId }),
        });
      }
      console.log("[webrtc] no streams requested — peer released (map-only)");
      return;
    }

    this.#closePc();
    // Keep the last video frame on screen during the rebuild, but always drop
    // the audio stream: a dead mic track has no freeze-frame value, and
    // keeping it would mask a rebuilt connection that came up silent.
    if (this.#state.audioStream) this.#patch({ audioStream: null });
    if (!this.#state.videoStream) this.#patch({ status: "connecting" });

    if (this.#ros.state !== "connected") return; // resumes on reconnect

    const pc = createLocalPeerConnection(this.#ros.ip, { fallback: this.#useFallbackStun });
    this.#pc = pc;
    this.#builtWithAudio = this.#state.audioRequested;
    wireDiagnosticDataChannels(pc);

    pc.onicecandidate = (event) => {
      if (this.#pc !== pc || !event.candidate) return;
      const c = event.candidate;
      // Log every candidate we send the robot: type (host/srflx/relay) + address — `.local` means Chrome
      // obfuscated a host IP behind an mDNS name (the robot must peer-reflexive past it).
      console.log(
        "[webrtc] ice ->",
        describeIceCandidate(c),
      );
      this.#ros.publish(WEBRTC_ICE_IN_TOPIC, {
        data: JSON.stringify({
          client_id: this.#clientId, // envelope: the robot routes our ICE by client_id (independent peer)
          candidate: event.candidate.candidate,
          sdpMLineIndex: event.candidate.sdpMLineIndex,
          sdpMid: event.candidate.sdpMid,
        }),
      });
    };

    pc.ontrack = (event) => {
      if (this.#pc !== pc) return;
      this.#onTrack(event, pc);
    };

    pc.oniceconnectionstatechange = () => {
      if (this.#pc !== pc) return;
      const s = pc.iceConnectionState;
      console.log("[webrtc] ice:", s);
      this.#patch({ iceState: s });
      if (s === "connected" || s === "completed") {
        this.#clearDegradeTimer();
      } else if (s === "failed") {
        // No working candidate pair — turn on the public-STUN fallback so the degrade-timer rebuild below
        // can learn an srflx candidate even when the robot's own STUN responder isn't reachable.
        this.#enableStunFallback();
        // ICE exhausted every candidate pair without finding a working one = NO NETWORK PATH between this
        // browser and the robot (e.g. the robot can't reach our host candidates — mDNS-obfuscated on a
        // network where they don't resolve — and srflx/NAT-hairpin didn't work either).
        console.error(
          "[webrtc] NO USABLE NETWORK PATH to the robot — ICE failed (no candidate pair connected). " +
            "If you're on the same LAN, your browser may be hiding its local IP via mDNS; the robot " +
            "couldn't open a return path. Workaround: set media.peerconnection.ice.obfuscate_host_addresses=false.",
        );
        this.#startDegradeTimer();
      } else if (s === "disconnected") {
        this.#startDegradeTimer();
      }
    };
    pc.onconnectionstatechange = () => {
      if (this.#pc === pc) console.log("[webrtc] connection:", pc.connectionState);
    };

    // Phase 1: expect an SDP offer back within OFFER_TIMEOUT_MS. If none
    // arrives the START (or its broadcast offer) was dropped — republish fast.
    // Re-armed to the longer MEDIA_TIMEOUT_MS in #onOffer once we've answered.
    this.#armWatchdog(OFFER_TIMEOUT_MS);

    // Ask the robot to push just the selected camera. It still negotiates every camera's transceiver for
    // a client_id peer (so switching is reneg-free), but won't encode/send the others until we request
    // them — so we don't waste its bandwidth/CPU on cameras we're not viewing.
    this.#ros.publish(WEBRTC_START_TOPIC, {
      data: JSON.stringify({
        source: "live",
        audio: this.#state.audioRequested,
        mic: this.#state.micRequested,
        client_id: this.#clientId,
        renegotiate: true,
        video: this.#activeCams,
      }),
    });
    console.log("[webrtc] handshake: START sent", {
      client_id: this.#clientId,
      audio: this.#builtWithAudio,
      mic: this.#state.micRequested,
    });
  }

  /**
   * @param {RTCTrackEvent} event
   * @param {RTCPeerConnection} pc owning connection — ignore events from a
   *   superseded pc whose stopped tracks may still fire mute/unmute.
   */
  #onTrack(event, pc) {
    const track = event.track;
    console.log("[webrtc] track:", track.kind, "mid=" + (event.transceiver?.mid ?? "?"));
    if (track.kind === "audio") {
      // Start in the operator's chosen state; never audible by default.
      // NB: deliberately not tuned below — zeroing the audio receiver's NetEq
      // buffer starves mic audio under jitter, and the latency win is video-only.
      track.enabled = this.#state.audioRequested;
      this.#patch({ audioStream: new MediaStream([track]) });
      return;
    }

    // Minimize the receive-side jitter buffer on the video receiver. The
    // playout-delay extension caps the ceiling; these pin the floor.
    // Units differ: jitterBufferTarget is in milliseconds, playoutDelayHint in
    // seconds. Both 0 here means "minimal delay". Modern Chrome honors
    // jitterBufferTarget and ignores the hint (not a strict fallback — both are
    // set whenever present). To match the robot-side max=40, the values diverge:
    // jitterBufferTarget = 40, playoutDelayHint = 0.04.
    const receiver = event.receiver;
    if (receiver) {
      try {
        if ("jitterBufferTarget" in receiver) receiver.jitterBufferTarget = 0;
        if ("playoutDelayHint" in receiver) receiver.playoutDelayHint = 0;
      } catch {
        // unsupported; default buffer applies
      }
    }

    const stream = new MediaStream([track]);
    // m-line index: "video0"/"0" -> 0, "video1"/"1" -> 1, … We keep every camera's stream by index. The
    // active set renders live (primary big + PiP thumbnails); the rest stay warm (negotiated) but unpushed.
    const mid = event.transceiver?.mid ?? "";
    const m = /(\d+)$/.exec(mid);
    const index = m ? Number(m[1]) : this.#videoTrackCount;
    this.#videoTrackCount += 1;
    this.#videoStreams[index] = stream;
    this.#videoLive[index] = false;

    // A remote track arrives muted and unmutes when RTP actually flows — only then is there a real frame.
    // We track liveness per camera so the PiP strip can show each thumbnail's state; the big stage only
    // swaps to a stream once it's genuinely live (else the cold-start overlay / previous freeze-frame holds).
    const showLive = () => {
      if (this.#pc !== pc) return; // stale pc
      this.#videoLive[index] = true;
      if (index === this.#primaryIndex) {
        // The displayed camera went live: clear the handshake watchdog and reveal it on the big stage.
        this.#handshakeAttempts = 0;
        this.#clearWatchdog();
        console.log("[webrtc] primary video live (camera index " + index + ")");
        this.#patch({ videoStream: stream, status: "streaming", ...this.#videoArrays() });
      } else {
        this.#patchVideo(); // a PiP thumbnail came up
      }
    };

    if (!track.muted) showLive();
    track.addEventListener("unmute", showLive);
    track.addEventListener("mute", () => {
      if (this.#pc !== pc || !this.#started) return;
      this.#videoLive[index] = false;
      // Media stalled mid-stream — keep the last good frame frozen. For the primary, flag connecting so the
      // stage degrades it (the degrade/handshake timers drive recovery); for a PiP, just mark it not-live.
      if (index === this.#primaryIndex && this.#state.videoStream === stream) {
        this.#patch({ status: "connecting", ...this.#videoArrays() });
      } else {
        this.#patchVideo();
      }
    });
  }

  /** Fresh copies of the per-camera stream/liveness arrays, so every patch emits a new reference. */
  #videoArrays() {
    return { videoStreams: this.#videoStreams.slice(), videoLive: this.#videoLive.slice() };
  }

  /** Re-emit the per-camera stream/liveness arrays so the PiP strip refreshes (no status change). */
  #patchVideo() {
    this.#patch(this.#videoArrays());
  }

  /** @param {any} payload /webrtc/offer_id message: std_msgs/String whose data is {client_id, sdp} */
  async #onOffer(payload) {
    const raw = payload?.data ?? payload?.msg?.data;
    if (typeof raw !== "string" || !raw) return;
    let env;
    try { env = JSON.parse(raw); } catch { return; }
    if (env.client_id !== this.#clientId) return; // an offer for some other device's peer
    const sdp = env.sdp;
    const pc = this.#pc;
    if (typeof sdp !== "string" || !sdp || !pc) return;
    if (this.#processingOffer || pc.signalingState !== "stable") return;

    this.#processingOffer = true;
    try {
      console.log("[webrtc] offer received (" + sdp.length + "B), answering");
      await pc.setRemoteDescription({ type: "offer", sdp });
      if (this.#pc !== pc) return;
      this.#remoteDescriptionSet = true;
      // Offer applied — past the lost-START window; now we're waiting on ICE
      // and media, which deserves a longer leash before we rebuild.
      this.#armWatchdog(MEDIA_TIMEOUT_MS);

      for (const candidate of this.#iceQueue) {
        if (this.#pc !== pc) return;
        try {
          await pc.addIceCandidate(candidate);
        } catch {
          // Malformed/stale candidates are common and harmless.
        }
      }
      this.#iceQueue = [];

      // Attach the operator mic to the robot's recvonly audio m-line before answering, so the answer's
      // matching m-line is sendonly (browser -> robot). Must happen after setRemoteDescription (the
      // transceiver only exists once the offer is applied) and before createAnswer.
      await this.#attachMic(pc, sdp);
      if (this.#pc !== pc) return;

      const answer = await pc.createAnswer();
      if (this.#pc !== pc) return;
      await pc.setLocalDescription(answer);
      if (this.#pc !== pc) return;

      this.#ros.publish(WEBRTC_ANSWER_TOPIC, {
        data: JSON.stringify({ client_id: this.#clientId, sdp: answer.sdp ?? "" }),
      });
      console.log("[webrtc] answer sent");
    } catch (err) {
      if (this.#pc === pc) console.error("[webrtc] offer processing failed:", err);
    } finally {
      // Brief guard so a duplicate offer broadcast doesn't double-process.
      setTimeout(() => {
        this.#processingOffer = false;
      }, OFFER_GUARD_RESET_MS);
    }
  }

  /** @param {any} payload /webrtc/ice_out_id message: std_msgs/String whose data is {client_id, candidate, ...} */
  async #onIceOut(payload) {
    const raw = payload?.data ?? payload?.msg?.data;
    const pc = this.#pc;
    if (typeof raw !== "string" || !raw || !pc) return;
    try {
      const parsed = JSON.parse(raw);
      if (parsed.client_id !== this.#clientId) return; // a candidate for some other device's peer
      if (!parsed.candidate) return;
      /** @type {RTCIceCandidateInit} */
      const candidate = {
        candidate: String(parsed.candidate),
        sdpMLineIndex: parsed.sdpMLineIndex ?? 0,
        sdpMid: parsed.sdpMid ?? undefined,
      };
      if (!this.#remoteDescriptionSet) {
        this.#iceQueue.push(candidate);
      } else {
        await pc.addIceCandidate(candidate);
      }
    } catch {
      // ICE parse/add failures are usually transient; the next candidate wins.
    }
  }

  // ---- timers & teardown --------------------------------------------------

  #startDegradeTimer() {
    if (this.#degradeTimer !== null) return;
    this.#degradeTimer = setTimeout(() => {
      this.#degradeTimer = null;
      if (this.#started) {
        // Connected briefly then lost the path (or never got media). Repeated rebuilds here usually mean
        // the robot can't keep a return path to this browser — see the NO USABLE NETWORK PATH note above.
        console.warn("[webrtc] no stable media path for 10s (robot may have no route back to us), rebuilding");
        this.#handshake();
      }
    }, ICE_DEGRADE_MS);
  }

  /** Latch the public-STUN fallback on (sticky until stop()); the next #handshake() rebuilds with it. */
  #enableStunFallback() {
    if (this.#useFallbackStun) return;
    this.#useFallbackStun = true;
    console.warn("[webrtc] enabling public STUN fallback for subsequent rebuilds");
    this.#patch({ stunFallback: true });
  }

  #clearDegradeTimer() {
    if (this.#degradeTimer !== null) {
      clearTimeout(this.#degradeTimer);
      this.#degradeTimer = null;
    }
  }

  /**
   * Arm (or re-arm) the handshake watchdog. On fire, escalate: rebuild the pc
   * up to MAX_HANDSHAKE_ATTEMPTS times, then surface an error for manual retry.
   * A freeze-frame stream survives the rebuilds (status stays "streaming"); a
   * cold start shows "establishing video link" throughout.
   * @param {number} ms
   */
  #armWatchdog(ms) {
    this.#clearWatchdog();
    this.#watchdog = setTimeout(() => {
      this.#watchdog = null;
      this.#handshakeAttempts += 1;
      if (this.#handshakeAttempts < MAX_HANDSHAKE_ATTEMPTS) {
        // The first attempt used the local-only config; escalate the rebuilds onto the public-STUN
        // fallback in case the missing media is an unreachable robot STUN responder (no srflx candidate).
        this.#enableStunFallback();
        console.warn(`[webrtc] no media yet (attempt ${this.#handshakeAttempts}), rebuilding`);
        this.#handshake();
      } else {
        console.error("[webrtc] no media after repeated handshakes");
        this.#closePc();
        this.#patch({ status: "error" });
      }
    }, ms);
  }

  #clearWatchdog() {
    if (this.#watchdog !== null) {
      clearTimeout(this.#watchdog);
      this.#watchdog = null;
    }
  }

  #clearAudioDebounce() {
    if (this.#audioDebounce !== null) {
      clearTimeout(this.#audioDebounce);
      this.#audioDebounce = null;
    }
  }

  #closePc() {
    this.#clearWatchdog();
    this.#clearDegradeTimer();
    this.#micSender = null;
    this.#processingOffer = false;
    this.#remoteDescriptionSet = false;
    this.#iceQueue = [];
    this.#videoTrackCount = 0;
    const pc = this.#pc;
    this.#pc = null;
    if (pc) {
      pc.onicecandidate = null;
      pc.ontrack = null;
      pc.oniceconnectionstatechange = null;
      pc.close();
    }
  }

  /** @param {Partial<WebRtcState>} patch */
  #patch(patch) {
    this.#state = { ...this.#state, ...patch };
    for (const cb of [...this.#listeners]) {
      try {
        cb(this.#state);
      } catch (err) {
        console.error("[webrtc] listener threw:", err);
      }
    }
  }
}
