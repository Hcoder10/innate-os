// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Clipboard helper that also works on insecure origins. The webapp is usually
// browsed over plain HTTP (http://<robot-ip>), where navigator.clipboard is
// undefined, so we fall back to the legacy textarea + execCommand path there.

/**
 * Copy text to the clipboard. Resolves on success, rejects on failure.
 * @param {string} text
 * @returns {Promise<void>}
 */
export async function copyText(text) {
  if (navigator.clipboard) return navigator.clipboard.writeText(text);
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try {
    if (!document.execCommand("copy")) throw new Error("execCommand copy failed");
  } finally {
    ta.remove();
  }
}
