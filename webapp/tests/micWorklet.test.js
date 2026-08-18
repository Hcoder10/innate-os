// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Resampler tests for js/agent/micWorklet.js — zero dependencies, plain node:
//   node tests/micWorklet.test.js
// The worklet is what makes the sim's browser microphone look like the robot's
// arecord stream, so what matters is the contract MicroInput's VAD depends on:
// 16-bit samples on an exact 24 kHz grid, whatever rate the sound card runs at.
// Every case reconstructs the whole stream and compares it against a clean
// 24 kHz sine, which is what catches a discontinuity at a render-quantum seam —
// the worklet sees audio 128 samples at a time and must interpolate across
// those boundaries, not restart at each one.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const WORKLET = fileURLToPath(new URL("../js/agent/micWorklet.js", import.meta.url));
const TARGET_RATE = 24000;
const CHUNK_SAMPLES = 1440;
const TONE_HZ = 440;

let passed = 0;
/** @param {string} name @param {() => void} fn */
function test(name, fn) {
  fn();
  passed += 1;
  console.log(`ok - ${name}`);
}

/**
 * Run the worklet over a sine at `contextRate`, fed in 128-sample render
 * quanta, and return everything it posted as one stream.
 * @param {number} contextRate
 * @param {number} blocks
 * @param {(n: number) => number} [signal] sample at input index n; default sine
 * @returns {{ chunks: Int16Array[], samples: Int16Array, inputCount: number }}
 */
function render(contextRate, blocks, signal) {
  /** @type {Int16Array[]} */
  const chunks = [];
  const scope = {
    sampleRate: contextRate,
    Int16Array,
    Math,
    /** @param {string} _name @param {any} cls */
    registerProcessor: (_name, cls) => (scope.processorClass = cls),
    // The worklet's base class: all it uses is `this.port.postMessage`.
    AudioWorkletProcessor: class {
      constructor() {
        this.port = { postMessage: (/** @type {ArrayBuffer} */ buf) => chunks.push(new Int16Array(buf)) };
      }
    },
    /** @type {any} */ processorClass: null,
  };
  vm.createContext(scope);
  vm.runInContext(readFileSync(WORKLET, "utf8"), scope);

  const processor = new scope.processorClass();
  const tone = signal ?? ((n) => Math.sin((2 * Math.PI * TONE_HZ * n) / contextRate));
  let n = 0;
  for (let b = 0; b < blocks; b++) {
    const block = new Float32Array(128);
    for (let i = 0; i < block.length; i++) block[i] = tone(n++);
    processor.process([[block]]);
  }

  const samples = new Int16Array(chunks.reduce((total, c) => total + c.length, 0));
  let offset = 0;
  for (const c of chunks) {
    samples.set(c, offset);
    offset += c.length;
  }
  return { chunks, samples, inputCount: n };
}

/** Largest deviation from a clean 24 kHz tone, as a fraction of full scale.
 *  @param {Int16Array} samples @returns {number} */
function worstError(samples) {
  let worst = 0;
  for (let i = 0; i < samples.length; i++) {
    const ideal = Math.sin((2 * Math.PI * TONE_HZ * i) / TARGET_RATE) * 0x7fff;
    worst = Math.max(worst, Math.abs(samples[i] - ideal) / 0x7fff);
  }
  return worst;
}

test("posts fixed-size Int16 chunks", () => {
  const { chunks } = render(48000, 400);
  assert.ok(chunks.length > 0, "expected at least one chunk");
  for (const c of chunks) {
    assert.equal(c.length, CHUNK_SAMPLES);
    assert.ok(c instanceof Int16Array);
  }
});

// One case per rate the sound card is likely to hand us: the sim's requested
// 24 kHz, the common 48 kHz, and a non-integer ratio that exercises the
// fractional read position hardest.
for (const [rate, tolerance] of [
  [24000, 0.001],
  [48000, 0.001],
  [44100, 0.005],
]) {
  test(`resamples ${rate} Hz onto the 24 kHz grid`, () => {
    const { samples, inputCount } = render(rate, 400);
    const expected = (inputCount * TARGET_RATE) / rate;
    // Anything not yet emitted is sitting in the partial chunk, so the shortfall
    // is bounded by one chunk. A rate-conversion bug drifts well past that.
    const shortfall = expected - samples.length;
    assert.ok(shortfall >= 0 && shortfall < CHUNK_SAMPLES, `shortfall ${shortfall} out of bounds`);
    assert.ok(worstError(samples) < tolerance, `worst error ${worstError(samples)} exceeds ${tolerance}`);
  });
}

test("clamps out-of-range input instead of wrapping", () => {
  // A sample above 1.0 that wrapped would come back as a large negative number,
  // which reads as a loud click to the VAD.
  const { samples } = render(24000, 200, (n) => (n % 2 ? 4 : -4));
  for (const s of samples) assert.ok(s === 0x7fff || s === -0x8000, `sample ${s} not clamped`);
});

test("silence in, silence out", () => {
  const { samples } = render(48000, 200, () => 0);
  for (const s of samples) assert.equal(s, 0);
});

console.log(`\n${passed} passed`);
