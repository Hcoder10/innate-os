// Ground-truth state feed for the sim viewer: the world server broadcasts
// {t, wall, pose, joints} after every physics slice (~100Hz) on its observer
// WebSocket, which the webapp front door proxies at /worldstate. This is the
// 3D view's only state source -- robot telemetry (/odom, /joint_states) is
// for robot software, and watching the world through the robot's own sensor
// pipeline is exactly the lag the observer stream exists to remove (see
// world_server.py "two interfaces").

export interface WorldState {
  /** Sim clock (s) -- the playback timeline samples interpolate on. */
  t: number;
  /** Server wall clock (s), same physical clock as the browser: Date.now()/1000 - wall
   * is the true server->browser delivery lag (SimSession's lag HUD). */
  wall: number;
  x: number;
  y: number;
  yaw: number;
  joints: Record<string, number>;
}

export class WorldStateController {
  onState?: (state: WorldState) => void;

  #url: string;
  #ws!: WebSocket;
  #open: Promise<void>;
  #resolveOpen!: () => void;
  #rejectOpen!: (err: Error) => void;
  #everOpened = false;
  #disposed = false;
  #retryMs = 500;

  constructor(url: string) {
    this.#url = url;
    this.#open = new Promise((resolve, reject) => {
      this.#resolveOpen = resolve;
      this.#rejectOpen = reject;
    });
    this.#connect();
  }

  /** (Re)open the socket; the server needs no subscription handshake -- it
   * pushes state from the first frame. On drop we retry with backoff until
   * dispose(), so a world-server restart doesn't kill the view. */
  #connect(): void {
    const ws = new WebSocket(this.#url);
    this.#ws = ws;
    ws.onopen = () => {
      this.#everOpened = true;
      this.#retryMs = 500;
      this.#resolveOpen();
    };
    ws.onerror = () => {
      // Settle init()'s await on a failed FIRST attempt (later errors are
      // no-ops on the settled promise); reconnection continues regardless.
      if (!this.#everOpened) this.#rejectOpen(new Error(`world state connection failed: ${this.#url}`));
    };
    ws.onclose = () => {
      if (this.#disposed) return;
      setTimeout(() => this.#connect(), this.#retryMs);
      this.#retryMs = Math.min(this.#retryMs * 2, 5000);
    };
    ws.onmessage = (ev) => this.#onMessage(ev.data as string);
  }

  async init(): Promise<void> {
    await this.#open;
  }

  dispose(): void {
    this.#disposed = true;
    this.#ws.close();
  }

  #onMessage(raw: string): void {
    const msg = JSON.parse(raw) as {
      t: number;
      wall: number;
      pose: [number, number, number];
      joints: Record<string, number>;
    };
    const joints = msg.joints;
    // joint6M is the gripper's mirrored finger (URDF mimic of joint6,
    // multiplier -1) -- the world exposes real joints only, so derive it here.
    joints["joint6M"] = -(joints["joint6"] ?? 0);
    this.onState?.({ t: msg.t, wall: msg.wall, x: msg.pose[0], y: msg.pose[1], yaw: msg.pose[2], joints });
  }
}
