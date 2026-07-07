// SimSession — a drop-in for the webapp's WebRtcSession when the robot is
// simulated: same state shape and methods, so cameraSwitch/profiling consume
// it unchanged. Unlike WebRTC there is no video pipeline at all -- the
// primary view is a real Three.js canvas mounted by createSimStage (full
// resolution, drag-to-orbit), and only the small PiP thumbnails are
// captureStream canvases the stage blits into.
//
// The session owns the rosbridge state feed (/odom + /joint_states, from the
// virtual driver) and the pose-smoothing applied per rendered frame; the
// stage owns the GL context and calls tick()/liveThumbnails()/blitThumbnail.

import type { SimScene } from "./scene";
import { RosbridgePhysicsController } from "./physics/rosbridgeController";

/** Thumbnail (PiP tile) render size, shared with createSimStage's scissor
 * pass. Square, matching the webapp's square .cam-tile (--pip-w/h), so
 * object-fit doesn't crop the view. */
export const THUMB_W = 240;
export const THUMB_H = 240;

/** Playback delay behind the driver's (offset-mapped) clock: sized for
 * typical delivery jitter around its mean, so a bracketing sample has
 * usually arrived when playback needs it. Deliberately NOT worst-case --
 * rare longer gaps are dead-reckoned through instead (see tick), keeping
 * the scene close to the map widget, which draws raw samples on arrival. */
const INTERP_DELAY_S = 0.05;

/** Longest gap the pose stream extrapolates through before holding. The
 * base accelerates at ~1 m/s^2, so 150ms of dead reckoning mis-predicts by
 * ~1cm worst-case -- invisible, unlike a fatter interpolation delay. */
const MAX_EXTRAP_S = 0.15;

/** EMA weight per sample for the browser-to-driver clock offset (~1s time
 * constant at 30Hz): fast enough to absorb the container clock's drift and
 * step corrections, slow enough that delivery jitter averages out. */
const OFFSET_EMA_ALPHA = 0.03;

/** Advance `samples` past renderT and return the bracketing pair plus the
 * clamped interpolation factor (holds the last sample during a gap instead
 * of extrapolating past it). Mutates the array (drops consumed history). */
function bracket<T extends { t: number }>(samples: T[], renderT: number): [T, T, number] {
  while (samples.length > 2 && samples[1].t <= renderT) samples.shift();
  const a = samples[0];
  const b = samples.length > 1 ? samples[1] : a;
  const span = b.t - a.t;
  const u = span > 1e-4 ? Math.min(1, Math.max(0, (renderT - a.t) / span)) : 1;
  return [a, b, u];
}

export interface SimSessionState {
  status: "idle" | "connecting" | "streaming" | "error";
  videoStream: MediaStream | null;
  videoStreams: (MediaStream | null)[];
  videoLive: boolean[];
  audioStream: MediaStream | null;
  audioRequested: boolean;
  iceState: string;
  stunFallback: boolean;
}

export class SimSession {
  #state: SimSessionState = {
    status: "idle",
    videoStream: null,
    videoStreams: [],
    videoLive: [],
    audioStream: null,
    audioRequested: false,
    iceState: "connected",
    stunFallback: false,
  };
  #listeners = new Set<(state: SimSessionState) => void>();

  #roster = ["main", "arm", "orbit"];
  #activeCams = ["main"];
  #primaryIndex = 0;
  #primaryName = "main";

  #controller: RosbridgePhysicsController | null = null;
  #thumbCanvases: HTMLCanvasElement[] = [];
  #thumbContexts: (CanvasRenderingContext2D | null)[] = [];
  #started = false;
  #gotPose = false;
  #stageReady = false;

  // Snapshot buffers for interpolated playback, keyed by the driver's own
  // stamps: rws delivery is bursty (median 32ms gaps, p90 ~70ms), and
  // arrival-time playback replays every burst as uneven motion, while the
  // stamps are an evenly spaced 30Hz timer. Playback maps the local clock
  // onto the stamp clock through #offsetS and renders INTERP_DELAY_S behind.
  // Pose and joints are separate streams: /odom and /joint_states interleave,
  // and a merged buffer stamps stale poses with fresh joint times, rendering
  // 30Hz pose data as 60Hz stop-go stutter.
  #poseSamples: { t: number; x: number; y: number; yaw: number }[] = [];
  #jointSamples: { t: number; joints: Record<string, number> }[] = [];
  // EMA of (local arrival - stamp). An AVERAGE is the whole trick: it is
  // bounded by construction (a min-ratchet integrates clock steps forever;
  // an incrementally built timeline ratchets on every hiccup -- both were
  // tried, both accumulated visible delay), it absorbs the container
  // clock's drift within ~1s, and jitter cancels out of it.
  #offsetS: number | null = null;
  #live = false;
  #spawned = false;

  // Delivery-lag tracking: the driver stamps state with the same physical
  // wall clock the browser reads, so Date.now()/1000 - stampS is the true
  // driver->browser pipeline delay. The session minimum is the fixed cost
  // of the pipeline; current-minus-min growing while driving means a queue
  // is filling at some hop (rws, DDS, proxy). Shown by the ?simperf HUD.
  #lagRecent: number[] = [];
  #lagMinS = Infinity;

  // Debug overlays (stage toggle chips); applied to the scene in tick().
  #scan: Float32Array | null = null;
  #scanDirty = false;
  #lidarOn = false;
  #hullsOn = false;
  #overlaysDirty = false;

  #rosUrl: string;

  constructor(opts: { rosUrl?: string } = {}) {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    this.#rosUrl = opts.rosUrl ?? `${scheme}://${location.host}/ws`;
  }

  get state(): SimSessionState {
    return { ...this.#state, videoStreams: [...this.#state.videoStreams], videoLive: [...this.#state.videoLive] };
  }

  get primaryCamera(): string {
    return this.#primaryName;
  }

  onChange(cb: (state: SimSessionState) => void): () => void {
    this.#listeners.add(cb);
    cb(this.state);
    return () => this.#listeners.delete(cb);
  }

  start(): void {
    if (this.#started) return;
    this.#started = true;
    this.#patch({ status: "connecting" });

    this.#thumbCanvases = this.#roster.map(() => {
      const c = document.createElement("canvas");
      c.width = THUMB_W;
      c.height = THUMB_H;
      return c;
    });
    this.#thumbContexts = this.#thumbCanvases.map((c) => c.getContext("2d"));
    // No captureStream: an active canvas-capture pipeline pinned the whole
    // page's composition to its 15fps tick (measured: 16fps with capture,
    // 120fps without). The webapp mounts these canvases directly instead
    // (thumbnailCanvas below); a 2D canvas in the DOM repaints only when
    // drawn into -- no media pipeline at all.

    this.#controller = new RosbridgePhysicsController(this.#rosUrl);
    this.#controller.onScan = (points) => {
      this.#scan = points;
      this.#scanDirty = true;
    };
    this.#controller.onPose = ({ x, y, yaw, stampS }) => {
      const lag = Date.now() / 1000 - stampS;
      if (lag < this.#lagMinS) this.#lagMinS = lag;
      this.#lagRecent.push(lag);
      if (this.#lagRecent.length > 60) this.#lagRecent.shift();
      const off = performance.now() / 1000 - stampS;
      this.#offsetS = this.#offsetS === null ? off : this.#offsetS + OFFSET_EMA_ALPHA * (off - this.#offsetS);
      const last = this.#poseSamples[this.#poseSamples.length - 1];
      if (last === undefined || stampS > last.t) {
        this.#poseSamples.push({ t: stampS, x, y, yaw });
        if (this.#poseSamples.length > 40) this.#poseSamples.shift();
      }
      this.#live = true;
      if (!this.#gotPose) {
        this.#gotPose = true;
        this.#maybeStreaming();
      }
    };
    this.#controller.onJoints = ({ joints, stampS }) => {
      const last = this.#jointSamples[this.#jointSamples.length - 1];
      if (last === undefined || stampS > last.t) {
        this.#jointSamples.push({ t: stampS, joints });
        if (this.#jointSamples.length > 40) this.#jointSamples.shift();
      }
    };
    this.#controller.init().catch((err) => {
      console.error("[sim-session] rosbridge init failed:", err);
      this.#patch({ status: "error" });
    });
  }

  stop(): void {
    this.#controller?.dispose();
    this.#controller = null;
    this.#started = false;
    this.#gotPose = false;
    this.#patch({ status: "idle", videoStream: null });
  }

  destroy(): void {
    this.stop();
    this.#listeners.clear();
  }

  /** Toggle the /scan hit-point overlay (stage "lidar" chip). */
  setLidarVisible(on: boolean): void {
    this.#lidarOn = on;
    this.#overlaysDirty = true;
  }

  /** Toggle the collision-hull wireframe overlay (stage "collisions" chip). */
  setCollisionHullsVisible(on: boolean): void {
    this.#hullsOn = on;
    this.#overlaysDirty = true;
  }

  // WebRTC-specific surface: harmless no-ops in sim.
  setAudio(_on: boolean): void {}
  async getStats(): Promise<null> {
    return null;
  }

  setActiveCameras(names: string[]): void {
    this.#activeCams = names.filter((n) => this.#roster.includes(n));
    if (!this.#activeCams.includes(this.#primaryName) && this.#activeCams.length) {
      this.#primaryName = this.#activeCams[0];
      this.#primaryIndex = this.#roster.indexOf(this.#primaryName);
    }
    this.#patch(this.#videoArrays());
  }

  setPrimaryCamera(index: number, name: string): void {
    if (index < 0 || index >= this.#roster.length) return;
    this.#primaryIndex = index;
    this.#primaryName = name;
    if (!this.#activeCams.includes(name)) this.#activeCams.push(name);
    this.#patch(this.#videoArrays());
  }

  // --- stage integration (createSimStage) ---

  /** Stage scene finished loading its assets. */
  stageReady(): void {
    this.#stageReady = true;
    this.#maybeStreaming();
  }

  stageError(err: unknown): void {
    console.error("[sim-session] stage failed:", err);
    this.#patch({ status: "error" });
  }

  /** Per-frame: interpolated playback INTERP_DELAY_S behind the driver's
   * stamp clock, mapped onto the local clock via #offsetS. */
  tick(scene: SimScene, _dt: number): void {
    if (!this.#live || this.#poseSamples.length === 0 || this.#offsetS === null) return;
    const first = this.#poseSamples[0];
    if (!this.#spawned) {
      this.#spawned = true;
      scene.spawnAt(first.x, first.y, first.yaw);
    }

    const renderT = performance.now() / 1000 - this.#offsetS - INTERP_DELAY_S;
    const [a, b, u] = bracket(this.#poseSamples, renderT);
    let x = a.x + (b.x - a.x) * u;
    let y = a.y + (b.y - a.y) * u;
    const dyaw = Math.atan2(Math.sin(b.yaw - a.yaw), Math.cos(b.yaw - a.yaw));
    let yaw = a.yaw + dyaw * u;
    // Playback caught up with the newest sample (delivery gap): dead-reckon
    // from the last two samples' velocity instead of visibly freezing. The
    // span guard skips degenerate pairs whose velocity would be noise.
    const span = b.t - a.t;
    if (renderT > b.t && span > 0.005) {
      const dt = Math.min(renderT - b.t, MAX_EXTRAP_S);
      x = b.x + ((b.x - a.x) / span) * dt;
      y = b.y + ((b.y - a.y) / span) * dt;
      yaw = b.yaw + (dyaw / span) * dt;
    }
    scene.setPose(x, y, yaw);

    if (this.#jointSamples.length > 0) {
      const [ja, jb, ju] = bracket(this.#jointSamples, renderT);
      const joints: Record<string, number> = {};
      for (const [name, va] of Object.entries(ja.joints)) {
        const vb = jb.joints[name] ?? va;
        joints[name] = va + (vb - va) * ju;
      }
      scene.setJointAngles(joints);
    }

    if (this.#overlaysDirty) {
      this.#overlaysDirty = false;
      scene.setLidarVisible(this.#lidarOn);
      scene.setCollisionHullsVisible(this.#hullsOn);
    }
    if (this.#lidarOn && this.#scanDirty && this.#scan) {
      this.#scanDirty = false;
      scene.setLidarPoints(this.#scan);
      scene.setLidarVisible(true); // first points may arrive after the toggle
    }
  }

  /** Active, non-primary views whose PiP tiles need frames. */
  liveThumbnails(): { index: number; name: string }[] {
    return this.#roster
      .map((name, index) => ({ index, name }))
      .filter(({ index, name }) => index !== this.#primaryIndex && this.#activeCams.includes(name));
  }

  /** Copy a region rendered at the source canvas' bottom-left into a thumb
   * stream (GL viewport origin is bottom-left; 2D canvas is top-left). */
  blitThumbnail(index: number, source: HTMLCanvasElement, pixelW: number, pixelH: number): void {
    this.#thumbContexts[index]?.drawImage(source, 0, source.height - pixelH, pixelW, pixelH, 0, 0, THUMB_W, THUMB_H);
  }

  #maybeStreaming(): void {
    if (this.#gotPose && this.#stageReady) {
      this.#patch({ status: "streaming", ...this.#videoArrays() });
    }
  }

  #videoArrays() {
    return {
      // No MediaStreams in sim: tiles mount thumbnailCanvas() nodes instead.
      videoStreams: this.#roster.map(() => null),
      videoLive: this.#roster.map((name) => this.#gotPose && this.#stageReady && this.#activeCams.includes(name)),
    };
  }

  /** The live 2D canvas behind a PiP tile; the webapp mounts it directly. */
  thumbnailCanvas(index: number): HTMLCanvasElement | null {
    return this.#thumbCanvases[index] ?? null;
  }

  /** Driver->browser state delivery lag: cur is the median of the last ~2s,
   * min is the session floor (the pipeline's fixed cost). cur >> min means
   * a queue is filling upstream. Null until state has arrived. */
  get pipelineLag(): { curMs: number; minMs: number } | null {
    if (this.#lagRecent.length === 0) return null;
    const sorted = [...this.#lagRecent].sort((a, b) => a - b);
    return { curMs: sorted[sorted.length >> 1] * 1000, minMs: this.#lagMinS * 1000 };
  }

  #patch(partial: Partial<SimSessionState>): void {
    Object.assign(this.#state, partial);
    const snapshot = this.state;
    for (const cb of this.#listeners) cb(snapshot);
  }
}

export function createSimSession(opts: { rosUrl?: string } = {}): SimSession {
  return new SimSession(opts);
}

export { createSimStage } from "./simStage";
