// Main-thread handle to ./urdfWorker.ts (see that file's message protocol).

export interface JointInfo {
  name: string;
  lower: number;
  upper: number;
}

export interface ObstacleInfo {
  name: string;
  shape: "box" | "sphere" | "cylinder";
  size: number[];
  pos: [number, number, number];
}

export interface Pose2D {
  x: number;
  y: number;
  yaw: number;
}

export class UrdfPhysicsController {
  onPose?: (pose: { x: number; y: number; z: number; yaw: number; joints: Record<string, number> }) => void;

  #worker: Worker;
  #readyResolve?: (info: { joints: JointInfo[]; obstacles: ObstacleInfo[] }) => void;

  constructor() {
    this.#worker = new Worker(new URL("./urdfWorker.ts", import.meta.url), { type: "module" });
    this.#worker.onmessage = (e: MessageEvent) => {
      const msg = e.data;
      if (msg.type === "pose") {
        this.onPose?.(msg);
      } else if (msg.type === "ready") {
        this.#readyResolve?.({ joints: msg.joints, obstacles: msg.obstacles });
      } else if (msg.type === "error") {
        console.error("[physics worker]", msg.message);
      }
    };
    this.#worker.onerror = (ev) => console.error("[physics] worker crashed:", ev.message, ev);
  }

  init(spawn: Pose2D): Promise<{ joints: JointInfo[]; obstacles: ObstacleInfo[] }> {
    return new Promise((resolve) => {
      this.#readyResolve = resolve;
      this.#worker.postMessage({ type: "init", spawn });
    });
  }

  reset(spawn: Pose2D): void {
    this.#worker.postMessage({ type: "reset", spawn });
  }

  step(dt: number, vx: number, wz: number, joints: Record<string, number>): void {
    this.#worker.postMessage({ type: "step", dt, vx, wz, joints });
  }

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
