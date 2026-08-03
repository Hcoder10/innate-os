// Ground-truth state feed for the sim viewer: the world server broadcasts
// {t, wall, pose, joints, objects} per physics slice on its observer WebSocket
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
  /** Ground truth of every manipulation prop (world.py GRASP_OBJECTS), keyed
   * by name: [x, y, z, qw, qx, qy, qz]. Empty on servers that predate them. */
  objects: Record<string, number[]>;
}

/** Drawable description of one manipulation prop, in MuJoCo semantics
 * (world.py grasp_object_specs): half-extents for boxes, [radius,
 * half-height] for cylinders, metres; rgba 0..1. */
export interface ObjectSpec {
  type: "box" | "cylinder" | "sphere";
  size: number[];
  rgba: number[];
}

export class WorldStateController {
  onState?: (state: WorldState) => void;
  /** One-time prop roster the server opens each connection with; absent on
   * servers that predate the manipulation props. */
  onObjectSpecs?: (specs: Record<string, ObjectSpec>) => void;

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

  /** Send a stage command (e.g. drop_objects) back up the observer socket.
   * Dropped silently while the socket is (re)connecting -- it is a button
   * press, not something worth queueing. */
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
      objects?: Record<string, number[]> | null;
      object_specs?: Record<string, ObjectSpec>;
    };
    if (msg.object_specs) {
      // The connection-opening roster frame; carries no state.
      this.onObjectSpecs?.(msg.object_specs);
      return;
    }
    const joints = msg.joints;
    // joint6M: the gripper's mirrored finger (URDF mimic of joint6, x-1).
    // Only a fallback for servers that don't stream it: under contact load
    // each finger is torque-clamped independently, so the real angle (which
    // current servers include) can differ from the mirror.
    joints["joint6M"] ??= -(joints["joint6"] ?? 0);
    this.onState?.({
      t: msg.t,
      wall: msg.wall,
      x: msg.pose[0],
      y: msg.pose[1],
      yaw: msg.pose[2],
      joints,
      objects: msg.objects ?? {},
    });
  }
}
