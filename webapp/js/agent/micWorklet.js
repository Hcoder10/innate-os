// Audio-thread half of the sim's microphone (see micStream.js): resamples the
// capture stream onto the robot's 24 kHz grid and posts it out as PCM16 chunks.
//
// Not an ES module — loaded by AudioWorklet.addModule(), which evaluates it in
// the audio rendering scope: no imports, no DOM, and globals lib.dom does not
// declare, so tsconfig excludes this one file. It runs off the main thread on
// purpose: the Agent page renders the sim with Three.js, and audio captured on
// the main thread drops out whenever a frame runs long.

const TARGET_RATE = 24000;
// 60 ms per message — 17 publishes/s over rosbridge instead of 50 at the
// robot's 20 ms cadence. The endpointer buffers, so chunk length is free.
const CHUNK_SAMPLES = 1440;

class MicChunker extends AudioWorkletProcessor {
  constructor() {
    super();
    // `sampleRate` is the context's rate; the step is 1 when it already runs at
    // TARGET_RATE, which is what AudioContext({sampleRate}) normally gives us.
    this._step = sampleRate / TARGET_RATE;
    // Read position in the current block. Starts negative after a seam: the
    // sample it interpolates from is the previous block's last one.
    this._pos = 0;
    this._prev = 0;
    this._chunk = new Int16Array(CHUNK_SAMPLES);
    this._filled = 0;
  }

  process(inputs) {
    const input = inputs[0]?.[0];
    if (!input?.length) return true;

    let pos = this._pos;
    while (pos + 1 < input.length) {
      const i = Math.floor(pos);
      const s0 = i < 0 ? this._prev : input[i];
      this._push(s0 + (input[i + 1] - s0) * (pos - i));
      pos += this._step;
    }
    this._prev = input[input.length - 1];
    this._pos = pos - input.length;
    return true;
  }

  _push(sample) {
    const s = Math.max(-1, Math.min(1, sample));
    this._chunk[this._filled++] = s < 0 ? s * 0x8000 : s * 0x7fff;
    if (this._filled < CHUNK_SAMPLES) return;
    this.port.postMessage(this._chunk.buffer, [this._chunk.buffer]);
    this._chunk = new Int16Array(CHUNK_SAMPLES);
    this._filled = 0;
  }
}

registerProcessor("mic-chunker", MicChunker);
