// TestPhysicsController — main-thread handle to the collision test sandbox
// worker (see ./testWorker.ts). Mirrors PhysicsController's shape but for
// the sandbox's simpler protocol: no driving, just gravity + optional drag
// force on any free body.

export interface BodyPose {
  name: string;
  x: number;
  y: number;
  z: number;
  qw: number;
  qx: number;
  qy: number;
  qz: number;
}

export interface BodyRef {
  name: string;
  bodyId: number;
}

export class TestPhysicsController {
  onPoses?: (bodies: BodyPose[]) => void;

  #worker: Worker;

  constructor() {
    this.#worker = new Worker(new URL("./testWorker.ts", import.meta.url), { type: "module" });
    this.#worker.onmessage = (e: MessageEvent) => {
      const msg = e.data;
      if (msg.type === "pose") this.onPoses?.(msg.bodies);
      else if (msg.type === "error") console.error("[physics]", msg.message);
    };
    this.#worker.onerror = (ev) => console.error("[physics] worker crashed:", ev.message, ev);
  }

  /** Resolves with the list of free bodies once the world has loaded and compiled. */
  init(): Promise<BodyRef[]> {
    return new Promise((resolve) => {
      const onReady = (e: MessageEvent) => {
        if (e.data?.type !== "ready") return;
        this.#worker.removeEventListener("message", onReady);
        resolve(e.data.bodies);
      };
      this.#worker.addEventListener("message", onReady);
      this.#worker.postMessage({ type: "init" });
    });
  }

  reset(): void {
    this.#worker.postMessage({ type: "reset" });
  }

  step(dt: number): void {
    this.#worker.postMessage({ type: "step", dt });
  }

  applyForce(bodyId: number, anchor: [number, number, number], force: [number, number, number]): void {
    this.#worker.postMessage({
      type: "apply_force",
      bodyId,
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
