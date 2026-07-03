// PhysicsController — main-thread handle to the MuJoCo physics worker (see
// ./worker.ts). Poses arrive asynchronously and decoupled from
// requestAnimationFrame, matching the render loop to whatever the worker
// last reported rather than blocking on a round trip every frame.

export interface Pose2D {
  x: number;
  y: number;
  yaw: number;
}

export class PhysicsController {
  onPose?: (pose: Pose2D) => void;

  #worker: Worker;

  constructor() {
    this.#worker = new Worker(new URL("./worker.ts", import.meta.url), { type: "module" });
    this.#worker.onmessage = (e: MessageEvent) => {
      const msg = e.data;
      if (msg.type === "pose") this.onPose?.({ x: msg.x, y: msg.y, yaw: msg.yaw });
      else if (msg.type === "error") console.error("[physics]", msg.message);
    };
    // Catches crashes that never make it to a postMessage('error') call
    // (e.g. a WASM trap during module init) so the worker never fails silently.
    this.#worker.onerror = (ev) => console.error("[physics] worker crashed:", ev.message, ev);
  }

  /** Resolves once the physics world has loaded and compiled. Defaults to the apartment (world.xml). */
  init(spawn: Pose2D, worldUrl?: string): Promise<void> {
    return new Promise((resolve) => {
      const onReady = (e: MessageEvent) => {
        if (e.data?.type !== "ready") return;
        this.#worker.removeEventListener("message", onReady);
        resolve();
      };
      this.#worker.addEventListener("message", onReady);
      this.#worker.postMessage({ type: "init", spawn, worldUrl });
    });
  }

  reset(spawn: Pose2D): void {
    this.#worker.postMessage({ type: "reset", spawn });
  }

  /** Advance physics by dt seconds, driving toward (vx, wz) the whole step. */
  step(dt: number, vx: number, wz: number): void {
    this.#worker.postMessage({ type: "step", dt, vx, wz });
  }

  /** Apply an external spring/drag force at a world-space anchor point on the robot. */
  applyForce(anchor: [number, number, number], force: [number, number, number]): void {
    this.#worker.postMessage({
      type: "apply_force",
      anchorX: anchor[0],
      anchorY: anchor[1],
      anchorZ: anchor[2],
      fx: force[0],
      fy: force[1],
      fz: force[2],
    });
  }

  clearForce(): void {
    this.#worker.postMessage({ type: "clear_force" });
  }
}
