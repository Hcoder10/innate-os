// @ts-check

import { isTypingContext } from "../shell.js";

const RIPPLE_COUNT = 3;
const WAVEFORM_BAR_COUNT = 7;
const SHORT_CLICK_MS = 350;
const HOLD_HINT_DURATION_MS = 4200;
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
 *   setCaptureState: (state: { on: boolean, busy: boolean, error: string | null }) => void,
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
  let captureOn = false;
  let captureBusy = false;
  let isViewEnabled = true;
  /** @type {string | null} */
  let unavailableReason = null;
  let activationId = 0;
  let pointerDownAt = 0;
  let holdHintVisible = false;
  let holdHintTimeout = 0;

  function renderHoldState() {
    const unavailable = unavailableReason !== null;
    const listening = isHeld && captureOn && !unavailable;
    const waiting = isHeld && (captureBusy || !captureOn) && !unavailable;
    const showingHoldHint = holdHintVisible && !unavailable && !waiting && !listening;
    control.classList.toggle("listening", listening);
    control.classList.toggle("waiting", waiting);
    control.classList.toggle("unavailable", unavailable);
    control.classList.toggle("show-hold-hint", showingHoldHint);
    button.setAttribute("aria-pressed", String(isHeld));
    button.setAttribute("aria-busy", String(waiting));
    button.setAttribute("aria-label", unavailable ? "Microphone disabled" : "Hold to talk to the agent");
    button.title = unavailableReason ?? "Hold to talk — Space, or click and hold";
    label.textContent = unavailable
      ? "Mic disabled"
      : waiting
        ? "Starting…"
        : listening
          ? "Listening…"
          : showingHoldHint
            ? "Hold down your space key or mouse to talk"
            : "Hold to talk";
  }

  function dismissHoldHint() {
    window.clearTimeout(holdHintTimeout);
    holdHintVisible = false;
  }

  function showHoldHint() {
    dismissHoldHint();
    holdHintVisible = true;
    holdHintTimeout = window.setTimeout(() => {
      holdHintVisible = false;
      renderHoldState();
    }, HOLD_HINT_DURATION_MS);
    renderHoldState();
  }

  function applyAvailability() {
    const wasDisabled = button.disabled;
    button.disabled = !isViewEnabled || unavailableReason !== null;
    if (button.disabled && !wasDisabled) releaseAllHolds();
    renderHoldState();
  }

  async function beginHold() {
    if (button.disabled || isHeld) return;
    isHeld = true;
    const currentActivationId = ++activationId;
    renderHoldState();
    try {
      await startListening();
      // release may land while permission or agent startup is pending
      if (currentActivationId !== activationId && !isHeld) stopListening();
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
    pointerDownAt = 0;
    holdSources.clear();
    syncHoldState();
  }

  /** @param {PointerEvent} event */
  function onPointerDown(event) {
    if (event.button !== 0) return;
    event.preventDefault();
    dismissHoldHint();
    pointerDownAt = event.timeStamp;
    button.setPointerCapture(event.pointerId);
    holdSources.add("pointer");
    syncHoldState();
  }

  /** @param {PointerEvent} event */
  function onPointerUp(event) {
    const wasShortClick = pointerDownAt > 0 && event.timeStamp - pointerDownAt < SHORT_CLICK_MS;
    pointerDownAt = 0;
    holdSources.delete("pointer");
    syncHoldState();
    if (wasShortClick && !button.disabled) showHoldHint();
  }

  function onPointerCancel() {
    pointerDownAt = 0;
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
  button.addEventListener("pointerup", onPointerUp, listenerOptions);
  button.addEventListener("pointercancel", onPointerCancel, listenerOptions);
  button.addEventListener("lostpointercapture", onPointerCancel, listenerOptions);
  window.addEventListener("keydown", onKeyDown, listenerOptions);
  window.addEventListener("keyup", onKeyUp, listenerOptions);
  window.addEventListener("blur", releaseAllHolds, listenerOptions);
  document.addEventListener("visibilitychange", onVisibilityChange, listenerOptions);
  document.addEventListener("focusin", onFocusIn, listenerOptions);

  return {
    setEnabled(enabled) {
      if (isViewEnabled === enabled) return;
      isViewEnabled = enabled;
      applyAvailability();
    },
    setCaptureState({ on, busy, error }) {
      if (captureOn === on && captureBusy === busy && unavailableReason === error) return;
      captureOn = on;
      captureBusy = busy;
      unavailableReason = error;
      applyAvailability();
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
      dismissHoldHint();
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
