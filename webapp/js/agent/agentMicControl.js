// @ts-check

import { isTypingContext } from "../shell.js";

const RIPPLE_COUNT = 3;
const WAVEFORM_BAR_COUNT = 7;
/** Mic RMS is quiet; scale it so the resting glass glow reads as activity. */
const LEVEL_GAIN = 6;
/** Band RMS is quieter still; scale so waveform bars fill the puck. */
const WAVEFORM_GAIN = 10;
/** Idle bar height so the waveform never collapses to a flat line. */
const WAVEFORM_FLOOR = 0.12;

/**
 * @param {HTMLElement} root
 * @param {{
 *   startListening: () => void | Promise<void>,
 *   stopListening: () => void
 * }} callbacks
 * @returns {{
 *   destroy: () => void,
 *   setEnabled: (enabled: boolean) => void,
 *   setAudioFeedback: (feedback: { level: number, waveform: number[] }) => void
 * }}
 */
export function createAgentMicControl(root, callbacks) {
  const { startListening, stopListening } = callbacks;

  const control = document.createElement("div");
  control.className = "agent-mic-control";

  const button = document.createElement("button");
  button.type = "button";
  button.className = "agent-mic-button";
  button.setAttribute("aria-label", "Hold to talk to the agent");
  button.setAttribute("aria-pressed", "false");
  button.title = "Hold to talk — Space, or click and hold";

  const icon = decorativeSpan("agent-mic-icon");

  const waveform = decorativeSpan("agent-mic-waveform");
  const waveformBars = Array.from({ length: WAVEFORM_BAR_COUNT }, () =>
    decorativeSpan("agent-mic-waveform-bar"),
  );
  waveform.append(...waveformBars);

  const innerWaves = rippleGroup("agent-mic-inner-waves", "agent-mic-inner-wave");
  const hoverGlow = decorativeSpan("agent-mic-hover-glow");
  const outerWaves = rippleGroup("agent-mic-outer-waves", "agent-mic-outer-wave");

  const label = document.createElement("span");
  label.className = "agent-mic-label";
  label.textContent = "Hold to talk";

  button.append(innerWaves, hoverGlow, icon, waveform);
  control.append(outerWaves, button, label);
  root.append(control);

  /** @type {Set<"pointer" | "space">} */
  const holdSources = new Set();
  const eventController = new AbortController();
  let isHeld = false;
  let activationId = 0;

  function renderHoldState() {
    control.classList.toggle("listening", isHeld);
    button.setAttribute("aria-pressed", String(isHeld));
    label.textContent = isHeld ? "Listening…" : "Hold to talk";
  }

  async function beginHold() {
    if (button.disabled || isHeld) return;
    isHeld = true;
    const currentActivationId = ++activationId;
    renderHoldState();
    try {
      await startListening();
      // release may land while permission or agent startup is pending
      if (currentActivationId !== activationId) stopListening();
    } catch {
      if (currentActivationId !== activationId) return;
      holdSources.clear();
      isHeld = false;
      renderHoldState();
    }
  }

  function endHold() {
    if (!isHeld) return;
    isHeld = false;
    activationId++;
    renderHoldState();
    stopListening();
  }

  function syncHoldState() {
    if (holdSources.size > 0) void beginHold();
    else endHold();
  }

  function releaseAllHolds() {
    holdSources.clear();
    syncHoldState();
  }

  /** @param {PointerEvent} event */
  function onPointerDown(event) {
    if (event.button !== 0) return;
    event.preventDefault();
    button.setPointerCapture(event.pointerId);
    holdSources.add("pointer");
    syncHoldState();
  }

  function onPointerRelease() {
    holdSources.delete("pointer");
    syncHoldState();
  }

  /** @param {KeyboardEvent} event */
  function onKeyDown(event) {
    if (
      button.disabled ||
      event.code !== "Space" ||
      event.repeat ||
      event.metaKey ||
      event.ctrlKey ||
      event.altKey ||
      event.shiftKey ||
      isTypingContext()
    ) {
      return;
    }
    event.preventDefault();
    holdSources.add("space");
    syncHoldState();
  }

  /** @param {KeyboardEvent} event */
  function onKeyUp(event) {
    if (event.code !== "Space" || !holdSources.has("space")) return;
    event.preventDefault();
    holdSources.delete("space");
    syncHoldState();
  }

  function onVisibilityChange() {
    if (document.visibilityState === "hidden") releaseAllHolds();
  }

  function onFocusIn() {
    if (isTypingContext() && holdSources.delete("space")) {
      syncHoldState();
    }
  }

  const listenerOptions = { signal: eventController.signal };
  button.addEventListener("pointerdown", onPointerDown, listenerOptions);
  button.addEventListener("pointerup", onPointerRelease, listenerOptions);
  button.addEventListener("pointercancel", onPointerRelease, listenerOptions);
  button.addEventListener("lostpointercapture", onPointerRelease, listenerOptions);
  window.addEventListener("keydown", onKeyDown, listenerOptions);
  window.addEventListener("keyup", onKeyUp, listenerOptions);
  window.addEventListener("blur", releaseAllHolds, listenerOptions);
  document.addEventListener("visibilitychange", onVisibilityChange, listenerOptions);
  document.addEventListener("focusin", onFocusIn, listenerOptions);

  return {
    setEnabled(enabled) {
      const disabled = !enabled;
      if (button.disabled === disabled) return;
      button.disabled = disabled;
      if (disabled) releaseAllHolds();
    },
    /** @param {{ level: number, waveform: number[] }} feedback */
    setAudioFeedback({ level, waveform }) {
      control.style.setProperty("--agent-mic-level", String(clamp(level * LEVEL_GAIN)));
      waveformBars.forEach((bar, index) => {
        const amplitude = clamp((waveform[index] ?? 0) * WAVEFORM_GAIN, WAVEFORM_FLOOR);
        bar.style.setProperty("--agent-wave", String(amplitude));
      });
    },
    destroy() {
      releaseAllHolds();
      eventController.abort();
      control.remove();
    },
  };
}

/** @param {string} className */
function decorativeSpan(className) {
  const element = document.createElement("span");
  element.className = className;
  element.setAttribute("aria-hidden", "true");
  return element;
}

/** @param {string} groupClass @param {string} rippleClass */
function rippleGroup(groupClass, rippleClass) {
  const group = decorativeSpan(groupClass);
  const ripples = Array.from({ length: RIPPLE_COUNT }, () => decorativeSpan(rippleClass));
  group.append(...ripples);
  return group;
}

/** @param {number} value @param {number} [min] @returns {number} */
function clamp(value, min = 0) {
  return Math.max(min, Math.min(1, value));
}
