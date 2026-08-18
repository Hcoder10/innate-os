// @ts-check
// Shared audio state, kept out of both owners so ttsAudio and the sessions
// don't import each other.
//
// micAudioActive: is the robot's microphone being streamed to (and played in)
// this browser? Set by the WebRTC session when the operator toggles mic audio;
// read by ttsAudio to decide whether to play a /tts/audio clip — while the mic
// is open we already hear the robot's speaker through it.
//
// ttsPlaying: is a /tts/audio clip playing right now? Set by ttsAudio; read by
// the sim's mic stream, which stops publishing while it is true so the robot
// does not transcribe its own voice back to itself.

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
