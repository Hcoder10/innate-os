// @ts-check
// Robot speech playback. The brain publishes synthesized speech (base64 WAV) on
// /tts/audio whenever it speaks — "make the robot speak", agent replies, skill
// narration, anything routed through the brain's TTS. The real robot plays this
// out its physical speaker; the sim has none, so the browser is the speaker.
// Mounted from the shell (which loads on every page), so speech plays no matter
// which page is open.

import { ros } from "./rosClient.js";
import { isMicAudioActive } from "./micAudioState.js";

const TTS_AUDIO_TOPIC = "/tts/audio";

let started = false;

export function initTtsAudio() {
  if (started) return;
  started = true;

  ros.subscribe(TTS_AUDIO_TOPIC, (msg) => {
    const b64 = msg?.data;
    if (typeof b64 !== "string" || !b64) return;
    // The robot also plays this out its physical speaker. When the operator has
    // the mic open we hear that speaker through it, so skip the clip to avoid
    // doubling. (In the sim there's no mic, so this is never set and we play.)
    if (isMicAudioActive()) return;
    try {
      play(b64);
    } catch (err) {
      console.warn("[tts] failed to play audio:", err);
    }
  });
}

/** @param {string} b64 base64-encoded WAV */
function play(b64) {
  const blob = new Blob([/** @type {BlobPart} */ (base64ToBytes(b64))], { type: "audio/wav" });
  const url = URL.createObjectURL(blob);
  const audio = new Audio(url);
  audio.addEventListener("ended", () => URL.revokeObjectURL(url), { once: true });
  audio.play().catch((err) => {
    // Browser autoplay policies block playback until the user has interacted
    // with the page; after any click/keypress this succeeds.
    console.warn("[tts] autoplay blocked (interact with the page first):", err?.message || err);
    URL.revokeObjectURL(url);
  });
}

/** @param {string} b64 @returns {Uint8Array} */
function base64ToBytes(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}
