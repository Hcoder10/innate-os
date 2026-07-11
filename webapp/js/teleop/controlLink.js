// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Cross-network control links for leader-arm teleop (arm panel "control
// path" selector). Speaks the tools/teleop_bench wire format: the production
// 38-byte leader packet (magic 0xAA55, u32 seq, f64 ms timestamp, 6×i32
// ticks, little-endian) sent over a WebRTC DataChannel or a cloud WS relay
// to follower_bridge.py on the robot, which feeds the production UDP :9999
// teleop input. The bridge echoes every packet back (+8 bytes of robot
// clock), so each link measures live RTT.
//
// The app runs on HTTPS, so anything WebSocket rides the robot proxy's
// same-origin /teleoprelay/* front (signaling is latency-irrelevant; for the
// ws transport the extra robot hop is called out in the selector label).

const MAGIC = 0xaa55;
const PACKET_SIZE = 38;
const RTT_WINDOW = 200;

/**
 * @param {number} seq
 * @param {number[]} ticks six servo positions
 * @returns {ArrayBuffer}
 */
function packControl(seq, ticks) {
  const buf = new ArrayBuffer(PACKET_SIZE);
  const v = new DataView(buf);
  v.setUint16(0, MAGIC, true);
  v.setUint32(2, seq >>> 0, true);
  v.setFloat64(6, Date.now(), true);
  for (let i = 0; i < 6; i++) v.setInt32(14 + 4 * i, ticks[i] | 0, true);
  return buf;
}

/** @param {ArrayBuffer} buf @returns {number | null} seq, or null if not an echo */
function echoSeq(buf) {
  if (buf.byteLength < PACKET_SIZE) return null;
  const v = new DataView(buf);
  if (v.getUint16(0, true) !== MAGIC) return null;
  return v.getUint32(2, true);
}

/** Shared send/RTT bookkeeping behind both link types. */
function makeCore() {
  /** @type {Map<number, number>} */
  const pending = new Map();
  /** @type {number[]} */
  const rtts = [];
  let seq = 0;
  return {
    pending,
    rtts,
    nextPacket(/** @type {number[]} */ ticks) {
      const buf = packControl(seq, ticks);
      pending.set(seq, performance.now());
      if (pending.size > 400) {
        for (const k of pending.keys()) {
          if (pending.size <= 200) break;
          pending.delete(k);
        }
      }
      seq++;
      return buf;
    },
    onEcho(/** @type {ArrayBuffer} */ buf) {
      const s = echoSeq(buf);
      if (s === null) return;
      const t0 = pending.get(s);
      if (t0 === undefined) return;
      pending.delete(s);
      rtts.push(performance.now() - t0);
      if (rtts.length > RTT_WINDOW) rtts.splice(0, RTT_WINDOW / 2);
    },
    /** @returns {number | null} median RTT (ms) over the recent window */
    rttP50() {
      if (!rtts.length) return null;
      const sorted = [...rtts].sort((a, b) => a - b);
      return sorted[Math.floor(sorted.length / 2)];
    },
  };
}

/** @returns {string} wss (or ws on the cleartext listener) same-origin URL */
function relayUrl(/** @type {string} */ path) {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${location.host}/teleoprelay${path}`;
}

/**
 * @typedef {{ start: () => Promise<void>, send: (ticks: number[]) => void,
 *   rttP50: () => number | null, close: () => void }} ControlLink
 */

/**
 * Cloud WS relay: browser -> robot proxy -> relay VM -> robot bridge.
 * @param {string} session
 * @returns {ControlLink}
 */
export function createWsRelayLink(session) {
  const core = makeCore();
  /** @type {WebSocket | null} */
  let ws = null;
  return {
    async start() {
      ws = new WebSocket(relayUrl(`/pair/${session}/operator`));
      ws.binaryType = "arraybuffer";
      await new Promise((resolve, reject) => {
        if (!ws) return reject(new Error("closed"));
        ws.onopen = resolve;
        ws.onerror = () => reject(new Error("relay WS failed"));
      });
      ws.onmessage = (e) => {
        if (e.data instanceof ArrayBuffer) core.onEcho(e.data);
      };
    },
    send(ticks) {
      if (ws?.readyState === WebSocket.OPEN) ws.send(core.nextPacket(ticks));
    },
    rttP50: () => core.rttP50(),
    close() {
      ws?.close();
      ws = null;
    },
  };
}

/**
 * WebRTC DataChannel (unordered, no retransmits — the REMOTE_TELEOP_DESIGN
 * RFC path). Signaling via the relay's /pair/{session}-sig channel through
 * the robot proxy; ICE against the relay VM's TURN. `forceRelay` uses the
 * browser-native iceTransportPolicy to demo the every-packet-through-TURN
 * worst case.
 * @param {string} session
 * @param {string} turnHost relay VM host for turn:
 * @param {boolean} forceRelay
 * @returns {ControlLink}
 */
export function createWebRtcLink(session, turnHost, forceRelay) {
  const core = makeCore();
  /** @type {RTCPeerConnection | null} */
  let pc = null;
  /** @type {RTCDataChannel | null} */
  let dc = null;
  return {
    async start() {
      pc = new RTCPeerConnection({
        iceServers: [
          { urls: "stun:stun.l.google.com:19302" },
          { urls: `turn:${turnHost}:3478`, username: "bench", credential: "benchpw" },
        ],
        iceTransportPolicy: forceRelay ? "relay" : "all",
      });
      dc = pc.createDataChannel("teleop", { ordered: false, maxRetransmits: 0 });
      dc.binaryType = "arraybuffer";
      dc.onmessage = (e) => {
        if (e.data instanceof ArrayBuffer) core.onEcho(e.data);
      };

      const sig = new WebSocket(relayUrl(`/pair/${session}-sig/operator`));
      try {
        await new Promise((resolve, reject) => {
          sig.onopen = resolve;
          sig.onerror = () => reject(new Error("signaling WS failed"));
        });
        // Non-trickle to match the aiortc bridge: gather fully, send one offer.
        await pc.setLocalDescription(await pc.createOffer());
        await new Promise((resolve) => {
          if (pc?.iceGatheringState === "complete") return resolve(undefined);
          const timer = setTimeout(resolve, 3000);
          pc?.addEventListener("icegatheringstatechange", () => {
            if (pc?.iceGatheringState === "complete") {
              clearTimeout(timer);
              resolve(undefined);
            }
          });
        });
        sig.send(JSON.stringify({ type: "offer", sdp: pc.localDescription?.sdp }));
        const answer = await new Promise((resolve, reject) => {
          const timer = setTimeout(() => reject(new Error("no answer from robot bridge")), 15000);
          sig.onmessage = (e) => {
            const msg = JSON.parse(String(e.data));
            if (msg.type === "answer") {
              clearTimeout(timer);
              resolve(msg);
            }
          };
          sig.onclose = () => reject(new Error("signaling closed"));
        });
        await pc.setRemoteDescription({ type: "answer", sdp: answer.sdp });
        await new Promise((resolve, reject) => {
          const timer = setTimeout(() => reject(new Error("DataChannel didn't open (ICE failed?)")), 15000);
          if (!dc) return reject(new Error("closed"));
          dc.onopen = () => {
            clearTimeout(timer);
            resolve(undefined);
          };
        });
      } finally {
        sig.close();
      }
    },
    send(ticks) {
      if (dc?.readyState === "open") dc.send(core.nextPacket(ticks));
    },
    rttP50: () => core.rttP50(),
    close() {
      dc?.close();
      pc?.close();
      dc = null;
      pc = null;
    },
  };
}

/**
 * The control-path menu. `rosbridge` is the existing /leader_positions
 * publish (handled by the arm panel itself, no link object).
 */
export const CONTROL_PATHS = Object.freeze([
  { key: "rosbridge", label: "rosbridge (default)" },
  { key: "webrtc", label: "WebRTC (P2P → TURN)" },
  { key: "webrtc-turn", label: "WebRTC forced TURN" },
  { key: "ws-relay", label: "WS cloud relay (via robot proxy)" },
]);

/**
 * @param {string} key CONTROL_PATHS key (not "rosbridge")
 * @param {string} session
 * @param {string} turnHost
 * @returns {ControlLink}
 */
export function createControlLink(key, session, turnHost) {
  if (key === "webrtc") return createWebRtcLink(session, turnHost, false);
  if (key === "webrtc-turn") return createWebRtcLink(session, turnHost, true);
  if (key === "ws-relay") return createWsRelayLink(session);
  throw new Error(`unknown control path: ${key}`);
}

/** Bridge transport + ice params for /teleop-bridge, per control path. */
export function bridgeParams(/** @type {string} */ key) {
  if (key === "webrtc") return { transport: "webrtc", ice: "all" };
  if (key === "webrtc-turn") return { transport: "webrtc", ice: "relay" };
  if (key === "ws-relay") return { transport: "ws", ice: "all" };
  return null;
}
