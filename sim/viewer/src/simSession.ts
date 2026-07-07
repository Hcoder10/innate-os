// SimSession — a drop-in for the webapp's WebRtcSession when the robot is
// simulated: same state shape and methods, so cameraSwitch/profiling consume
// it unchanged. Unlike WebRTC there is no video pipeline at all -- the
// primary view is a real Three.js canvas mounted by createSimStage (full
// resolution, drag-to-orbit), and only the small PiP thumbnails are
// captureStream canvases the stage blits into.
//
// State comes from the world server's ground-truth observer stream
// (/worldstate -> world_server.py), NOT from robot telemetry: one message
// per physics slice (~100Hz) carrying pose + joints on the sim clock, so
// playback is a short clamped interpolation with no wall-clock mapping and
// no extrapolation. The rosbridge connection remains only for the /scan
// debug overlay -- lidar is a robot sensor, so the robot's pipeline is the
// honest source for it.

import type { SimScene } from "./scene";
import { RosbridgePhysicsController } from "./physics/rosbridgeController";
import { WorldStateController } from "./physics/worldStateController";

/** Thumbnail (PiP tile) render size, shared with createSimStage's scissor
 * pass. Square, matching the webapp's square .cam-tile (--pip-w/h), so
 * object-fit doesn't crop the view. */
export const THUMB_W = 240;
export const THUMB_H = 240;

/** Playback delay bounds (see #delayS): the floor covers one ~100Hz stream
 * interval plus a frame; the ceiling keeps a terrible connection watchable
 * rather than minutes behind. */
const DELAY_MIN_S = 0.015;
const DELAY_MAX_S = 0.25;

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

  #controller: WorldStateController | null = null;
  #scanFeed: RosbridgePhysicsController | null = null;
  #thumbCanvases: HTMLCanvasElement[] = [];
  #thumbContexts: (CanvasRenderingContext2D | null)[] = [];
  #started = false;
  #gotPose = false;
  #stageReady = false;

  // Ground-truth snapshots on the sim clock (one stream: pose and joints
  // arrive in the same message, so no cross-stream stamp mixing to avoid).
  #samples: { t: number; x: number; y: number; yaw: number; joints: Record<string, number> }[] = [];
  // Recent inter-arrival gaps (s) -- sizes the playback delay to measured
  // delivery jitter instead of a hand-tuned constant.
  #gaps: number[] = [];
  #lastArrival = 0;
  // Playback position on the sim clock: advanced by the frame clock and
  // softly steered toward (newest - delay), so delivery jitter is absorbed
  // instead of replayed 1:1 into the scene.
  #playT: number | null = null;
  #live = false;
  #spawned = false;

  // Delivery-lag tracking: the server stamps state with the same physical
  // wall clock the browser reads, so Date.now()/1000 - wall is the true
  // server->browser pipeline delay. The session minimum is the fixed cost
  // of the pipeline; current-minus-min growing while driving means a queue
  // is filling at some hop. Shown by the ?simperf HUD.
  #lagRecent: number[] = [];
  #lagMinS = Infinity;

  // Debug overlays (stage toggle chips); applied to the scene in tick().
  #scan: Float32Array | null = null;
  #scanDirty = false;
  #lidarOn = false;
  #hullsOn = false;
  #overlaysDirty = false;

  #stateUrl: string;
  #rosUrl: string;

  constructor(opts: { stateUrl?: string; rosUrl?: string } = {}) {
    const scheme = location.protocol === "https:" ? "wss" : "ws";
    this.#stateUrl = opts.stateUrl ?? `${scheme}://${location.host}/worldstate`;
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

    this.#controller = new WorldStateController(this.#stateUrl);
    this.#controller.onState = (s) => {
      const lag = Date.now() / 1000 - s.wall;
      if (lag < this.#lagMinS) this.#lagMinS = lag;
      this.#lagRecent.push(lag);
      if (this.#lagRecent.length > 60) this.#lagRecent.shift();

      const now = performance.now() / 1000;
      if (this.#lastArrival > 0) {
        this.#gaps.push(Math.min(now - this.#lastArrival, 0.5));
        if (this.#gaps.length > 60) this.#gaps.shift();
      }
      this.#lastArrival = now;

      const last = this.#samples[this.#samples.length - 1];
      if (last === undefined || s.t > last.t) {
        this.#samples.push({ t: s.t, x: s.x, y: s.y, yaw: s.yaw, joints: s.joints });
        if (this.#samples.length > 60) this.#samples.shift();
      } else if (s.t < last.t - 0.5) {
        // Sim clock jumped backwards (world-server restart): restart playback.
        this.#samples = [{ t: s.t, x: s.x, y: s.y, yaw: s.yaw, joints: s.joints }];
        this.#playT = null;
      }
      this.#live = true;
      if (!this.#gotPose) {
        this.#gotPose = true;
        this.#maybeStreaming();
      }
    };
    this.#controller.init().catch((err) => {
      console.error("[sim-session] world state feed failed:", err);
      this.#patch({ status: "error" });
    });
  }

  stop(): void {
    this.#controller?.dispose();
    this.#controller = null;
    this.#scanFeed?.dispose();
    this.#scanFeed = null;
    this.#started = false;
    this.#gotPose = false;
    this.#patch({ status: "idle", videoStream: null });
  }

  destroy(): void {
    this.stop();
    this.#listeners.clear();
  }

  /** Toggle the /scan hit-point overlay (stage "lidar" chip). The rosbridge
   * connection is opened on first use -- the 3D view itself never consumes
   * robot telemetry. */
  setLidarVisible(on: boolean): void {
    this.#lidarOn = on;
    this.#overlaysDirty = true;
    if (on && this.#scanFeed === null) {
      this.#scanFeed = new RosbridgePhysicsController(this.#rosUrl);
      this.#scanFeed.onScan = (points) => {
        this.#scan = points;
        this.#scanDirty = true;
      };
      this.#scanFeed.init().catch((err) => console.warn("[sim-session] scan overlay unavailable:", err));
    }
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

  /** Playback delay behind the newest sample: 1.5x the p90 inter-arrival gap
   * (clamped), so a bracketing sample has usually arrived when playback needs
   * it. Resolves to ~20ms on a localhost stream and grows only as much as the
   * actual transport demands. */
  #delayS(): number {
    if (this.#gaps.length < 5) return 0.05; // conservative until measured
    const sorted = [...this.#gaps].sort((a, b) => a - b);
    const p90 = sorted[Math.floor(sorted.length * 0.9)];
    return Math.min(DELAY_MAX_S, Math.max(DELAY_MIN_S, p90 * 1.5));
  }

  /** Per-frame: clamped interpolation on the sim clock, #delayS behind the
   * newest sample. A delivery gap holds the last pose -- never extrapolates;
   * delayed-but-coherent beats optimistic-and-wrong. */
  tick(scene: SimScene, dt: number): void {
    if (!this.#live || this.#samples.length === 0) return;
    const first = this.#samples[0];
    if (!this.#spawned) {
      this.#spawned = true;
      scene.spawnAt(first.x, first.y, first.yaw);
    }

    // Advance the playback clock with the frame clock, softly steered toward
    // the stream (hard-locking to `target` would replay delivery jitter 1:1;
    // the ~250ms steering constant absorbs it). A large error -- tab was
    // hidden, server restarted -- snaps instead of chasing for seconds.
    const target = this.#samples[this.#samples.length - 1].t - this.#delayS();
    if (this.#playT === null || Math.abs(target - this.#playT) > 0.3) this.#playT = target;
    else this.#playT += dt + (target - this.#playT) * Math.min(1, dt * 4);

    const [a, b, u] = bracket(this.#samples, this.#playT);
    const x = a.x + (b.x - a.x) * u;
    const y = a.y + (b.y - a.y) * u;
    const dyaw = Math.atan2(Math.sin(b.yaw - a.yaw), Math.cos(b.yaw - a.yaw));
    scene.setPose(x, y, a.yaw + dyaw * u);

    const joints: Record<string, number> = {};
    for (const [name, va] of Object.entries(a.joints)) {
      const vb = b.joints[name] ?? va;
      joints[name] = va + (vb - va) * u;
    }
    scene.setJointAngles(joints);

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

  /** Server->browser state delivery lag: cur is the median of the last ~2s,
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

export function createSimSession(opts: { stateUrl?: string; rosUrl?: string } = {}): SimSession {
  return new SimSession(opts);
}

export { createSimStage } from "./simStage";
