// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Press-to-activate: make buttons fire on press-down instead of on release.
//
// A native `click` only fires on pointer *up* — press, then release over the
// same element. This makes the whole webapp feel snappier by activating a
// button the instant it's pressed: on a qualifying pointerdown we dispatch the
// button's `click` immediately, then swallow the real click that the browser
// still delivers on release, so each handler runs exactly once.
//
// Scope and safety:
//   - `<button>` only. Nav links (full page loads), form controls, and the
//     drag surfaces (joystick, map, head tilt) keep native behavior — press-
//     activating those would misfire on scrolls, drags, and accidental taps.
//   - Mouse and pen only. Touch keeps native click: on a touchscreen a press
//     that turns into a scroll would otherwise fire the button under the finger.
//   - A small KEEP_ON_RELEASE set stays on release — the few actions where an
//     accidental press is costly (see the list below). Any button can opt out
//     with `data-activate="release"` or the `on-release` class.
//   - Keyboard activation (Enter/Space) has no pointerdown, so it is untouched —
//     and its clicks carry detail === 0, so the release swallow lets them pass
//     even while a pointer press is being held on the same button.

// Buttons that must NOT press-activate. Kept deliberately short — most
// destructive actions (delete, reboot, restart, cancel-run, discard) already
// sit behind a confirm() dialog, so press-activating them only opens that
// dialog sooner. These are the ones worth an extra beat before they commit:
const KEEP_ON_RELEASE = [
  '[data-activate="release"]', // generic escape hatch
  ".on-release", // generic escape hatch
  ".modal-start", // "Start training run" — launches a paid cloud run, no confirm
  ".set-reset-all", // "Reset all to defaults" — wipes every override, no confirm
  // Stop / cancel — deliberate mouse-up actions, per product decision.
  ".agent-indicator-stop",
  ".record-stop",
  ".record-wizard-cancel",
  ".run-cancel",
  ".skill-confirm.stop",
].join(",");

/** Install the press-to-activate handler once for the page. */
export function installPressActivate() {
  if (/** @type {any} */ (window).__pressActivateInstalled) return;
  /** @type {any} */ (window).__pressActivateInstalled = true;
  document.addEventListener("pointerdown", onPointerDown, true);
}

/** @param {PointerEvent} e */
function onPointerDown(e) {
  if (e.button !== 0) return; // primary button only
  if (e.pointerType === "touch") return; // touch stays native (scroll safety)

  const target = /** @type {Element | null} */ (e.target);
  const btn = target?.closest?.("button");
  if (!btn || !(btn instanceof HTMLButtonElement)) return;
  if (btn.disabled || btn.getAttribute("aria-disabled") === "true") return;
  if (btn.matches(KEEP_ON_RELEASE)) return;

  // Fire the click now, carrying the pointer's modifiers/coords so handlers
  // that read them behave as if the release had happened here. Dispatch first,
  // then arm the swallow — so our own synthetic click runs the handlers and
  // only the browser's later release click is suppressed.
  btn.dispatchEvent(
    new MouseEvent("click", {
      bubbles: true,
      cancelable: true,
      view: window,
      detail: 1,
      clientX: e.clientX,
      clientY: e.clientY,
      screenX: e.screenX,
      screenY: e.screenY,
      ctrlKey: e.ctrlKey,
      shiftKey: e.shiftKey,
      altKey: e.altKey,
      metaKey: e.metaKey,
    }),
  );
  swallowNextClick(btn);
}

/**
 * Suppress the click the browser will deliver if this press is released over
 * the button, so the handler that already ran on press doesn't run twice.
 *
 * Keyboard-generated clicks (Enter/Space) carry detail === 0 while pointer
 * clicks carry detail >= 1, so a keyboard activation landing inside the
 * swallow window (pointer still held) passes through untouched. Disarming
 * follows the actual release rather than a fixed timer: the native click (if
 * any) fires between pointerup and the macrotask queued from it, so a press
 * held for any duration still swallows exactly its own release click, and a
 * release elsewhere (or a cancelled pointer) cleans up without eating anything.
 * @param {HTMLButtonElement} btn
 */
function swallowNextClick(btn) {
  /** @param {Event} ev */
  const onClick = (ev) => {
    if (/** @type {MouseEvent} */ (ev).detail === 0) return; // keyboard — not ours to swallow
    ev.stopImmediatePropagation();
    ev.preventDefault();
    cleanup();
  };
  const onRelease = () => setTimeout(cleanup, 0);
  function cleanup() {
    btn.removeEventListener("click", onClick, true);
    document.removeEventListener("pointerup", onRelease, true);
    document.removeEventListener("pointercancel", onRelease, true);
  }
  btn.addEventListener("click", onClick, true);
  document.addEventListener("pointerup", onRelease, { capture: true, once: true });
  document.addEventListener("pointercancel", onRelease, { capture: true, once: true });
}
