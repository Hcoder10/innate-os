// @ts-check
// Shared flag: is the robot's microphone currently being streamed to (and
// played in) this browser? Set by the WebRTC session when the operator toggles
// mic audio; read by the TTS playback (ttsAudio) to decide whether to play a
// /tts/audio clip. While the mic is open we already hear the robot's speaker
// through it, so playing the clip too would double the speech.

let micActive = false;

/** @param {boolean} on */
export function setMicAudioActive(on) {
  micActive = on;
}

/** @returns {boolean} */
export function isMicAudioActive() {
  return micActive;
}
