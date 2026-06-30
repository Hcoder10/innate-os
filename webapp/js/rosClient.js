// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// RosClient — the single rosbridge (rws) WebSocket shared by the whole page.
// Ported from the mobile app's useWebSocketManager: pending service-call map
// with timeouts, per-topic handler registry, subscribe retry with backoff on
// rws "Topic <t> not found" errors, and a publish-zero-on-hide drive safety.
// Web-specific additions: automatic reconnect with backoff after an
// unexpected close (the socket replays every active subscription on reopen,
// so consumers never notice), and localStorage for the last-connected IP.

import { JOYSTICK_TOPIC, LAST_IP_KEY, ROSBRIDGE_PORT } from "./constants.js";

const SERVICE_TIMEOUT_MS = 10_000;
const CONNECT_TIMEOUT_MS = 8_000;
const RECONNECT_BASE_MS = 1_000;
const RECONNECT_CAP_MS = 10_000;
const SUB_RETRY_CAP_MS = 30_000;

/**
 * @typedef {Object} Subscription
 * @property {Set<(msg: any) => void>} handlers
 * @property {number | undefined} throttleRate
 * @property {number} retryCount
 * @property {number | null} retryTimer
 */

export class RosClient {
  /** @type {WebSocket | null} */ #ws = null;
  /** @type {ConnState} */ #state = "disconnected";
  /** @type {string | null} */ #ip = null;
  /** @type {Set<(state: ConnState, ip: string | null) => void>} */ #stateListeners = new Set();
  /** @type {Map<string, Subscription>} */ #subs = new Map();
  /** @type {Map<string, { resolve: (v: any) => void, reject: (e: Error) => void, timer: number }>} */ #pendingCalls = new Map();
  #callCounter = 0;
  #reconnectDelay = RECONNECT_BASE_MS;
  /** @type {number | null} */ #reconnectTimer = null;
  /** @type {number | null} */ #connectTimer = null;
  /** True once the current target IP has had a successful open; gates the
   * reconnect-forever loop so a typo'd address fails fast instead. */
  #everConnected = false;

  constructor() {
    // Safety net: a hidden or closing page must never leave the robot
    // driving on a latched vector. DriveController halts its own state on the
    // same signals; this raw zero goes out even if that module is broken.
    const publishZero = () => {
      if (this.#state === "connected") {
        this.publish(JOYSTICK_TOPIC, { x: 0, y: 0 });
      }
    };
    window.addEventListener("pagehide", publishZero);
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") publishZero();
    });
  }

  /** @returns {ConnState} */
  get state() {
    return this.#state;
  }

  /** @returns {string | null} */
  get ip() {
    return this.#ip;
  }

  /** Last successfully connected address, or null. */
  get lastIp() {
    return localStorage.getItem(LAST_IP_KEY);
  }

  /**
   * Subscribe to connection-state changes. The callback fires immediately
   * with the current state, then on every transition.
   * @param {(state: ConnState, ip: string | null) => void} cb
   * @returns {() => void} unsubscribe
   */
  onStateChange(cb) {
    this.#stateListeners.add(cb);
    cb(this.#state, this.#ip);
    return () => this.#stateListeners.delete(cb);
  }

  /** @param {string} ip Hostname or IP of the robot (port is fixed). */
  connect(ip) {
    if (!ip) return;
    if (this.#ws && this.#ip === ip && (this.#state === "connected" || this.#state === "connecting")) {
      return;
    }
    this.#clearReconnect();
    this.#teardownSocket();
    this.#ip = ip;
    this.#everConnected = false;
    this.#open("connecting");
  }

  /** Intentional close: no reconnect, pending calls rejected. */
  disconnect() {
    this.#clearReconnect();
    this.#teardownSocket();
    this.#rejectAllPending(new Error("Disconnected"));
    this.#everConnected = false;
    this.#setState("disconnected");
  }

  /**
   * Fire-and-forget publish. Dropped (not queued) when the socket is down —
   * callers that care observe onStateChange.
   * @param {string} topic
   * @param {object} msg
   */
  publish(topic, msg) {
    this.#send({ op: "publish", topic, msg });
  }

  /**
   * @param {string} service
   * @param {object} [args]
   * @param {number} [timeoutMs]
   * @returns {Promise<any>} resolves with the response `values`
   */
  callService(service, args = {}, timeoutMs = SERVICE_TIMEOUT_MS) {
    return new Promise((resolve, reject) => {
      if (this.#state !== "connected" || !this.#ws) {
        reject(new Error(`Not connected; cannot call ${service}`));
        return;
      }
      const id = `call_${this.#callCounter++}_${service.replace(/[^a-zA-Z0-9]/g, "_")}`;
      const timer = setTimeout(() => {
        if (this.#pendingCalls.delete(id)) {
          reject(new Error(`Service call ${service} timed out after ${timeoutMs}ms`));
        }
      }, timeoutMs);
      this.#pendingCalls.set(id, { resolve, reject, timer });
      this.#send({ op: "call_service", id, service, args });
    });
  }

  /**
   * Ref-counted subscribe. The first handler for a topic sends the subscribe
   * op; the last one leaving sends unsubscribe. Active topics are replayed
   * automatically after every reconnect, and retried with backoff when rws
   * reports the publisher doesn't exist yet.
   * @param {string} topic
   * @param {(msg: any) => void} handler Receives the message payload
   *   (`frame.msg`, falling back to the whole frame for rws variants).
   * @param {number} [throttleRate] Server-side throttle in ms.
   * @returns {() => void} unsubscribe
   */
  subscribe(topic, handler, throttleRate) {
    let sub = this.#subs.get(topic);
    if (!sub) {
      sub = { handlers: new Set(), throttleRate, retryCount: 0, retryTimer: null };
      this.#subs.set(topic, sub);
      if (this.#state === "connected") this.#sendSubscribe(topic, sub);
    }
    sub.handlers.add(handler);
    return () => {
      const current = this.#subs.get(topic);
      if (!current || !current.handlers.delete(handler)) return;
      if (current.handlers.size === 0) {
        if (current.retryTimer !== null) clearTimeout(current.retryTimer);
        this.#subs.delete(topic);
        if (this.#state === "connected") this.#send({ op: "unsubscribe", topic });
      }
    };
  }

  // ---- internals ----------------------------------------------------------

  /**
   * Through the robot's HTTPS front door, rosbridge rides the same-origin
   * wss proxy (plain ws:// would be blocked as mixed content). Everywhere
   * else, talk to rws directly.
   * @param {string} ip
   */
  #urlFor(ip) {
    if (location.protocol === "https:" && ip === location.hostname) {
      return `wss://${location.host}/ws`;
    }
    return `ws://${ip}:${ROSBRIDGE_PORT}`;
  }

  /** @param {"connecting" | "reconnecting"} phase */
  #open(phase) {
    const ip = this.#ip;
    if (!ip) return;
    this.#setState(phase);
    let ws;
    try {
      ws = new WebSocket(this.#urlFor(ip));
    } catch (err) {
      console.error("[ros] WebSocket constructor failed:", err);
      this.#handleClose();
      return;
    }
    this.#ws = ws;

    // Browsers can sit in CONNECTING for the full OS TCP timeout; cut an
    // unreachable address short so the UI can report failure quickly.
    this.#connectTimer = setTimeout(() => {
      if (ws.readyState === WebSocket.CONNECTING) ws.close();
    }, CONNECT_TIMEOUT_MS);

    ws.onopen = () => {
      if (this.#ws !== ws) return;
      this.#clearConnectTimer();
      this.#reconnectDelay = RECONNECT_BASE_MS;
      this.#everConnected = true;
      try {
        localStorage.setItem(LAST_IP_KEY, ip);
      } catch {
        // Storage can be unavailable (private mode); connection still works.
      }
      this.#setState("connected");
      for (const [topic, sub] of this.#subs) {
        sub.retryCount = 0;
        this.#sendSubscribe(topic, sub);
      }
    };

    ws.onmessage = (event) => {
      if (this.#ws !== ws) return;
      this.#handleFrame(String(event.data));
    };

    ws.onclose = () => {
      if (this.#ws !== ws) return;
      this.#handleClose();
    };

    ws.onerror = () => {
      // onclose always follows; nothing useful in the error event itself.
    };
  }

  #handleClose() {
    this.#clearConnectTimer();
    this.#ws = null;
    this.#rejectAllPending(new Error("Connection closed"));
    for (const sub of this.#subs.values()) {
      if (sub.retryTimer !== null) {
        clearTimeout(sub.retryTimer);
        sub.retryTimer = null;
      }
      sub.retryCount = 0;
    }
    if (!this.#everConnected) {
      // First attempt to this address never opened: fail fast for the
      // connect panel instead of silently retrying a typo forever.
      this.#setState("disconnected");
      return;
    }
    this.#setState("reconnecting");
    const delay = this.#reconnectDelay;
    this.#reconnectDelay = Math.min(this.#reconnectDelay * 2, RECONNECT_CAP_MS);
    this.#reconnectTimer = setTimeout(() => {
      this.#reconnectTimer = null;
      if (this.#state === "reconnecting") this.#open("reconnecting");
    }, delay);
  }

  /** @param {string} raw */
  #handleFrame(raw) {
    /** @type {any} */
    let data;
    try {
      data = JSON.parse(raw);
    } catch {
      return;
    }

    if (data.op === "service_response") {
      const pending = data.id ? this.#pendingCalls.get(data.id) : undefined;
      if (!pending) return;
      this.#pendingCalls.delete(data.id);
      clearTimeout(pending.timer);
      if (data.result === false) {
        pending.reject(new Error(typeof data.values === "string" ? data.values : "Service call failed"));
      } else {
        pending.resolve(data.values);
      }
      return;
    }

    if (typeof data.topic === "string") {
      const sub = this.#subs.get(data.topic);
      if (!sub) return;
      sub.retryCount = 0;
      // rws payload quirk: the message normally rides in `frame.msg`, but
      // some paths deliver fields on the frame itself — pass the frame
      // through so `payload.data` keeps working either way.
      const payload = data.msg ?? data;
      for (const handler of sub.handlers) {
        try {
          handler(payload);
        } catch (err) {
          console.error(`[ros] handler for ${data.topic} threw:`, err);
        }
      }
      return;
    }

    if (data.op === "status" && data.level === "error") {
      const match = typeof data.msg === "string" && data.msg.match(/^Topic (.+?) not found/);
      if (match && this.#subs.has(match[1])) {
        this.#scheduleSubRetry(match[1]);
      } else {
        console.error("[ros] rosbridge error:", data.msg);
      }
    }
  }

  /**
   * Publisher didn't exist yet (subscribe raced node startup): retry with
   * exponential backoff, capped, until the topic appears or we unsubscribe.
   * @param {string} topic
   */
  #scheduleSubRetry(topic) {
    const sub = this.#subs.get(topic);
    if (!sub) return;
    if (sub.retryTimer !== null) clearTimeout(sub.retryTimer);
    const delay = Math.min(1000 * 2 ** sub.retryCount, SUB_RETRY_CAP_MS);
    sub.retryCount += 1;
    sub.retryTimer = setTimeout(() => {
      sub.retryTimer = null;
      if (this.#subs.has(topic) && this.#state === "connected") {
        this.#sendSubscribe(topic, sub);
      }
    }, delay);
  }

  /**
   * @param {string} topic
   * @param {Subscription} sub
   */
  #sendSubscribe(topic, sub) {
    this.#send({
      op: "subscribe",
      topic,
      ...(sub.throttleRate ? { throttle_rate: sub.throttleRate } : {}),
    });
  }

  /** @param {object} payload */
  #send(payload) {
    if (this.#ws?.readyState === WebSocket.OPEN) {
      this.#ws.send(JSON.stringify(payload));
    }
  }

  /** @param {Error} err */
  #rejectAllPending(err) {
    for (const pending of this.#pendingCalls.values()) {
      clearTimeout(pending.timer);
      pending.reject(err);
    }
    this.#pendingCalls.clear();
  }

  #teardownSocket() {
    this.#clearConnectTimer();
    const ws = this.#ws;
    this.#ws = null;
    if (ws) {
      ws.onopen = null;
      ws.onmessage = null;
      ws.onclose = null;
      ws.onerror = null;
      try {
        ws.close();
      } catch {
        // Already closed.
      }
    }
  }

  #clearReconnect() {
    if (this.#reconnectTimer !== null) {
      clearTimeout(this.#reconnectTimer);
      this.#reconnectTimer = null;
    }
    this.#reconnectDelay = RECONNECT_BASE_MS;
  }

  #clearConnectTimer() {
    if (this.#connectTimer !== null) {
      clearTimeout(this.#connectTimer);
      this.#connectTimer = null;
    }
  }

  /** @param {ConnState} state */
  #setState(state) {
    if (state === this.#state) return;
    this.#state = state;
    // Snapshot: a listener registered during this emission already saw the
    // current state via onStateChange's immediate fire — visiting it here
    // too would double-deliver (Sets iterate items added mid-loop).
    for (const cb of [...this.#stateListeners]) {
      try {
        cb(state, this.#ip);
      } catch (err) {
        console.error("[ros] state listener threw:", err);
      }
    }
  }
}

/** The one client every module shares. */
export const ros = new RosClient();
