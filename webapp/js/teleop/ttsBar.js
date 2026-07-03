// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// TTS bar — type, Enter, the robot says it. While focused, keyboard drive is
// suppressed (the typing-context guard in keyboardDrive). Esc returns focus
// to the page so WASD works again.

import { TTS_TOPIC } from "../constants.js";

const SENT_FLASH_MS = 600;

/**
 * @param {HTMLElement} parent
 * @param {import("../rosClient.js").RosClient} rosClient
 * @returns {{ destroy: () => void }}
 */
export function createTtsBar(parent, rosClient) {
  const wrap = document.createElement("div");
  wrap.className = "tts-bar";

  const input = document.createElement("input");
  input.type = "text";
  input.className = "tts-input";
  input.placeholder = "Make the robot speak…";
  input.autocomplete = "off";
  input.spellcheck = false;
  input.setAttribute("aria-label", "Text to speech");

  const hint = document.createElement("span");
  hint.className = "tts-hint mono";
  hint.textContent = "↵";

  wrap.append(input, hint);
  parent.appendChild(wrap);

  /** @type {number | null} */
  let flashTimer = null;

  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      input.blur();
      return;
    }
    if (e.key !== "Enter") return;
    const text = input.value.trim();
    if (!text) return;
    rosClient.publish(TTS_TOPIC, { data: text });
    input.value = "";
    wrap.classList.add("sent");
    if (flashTimer !== null) clearTimeout(flashTimer);
    flashTimer = setTimeout(() => {
      flashTimer = null;
      wrap.classList.remove("sent");
    }, SENT_FLASH_MS);
  });

  return {
    destroy() {
      if (flashTimer !== null) clearTimeout(flashTimer);
      wrap.remove();
    },
  };
}
