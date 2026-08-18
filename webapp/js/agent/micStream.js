// @ts-check
// The browser as the sim robot's microphone, mirroring ttsAudio where it is the
// speaker. Streams base64 PCM16 on /mic/audio, where MicroInput takes it in
// place of arecord and runs the robot's own VAD + transcription. Never mounts
// against a real robot, which listens with its own mic.
//
// Publishing pauses while a clip plays in this page: the sim's speaker is these
// same speakers, and the robot must not transcribe itself.

import { MIC_AUDIO_TOPIC } from "../constants.js";
import { isTtsPlaying } from "../micAudioState.js";

const WORKLET_URL = "/js/agent/micWorklet.js";
// The rate MicroInput's VAD and the vendor wire formats are built around.
const SAMPLE_RATE = 24000;

/**
 * @typedef {{ on: boolean, busy: boolean, level: number, error: string | null }} MicState
 */

/**
 * @param {import("../rosClient.js").RosClient} rosClient
 * @param {(state: MicState) => void} onState
 * @returns {{ toggle: () => Promise<void>, stop: () => void, destroy: () => void }}
 */
export function createMicStream(rosClient, onState) {
  /** @type {MicState} */
  let state = { on: false, busy: false, level: 0, error: null };
  /** @type {MediaStream | null} */
  let media = null;
  /** @type {AudioContext | null} */
  let ctx = null;
  /** @type {(() => void) | null} */
  let unadvertise = null;
  let destroyed = false;

  /** @param {Partial<MicState>} patch */
  function patch(patch) {
    state = { ...state, ...patch };
    onState(state);
  }

  async function start() {
    if (!navigator.mediaDevices?.getUserMedia) {
      // getUserMedia exists only in a secure context — over plain http from
      // another machine the API is simply absent.
      patch({ error: "Open the app over https to use the microphone" });
      return;
    }
    patch({ busy: true, error: null });
    try {
      media = await navigator.mediaDevices.getUserMedia({
        // Keeps the robot's own speech out of its ears; the gate in publish()
        // covers what leaks past it.
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      // The mic device subscribes only once the agent runs, so the bridge has
      // nothing to infer the type from yet.
      unadvertise = rosClient.advertise(MIC_AUDIO_TOPIC, "std_msgs/msg/String");
      if (destroyed) return teardown(); // unmounted mid-await: a stream captured now is never stopped
      ctx = new AudioContext({ sampleRate: SAMPLE_RATE });
      await ctx.audioWorklet.addModule(WORKLET_URL);
      if (destroyed) return teardown();
      const chunker = new AudioWorkletNode(ctx, "mic-chunker");
      chunker.port.onmessage = (e) => publish(e.data);
      ctx.createMediaStreamSource(media).connect(chunker);
      // A processor whose output goes nowhere is not guaranteed to be pulled,
      // so the graph reaches the destination through a silent gain node.
      const mute = ctx.createGain();
      mute.gain.value = 0;
      chunker.connect(mute).connect(ctx.destination);
      patch({ on: true, busy: false });
    } catch (err) {
      teardown();
      patch({ on: false, busy: false, level: 0, error: describe(err) });
    }
  }

  /** @param {ArrayBuffer} buf */
  function publish(buf) {
    if (destroyed) return;
    const pcm = new Int16Array(buf);
    patch({ level: rms(pcm) });
    if (isTtsPlaying()) return;
    rosClient.publish(MIC_AUDIO_TOPIC, { data: toBase64(new Uint8Array(buf)) });
  }

  function teardown() {
    for (const track of media?.getTracks() ?? []) track.stop();
    media = null;
    void ctx?.close();
    ctx = null;
    unadvertise?.();
    unadvertise = null;
  }

  function stop() {
    if (!state.on) return;
    teardown();
    patch({ on: false, level: 0 });
  }

  return {
    async toggle() {
      if (state.busy) return;
      if (state.on) stop();
      else await start();
    },
    stop,
    destroy() {
      destroyed = true;
      teardown();
    },
  };
}

/** @param {Int16Array} pcm @returns {number} 0..1 */
function rms(pcm) {
  let sum = 0;
  for (const s of pcm) sum += s * s;
  return Math.sqrt(sum / pcm.length) / 32768;
}

/** @param {Uint8Array} bytes @returns {string} */
function toBase64(bytes) {
  let bin = "";
  // Chunked: fromCharCode spreads its arguments, and a whole chunk at once sits
  // close enough to the argument limit to be worth not testing.
  for (let i = 0; i < bytes.length; i += 1024) {
    bin += String.fromCharCode(...bytes.subarray(i, i + 1024));
  }
  return btoa(bin);
}

/** @param {any} err @returns {string} */
function describe(err) {
  if (err?.name === "NotAllowedError") return "Microphone permission denied";
  if (err?.name === "NotFoundError") return "No microphone found";
  return err?.message || "Microphone unavailable";
}
