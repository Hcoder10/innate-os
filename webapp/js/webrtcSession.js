// @ts-check
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

// No SDP offer back this soon after START → the START or its broadcast offer
// was dropped (rws /webrtc/* are fire-and-forget); cheap to just republish.
const OFFER_TIMEOUT_MS = 5_000;
// Offer applied but no media flowing this long → ICE/pipeline is stuck, and
// rebuilding is worth throwing away the in-flight negotiation.
const MEDIA_TIMEOUT_MS = 20_000;
const ICE_DEGRADE_MS = 10_000;
const AUDIO_REBUILD_DEBOUNCE_MS = 700;
const OFFER_GUARD_RESET_MS = 1_000;
// Escalating rebuilds before we surface an error and wait for a manual retry.
// At ~5s/attempt for dropped offers that's ~30s of fast retries instead of a
// single 30s stare-at-black, which is what made refreshing feel faster.
const MAX_HANDSHAKE_ATTEMPTS = 6;

export class WebRtcSession {
  /** @type {import("./rosClient.js").RosClient} */ #ros;
  /** @type {RTCPeerConnection | null} */ #pc = null;
  /** @type {WebRtcState} */ #state = {
    status: "idle",
    videoStream: null,
    audioStream: null,
    audioRequested: false,
  };
  /** @type {Set<(state: WebRtcState) => void>} */ #listeners = new Set();
  /** @type {(() => void)[]} */ #unsubs = [];

  #started = false;
  #builtWithAudio = false;
  #processingOffer = false;
  #remoteDescriptionSet = false;
  /** @type {RTCIceCandidateInit[]} */ #iceQueue = [];
  #videoTrackCount = 0;
  #handshakeAttempts = 0;
  // Unique per page-load; tags our own /webrtc/start so the start listener can
  // tell it apart from another device's (e.g. the phone app).
  #clientId = globalThis.crypto?.randomUUID?.() ?? Math.random().toString(36).slice(2);
  // Another device actively took the camera; we've yielded and won't
  // auto-reconnect until the operator explicitly retries (start()).
  #preempted = false;

  /** @type {number | null} */ #watchdog = null;
  /** @type {number | null} */ #degradeTimer = null;
  /** @type {number | null} */ #audioDebounce = null;

  /** @param {import("./rosClient.js").RosClient} rosClient */
  constructor(rosClient) {
    this.#ros = rosClient;
    this.#unsubs = [
      rosClient.subscribe(WEBRTC_OFFER_TOPIC, (p) => void this.#onOffer(p)),
      rosClient.subscribe(WEBRTC_ICE_OUT_TOPIC, (p) => void this.#onIceOut(p)),
      // Last-active-wins: a /webrtc/start we didn't send means another device
      // (the phone app) is actively opening the camera. Yield to it and stop
      // reconnecting rather than fighting back (which would ping-pong). The
      // operator reclaims it with Retry (start()).
      rosClient.subscribe(WEBRTC_START_TOPIC, (p) => this.#onStart(p)),
      rosClient.onStateChange((state) => {
        // The robot may have restarted while we were away; renegotiate.
        if (state === "connected" && this.#started && !this.#preempted) {
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
    this.#preempted = false; // explicit (re)take, even if we'd yielded the camera
    this.#handshakeAttempts = 0;
    this.#handshake();
  }

  /** Tear down entirely (drops the freeze-frame too). */
  stop() {
    this.#started = false;
    this.#closePc();
    this.#clearAudioDebounce();
    this.#patch({ status: "idle", videoStream: null, audioStream: null });
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
    const track = this.#state.audioStream?.getAudioTracks()[0];
    if (track) track.enabled = on;
    this.#patch({ audioRequested: on });

    this.#clearAudioDebounce();
    this.#audioDebounce = setTimeout(() => {
      this.#audioDebounce = null;
      if (this.#started && !this.#preempted && this.#builtWithAudio !== this.#state.audioRequested) {
        this.#handshake();
      }
    }, AUDIO_REBUILD_DEBOUNCE_MS);
  }

  // ---- handshake ----------------------------------------------------------

  #handshake() {
    this.#closePc();
    // Keep the last video frame on screen during the rebuild, but always drop
    // the audio stream: a dead mic track has no freeze-frame value, and
    // keeping it would mask a rebuilt connection that came up silent.
    if (this.#state.audioStream) this.#patch({ audioStream: null });
    if (!this.#state.videoStream) this.#patch({ status: "connecting" });

    if (this.#ros.state !== "connected") return; // resumes on reconnect

    const pc = new RTCPeerConnection({
      iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
    });
    this.#pc = pc;
    this.#builtWithAudio = this.#state.audioRequested;

    pc.onicecandidate = (event) => {
      if (this.#pc !== pc || !event.candidate) return;
      this.#ros.publish(WEBRTC_ICE_IN_TOPIC, {
        data: JSON.stringify({
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
      if (s === "connected" || s === "completed") {
        this.#clearDegradeTimer();
      } else if (s === "disconnected" || s === "failed") {
        this.#startDegradeTimer();
      }
    };

    // Phase 1: expect an SDP offer back within OFFER_TIMEOUT_MS. If none
    // arrives the START (or its broadcast offer) was dropped — republish fast.
    // Re-armed to the longer MEDIA_TIMEOUT_MS in #onOffer once we've answered.
    this.#armWatchdog(OFFER_TIMEOUT_MS);

    this.#ros.publish(WEBRTC_START_TOPIC, {
      data: JSON.stringify({ source: "live", audio: this.#state.audioRequested, client_id: this.#clientId }),
    });
  }

  /**
   * @param {RTCTrackEvent} event
   * @param {RTCPeerConnection} pc owning connection — ignore events from a
   *   superseded pc whose stopped tracks may still fire mute/unmute.
   */
  #onTrack(event, pc) {
    const track = event.track;
    const receiver = event.receiver;
    if (receiver) {
      try {
        if ("jitterBufferTarget" in receiver) receiver.jitterBufferTarget = 0;
        if ("playoutDelayHint" in receiver) receiver.playoutDelayHint = 0;
      } catch {
        // unsupported; default buffer applies
      }
    }
    if (track.kind === "audio") {
      // Start in the operator's chosen state; never audible by default.
      track.enabled = this.#state.audioRequested;
      this.#patch({ audioStream: new MediaStream([track]) });
      return;
    }

    const stream = new MediaStream([track]);
    const mid = event.transceiver?.mid;
    const isMain = mid === "0" || mid === "video0";
    const isArm = mid === "1" || mid === "video1";
    let main = false;
    if (isMain) {
      main = true;
    } else if (!isArm) {
      // Unknown mids: fall back to arrival order (main camera offers first).
      this.#videoTrackCount += 1;
      main = this.#videoTrackCount === 1;
    }
    if (!main) return; // arm camera — ignored in v1

    // A remote track arrives muted and unmutes when RTP actually flows — only
    // then is there a real frame. Exposing the stream before that swaps the
    // <video> to a black source and hides the "establishing video link"
    // overlay, so we hold off: the cold-start overlay (or the previous
    // freeze-frame) stays until media is genuinely live. ontrack itself is not
    // the signal, and we keep the watchdog armed until media arrives.
    const goLive = () => {
      if (this.#pc !== pc) return; // stale track from a superseded pc
      this.#handshakeAttempts = 0;
      this.#clearWatchdog();
      this.#patch({ videoStream: stream, status: "streaming" });
    };

    if (!track.muted) goLive();
    track.addEventListener("unmute", goLive);
    track.addEventListener("mute", () => {
      // Media stalled mid-stream — keep the last good frame frozen and flag
      // connecting so the stage degrades it; the degrade/handshake timers
      // drive recovery.
      if (this.#pc === pc && this.#state.videoStream === stream && this.#started) {
        this.#patch({ status: "connecting" });
      }
    });
  }

  /** @param {any} payload /webrtc/offer message (StringMsg, dual-path) */
  async #onOffer(payload) {
    const sdp = payload?.data ?? payload?.msg?.data;
    const pc = this.#pc;
    if (typeof sdp !== "string" || !sdp || !pc) return;
    if (this.#processingOffer || pc.signalingState !== "stable") return;

    this.#processingOffer = true;
    try {
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

      const answer = await pc.createAnswer();
      if (this.#pc !== pc) return;
      await pc.setLocalDescription(answer);
      if (this.#pc !== pc) return;

      this.#ros.publish(WEBRTC_ANSWER_TOPIC, { data: answer.sdp ?? "" });
    } catch (err) {
      if (this.#pc === pc) console.error("[webrtc] offer processing failed:", err);
    } finally {
      // Brief guard so a duplicate offer broadcast doesn't double-process.
      setTimeout(() => {
        this.#processingOffer = false;
      }, OFFER_GUARD_RESET_MS);
    }
  }

  /** @param {any} payload /webrtc/ice_out message (StringMsg, dual-path) */
  async #onIceOut(payload) {
    const raw = payload?.data ?? payload?.msg?.data;
    const pc = this.#pc;
    if (typeof raw !== "string" || !raw || !pc) return;
    try {
      const parsed = JSON.parse(raw);
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

  /**
   * A /webrtc/start appeared on the topic. If it's ours (matching client_id),
   * ignore it. Otherwise another device (the phone app) is actively opening the
   * camera — yield: tear down and stop auto-reconnecting so we don't ping-pong
   * the stream. The operator reclaims it via Retry (start()), which clears it.
   * @param {any} payload /webrtc/start message (StringMsg, dual-path)
   */
  #onStart(payload) {
    if (!this.#started || this.#preempted) return;
    const raw = payload?.data ?? payload?.msg?.data;
    if (typeof raw !== "string") return;
    let id;
    try {
      id = JSON.parse(raw)?.client_id;
    } catch {
      id = undefined; // untagged / unparseable → treat as another device
    }
    if (id === this.#clientId) return; // our own start, echoed back to us
    this.#preempted = true;
    this.#closePc();
    this.#patch({ status: "preempted", videoStream: null, audioStream: null });
  }

  // ---- timers & teardown --------------------------------------------------

  #startDegradeTimer() {
    if (this.#degradeTimer !== null) return;
    this.#degradeTimer = setTimeout(() => {
      this.#degradeTimer = null;
      if (this.#started && !this.#preempted) {
        console.warn("[webrtc] ICE degraded for 10s, rebuilding");
        this.#handshake();
      }
    }, ICE_DEGRADE_MS);
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
