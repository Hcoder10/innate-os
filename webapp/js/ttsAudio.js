// @ts-check
// Robot speech playback. In SIM mode the brain publishes synthesized speech
// (base64 WAV) on /tts/audio whenever it speaks — "make the robot speak", agent
// replies, skill narration — because the sim has no audio device, so the
// browser is the speaker. The real robot plays speech out its own physical
// speaker and publishes nothing here (a browser playing it too would double
// the voice), so against a robot this module simply never fires.
// Mounted from the shell (which loads on every page), so speech plays no matter
// which page is open.

import { ros } from "./rosClient.js";
import { isMicAudioActive } from "./micAudioState.js";

const TTS_AUDIO_TOPIC = "/tts/audio";

let started = false;

// One speaker across tabs: rosbridge fans /tts/audio out to every client, so
// N open tabs played N overlapping copies. A held Web Lock elects exactly one
// playing tab; when that tab closes, the browser passes the lock (and the
// voice) to the next one. Browsers without Web Locks keep the old behavior.
let speaker = !("locks" in navigator);
navigator.locks?.request("innate-tts-speaker", () => {
  speaker = true;
  return new Promise(() => {}); // hold until this tab closes
});

export function initTtsAudio() {
  if (started) return;
  started = true;

  ros.subscribe(TTS_AUDIO_TOPIC, (msg) => {
    if (!speaker) return; // another tab is the elected speaker
    const b64 = msg?.data;
    if (typeof b64 !== "string" || !b64) return;
    // Defensive: if a clip does arrive while the operator has the robot mic
    // open, skip it — the speaker would be heard through the mic as well.
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
