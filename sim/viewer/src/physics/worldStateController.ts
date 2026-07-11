// Ground-truth state feed for the sim viewer: the world server broadcasts
// {t, wall, pose, joints} per physics slice on its observer WebSocket
// (proxied at /worldstate) -- the 3D view's only state source. See
// world_server.py "two interfaces".

export interface WorldState {
  /** Sim clock (s) -- the playback timeline. */
  t: number;
  /** Server wall clock (s): Date.now()/1000 - wall = true delivery lag. */
  wall: number;
  x: number;
  y: number;
  yaw: number;
  joints: Record<string, number>;
  /** Scenario human ground truth [x, y, z, qw, qx, qy, qz]; null until dropped. */
  human: number[] | null;
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

  /** (Re)open the socket (no handshake; the server just pushes). On drop,
   * retry with backoff until dispose(). */
  #connect(): void {
    const ws = new WebSocket(this.#url);
    this.#ws = ws;
    ws.onopen = () => {
      this.#everOpened = true;
      this.#retryMs = 500;
      this.#resolveOpen();
    };
    ws.onerror = () => {
      // Settle init()'s await on a failed FIRST attempt; reconnection continues.
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

  /** Send a scenario command up the observer socket (e.g. drop_human);
   * dropped silently while the socket is (re)connecting. */
  send(cmd: object): void {
    if (this.#ws.readyState === WebSocket.OPEN) this.#ws.send(JSON.stringify(cmd));
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
      human?: number[] | null;
    };
    const joints = msg.joints;
    // joint6M: the gripper's mirrored finger (URDF mimic of joint6, x-1).
    joints["joint6M"] = -(joints["joint6"] ?? 0);
    this.onState?.({
      t: msg.t,
      wall: msg.wall,
      x: msg.pose[0],
      y: msg.pose[1],
      yaw: msg.pose[2],
      joints,
      human: msg.human ?? null,
    });
  }
}
