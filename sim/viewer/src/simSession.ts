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

/** Thumbnail (PiP tile) render size, shared with createSimStage's scissor pass. */
export const THUMB_W = 320;
export const THUMB_H = 180;

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
  #streams: (MediaStream | null)[] = [];
  #started = false;
  #gotPose = false;
  #stageReady = false;

  // Smoothing target; applied by tick() every rendered frame.
  #target = { x: 0, y: 0, yaw: 0, joints: {} as Record<string, number>, live: false };
  #pose = { x: 0, y: 0, yaw: 0 };
  #shownJoints: Record<string, number> = {};
  #spawned = false;

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
    this.#streams = this.#thumbCanvases.map((c) => c.captureStream(15));

    this.#controller = new RosbridgePhysicsController(this.#rosUrl);
    this.#controller.onScan = (points) => {
      this.#scan = points;
      this.#scanDirty = true;
    };
    this.#controller.onPose = ({ x, y, yaw, joints }) => {
      Object.assign(this.#target, { x, y, yaw, live: true });
      Object.assign(this.#target.joints, joints);
      if (!this.#gotPose) {
        this.#gotPose = true;
        this.#pose = { x, y, yaw };
        Object.assign(this.#shownJoints, joints);
        this.#maybeStreaming();
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

  /** Per-frame: smooth the scene toward the latest network sample. */
  tick(scene: SimScene, dt: number): void {
    if (!this.#target.live) return;
    if (!this.#spawned) {
      this.#spawned = true;
      scene.spawnAt(this.#pose.x, this.#pose.y, this.#pose.yaw);
    }
    const alpha = 1 - Math.exp(-dt * 15);
    this.#pose.x += (this.#target.x - this.#pose.x) * alpha;
    this.#pose.y += (this.#target.y - this.#pose.y) * alpha;
    const dyaw = Math.atan2(Math.sin(this.#target.yaw - this.#pose.yaw), Math.cos(this.#target.yaw - this.#pose.yaw));
    this.#pose.yaw += dyaw * alpha;
    for (const [name, target] of Object.entries(this.#target.joints)) {
      this.#shownJoints[name] = (this.#shownJoints[name] ?? target) + (target - (this.#shownJoints[name] ?? target)) * alpha;
    }
    scene.setPose(this.#pose.x, this.#pose.y, this.#pose.yaw);
    scene.setJointAngles(this.#shownJoints);

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
      // The primary view is the live stage canvas, not a stream -- but tiles
      // only display non-primary entries, so streams exist for all actives.
      videoStreams: this.#roster.map((name, i) => (this.#activeCams.includes(name) ? this.#streams[i] : null)),
      videoLive: this.#roster.map((name) => this.#gotPose && this.#stageReady && this.#activeCams.includes(name)),
    };
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
