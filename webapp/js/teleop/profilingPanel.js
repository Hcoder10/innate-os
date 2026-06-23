// @ts-check
// Profiling panel — a live read-out of the WebRTC receive side, for tuning
// glass-to-glass latency. A discrete pulse button (top-right) toggles it; "p"
// does too. Hidden by default so the cockpit stays clean for operators.
//
// The headline is the jitter buffer delay: the receiver's de-jitter buffer is
// the dominant tunable latency on a clean LAN. RTT, bitrate, loss, and freezes
// give the context to judge whether a tighter buffer is safe.
//
// Rates (bitrate, loss) are deltas between polls; jitter buffer delay is the
// delta of cumulative hold-time over the delta of frames emitted, i.e. the
// average delay of the frames played out since the last poll.

const POLL_MS = 1000;
const TOGGLE_KEY = "KeyP";

/**
 * @typedef {object} Sample
 * @property {number} t           performance.now() at capture (ms)
 * @property {number} jbDelay     jitterBufferDelay, cumulative seconds
 * @property {number} jbEmitted   jitterBufferEmittedCount, cumulative frames
 * @property {number} bytes       bytesReceived, cumulative
 * @property {number} packets     packetsReceived, cumulative
 * @property {number} lost        packetsLost, cumulative
 */

/**
 * @param {HTMLElement} parent cockpit root — owns the toggle button + panel.
 * @param {import("../webrtcSession.js").WebRtcSession} session
 * @returns {{ destroy: () => void }}
 */
export function createProfilingPanel(parent, session) {
  // Discrete toggle: a small glass pulse button, top-right.
  const toggleBtn = document.createElement("button");
  toggleBtn.type = "button";
  toggleBtn.className = "profiling-toggle";
  toggleBtn.title = "Profiling (p)";
  toggleBtn.setAttribute("aria-label", "Toggle profiling panel");
  toggleBtn.innerHTML =
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M3 12h4l2 6 4-14 2 8h6"/></svg>';

  // The panel itself — its own glass box, sits just under the button.
  const wrap = document.createElement("div");
  wrap.className = "profiling";
  wrap.hidden = true;

  const head = document.createElement("div");
  head.className = "profiling-head";
  const title = document.createElement("span");
  title.className = "microlabel";
  title.textContent = "profiling";
  const hint = document.createElement("span");
  hint.className = "profiling-hint mono";
  hint.textContent = "p";
  head.append(title, hint);

  const grid = document.createElement("div");
  grid.className = "profiling-grid";

  const jitterBuffer = metric("jitter buf", "ms", true);
  const rtt = metric("rtt", "ms");
  const jitter = metric("jitter", "ms");
  const fps = metric("fps", "");
  const resolution = metric("res", "");
  const bitrate = metric("bitrate", "kb/s");
  const loss = metric("loss", "%");
  const dropped = metric("dropped", "");
  const freezes = metric("freezes", "");
  const all = [jitterBuffer, rtt, jitter, fps, resolution, bitrate, loss, dropped, freezes];
  for (const m of all) grid.append(m.el);

  wrap.append(head, grid);
  parent.append(toggleBtn, wrap);

  /** @type {Sample | null} */
  let prev = null;
  /** @type {number | null} */
  let timer = null;

  /**
   * @param {string} labelText
   * @param {string} unit
   * @param {boolean} [headline]
   */
  function metric(labelText, unit, headline = false) {
    const el = document.createElement("div");
    el.className = headline ? "profiling-item headline" : "profiling-item";
    const label = document.createElement("span");
    label.className = "microlabel";
    label.textContent = labelText;
    const value = document.createElement("span");
    value.className = "profiling-value mono";
    value.textContent = "—";
    const unitEl = document.createElement("span");
    unitEl.className = "profiling-unit";
    unitEl.textContent = unit;
    const row = document.createElement("div");
    row.className = "profiling-row";
    row.append(value, unitEl);
    el.append(label, row);
    return { el, value };
  }

  /**
   * @param {number | undefined | null} n
   * @param {number} [digits]
   */
  function fmt(n, digits = 0) {
    return typeof n === "number" && Number.isFinite(n) ? n.toFixed(digits) : "—";
  }

  function clearValues() {
    for (const m of all) m.value.textContent = "—";
    prev = null;
  }

  async function poll() {
    /** @type {RTCStatsReport | null} */
    let report;
    try {
      report = await session.getStats();
    } catch {
      report = null;
    }
    if (!report) {
      clearValues();
      return;
    }

    /** @type {any} */ let vid = null;
    /** @type {any} */ let selectedPair = null;
    /** @type {string | undefined} */ let selectedPairId;

    report.forEach((/** @type {any} */ s) => {
      if (s.type === "inbound-rtp" && s.kind === "video") vid = s;
      else if (s.type === "transport" && s.selectedCandidatePairId) selectedPairId = s.selectedCandidatePairId;
    });
    report.forEach((/** @type {any} */ s) => {
      if (s.type !== "candidate-pair") return;
      if (s.id === selectedPairId) selectedPair = s;
      else if (!selectedPair && (s.nominated || s.selected)) selectedPair = s;
    });

    if (!vid) {
      clearValues();
      return;
    }

    rtt.value.textContent = fmt(selectedPair?.currentRoundTripTime * 1000, 1);
    jitter.value.textContent = fmt(vid.jitter * 1000, 1);
    fps.value.textContent = fmt(vid.framesPerSecond, 0);
    resolution.value.textContent =
      vid.frameWidth && vid.frameHeight ? `${vid.frameWidth}×${vid.frameHeight}` : "—";
    dropped.value.textContent = fmt(vid.framesDropped, 0);
    freezes.value.textContent = fmt(vid.freezeCount, 0);

    /** @type {Sample} */
    const now = {
      t: performance.now(),
      jbDelay: vid.jitterBufferDelay ?? 0,
      jbEmitted: vid.jitterBufferEmittedCount ?? 0,
      bytes: vid.bytesReceived ?? 0,
      packets: vid.packetsReceived ?? 0,
      lost: vid.packetsLost ?? 0,
    };

    if (prev) {
      const dt = (now.t - prev.t) / 1000;
      const dEmitted = now.jbEmitted - prev.jbEmitted;
      const dDelay = now.jbDelay - prev.jbDelay;
      jitterBuffer.value.textContent = dEmitted > 0 ? fmt((dDelay / dEmitted) * 1000, 0) : "—";

      if (dt > 0) bitrate.value.textContent = fmt(((now.bytes - prev.bytes) * 8) / dt / 1000, 0);

      const dRecv = now.packets - prev.packets;
      const dLost = now.lost - prev.lost;
      const denom = dRecv + dLost;
      loss.value.textContent = denom > 0 ? fmt((dLost / denom) * 100, 1) : "0.0";
    } else if (now.jbEmitted > 0) {
      // First poll: show the session-cumulative average so the panel isn't blank.
      jitterBuffer.value.textContent = fmt((now.jbDelay / now.jbEmitted) * 1000, 0);
    }
    prev = now;
  }

  function start() {
    if (timer !== null) return;
    void poll();
    timer = window.setInterval(() => void poll(), POLL_MS);
  }

  function stop() {
    if (timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  }

  /** @param {boolean} open */
  function setOpen(open) {
    wrap.hidden = !open;
    toggleBtn.classList.toggle("active", open);
    toggleBtn.setAttribute("aria-pressed", String(open));
    if (open) {
      start();
    } else {
      stop();
      clearValues();
    }
  }

  function toggle() {
    setOpen(Boolean(wrap.hidden));
  }

  toggleBtn.addEventListener("click", toggle);

  /** @param {KeyboardEvent} e */
  function onKeyDown(e) {
    if (e.code !== TOGGLE_KEY || e.repeat || e.metaKey || e.ctrlKey || e.altKey) return;
    const el = document.activeElement;
    if (el instanceof HTMLElement && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable)) {
      return;
    }
    toggle();
  }

  window.addEventListener("keydown", onKeyDown);

  return {
    destroy() {
      stop();
      window.removeEventListener("keydown", onKeyDown);
      toggleBtn.remove();
      wrap.remove();
    },
  };
}
