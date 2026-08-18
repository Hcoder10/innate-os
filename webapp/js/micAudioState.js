// @ts-check
// Shared audio state, kept out of both owners so ttsAudio and the sessions don't
// import each other.
//
// micAudioActive: the robot's mic is streaming to this browser (set by the
// WebRTC session). ttsAudio skips clips while it is on — we already hear the
// robot's speaker through the mic.
//
// ttsPlaying: a clip is playing. The sim's mic stream stops publishing while it
// is on, so the robot does not transcribe its own voice.

let micActive = false;

/** @param {boolean} on */
export function setMicAudioActive(on) {
  micActive = on;
}

/** @returns {boolean} */
export function isMicAudioActive() {
  return micActive;
}

// A clip can start before the previous one has ended, so count rather than flag.
let ttsClips = 0;

/** @param {boolean} playing */
export function setTtsPlaying(playing) {
  ttsClips = Math.max(0, ttsClips + (playing ? 1 : -1));
}

/** @returns {boolean} */
export function isTtsPlaying() {
  return ttsClips > 0;
}
