// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Clipboard helpers that also work on insecure origins. The webapp is usually
// browsed over plain HTTP (http://<robot-ip>), where navigator.clipboard is
// undefined, so we fall back to the legacy textarea + execCommand path there.
// The async path can also fail on HTTPS (document unfocused, permission
// denied, writeText missing in old WebViews) — those fall through to the
// legacy path too instead of surfacing the original error.

export const ICON_COPY =
  '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
const ICON_CHECK =
  '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg>';
const ICON_X =
  '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';

/**
 * Copy text to the clipboard. Resolves on success, rejects on failure.
 * @param {string} text
 * @returns {Promise<void>}
 */
export async function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    try {
      return await navigator.clipboard.writeText(text);
    } catch {
      // NotAllowedError (document unfocused / permission denied) — try legacy.
    }
  }
  legacyCopy(text);
}

/**
 * Legacy hidden-textarea + execCommand("copy") path (deprecated but the only
 * option on insecure origins). Synchronous; throws on failure.
 * @param {string} text
 */
function legacyCopy(text) {
  const prevFocus = /** @type {HTMLElement | null} */ (document.activeElement);
  const sel = document.getSelection();
  const ranges = sel ? Array.from({ length: sel.rangeCount }, (_, i) => sel.getRangeAt(i)) : [];

  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", ""); // no soft-keyboard popup on touch devices
  // Pin inside the viewport: with auto offsets a fixed element lands at its
  // static flow position, which on a tall page can be off-screen — and some
  // browsers refuse to copy from a non-rendered element.
  ta.style.position = "fixed";
  ta.style.top = "0";
  ta.style.left = "0";
  ta.style.width = "1px";
  ta.style.height = "1px";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  ta.setSelectionRange(0, text.length); // iOS Safari ignores select()
  try {
    if (!document.execCommand("copy")) throw new Error("execCommand copy failed");
  } finally {
    ta.remove();
    // Give back what select() stole: the user's text selection and focus.
    if (sel) {
      sel.removeAllRanges();
      for (const r of ranges) sel.addRange(r);
    }
    if (prevFocus) prevFocus.focus();
  }
}

/**
 * Copy `text` and flash feedback on an icon copy-button: a check on success
 * (with `activeClass` while shown), an ✕ on failure, then restore ICON_COPY.
 * Shared by the log views; never rejects.
 * @param {string} text
 * @param {HTMLElement} btn Button whose idle icon is ICON_COPY.
 * @param {string} activeClass CSS class toggled on the button during the flash.
 * @returns {Promise<void>}
 */
export function copyToButton(text, btn, activeClass) {
  return copyText(text)
    .then(() => {
      btn.innerHTML = ICON_CHECK;
      btn.classList.add(activeClass);
    })
    .catch(() => {
      btn.innerHTML = ICON_X;
    })
    .then(() => {
      setTimeout(() => {
        btn.innerHTML = ICON_COPY;
        btn.classList.remove(activeClass);
      }, 1400);
    });
}
