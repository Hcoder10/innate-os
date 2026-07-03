// Main-thread handle to ./apartmentWorker.ts (see that file's message protocol).

import type { CollisionMode } from "./apartmentWorker";

export interface JointInfo {
  name: string;
  lower: number;
  upper: number;
}

export interface Pose2D {
  x: number;
  y: number;
  yaw: number;
}

export class ApartmentPhysicsController {
  onPose?: (pose: { x: number; y: number; z: number; yaw: number; joints: Record<string, number> }) => void;

  #worker: Worker;
  #readyResolve?: (info: { joints: JointInfo[] }) => void;

  constructor() {
    this.#worker = new Worker(new URL("./apartmentWorker.ts", import.meta.url), { type: "module" });
    this.#worker.onmessage = (e: MessageEvent) => {
      const msg = e.data;
      if (msg.type === "pose") {
        this.onPose?.(msg);
      } else if (msg.type === "ready") {
        this.#readyResolve?.({ joints: msg.joints });
      } else if (msg.type === "error") {
        console.error("[physics worker]", msg.message);
      }
    };
    this.#worker.onerror = (ev) => console.error("[physics] worker crashed:", ev.message, ev);
  }

  init(spawn: Pose2D, collision: CollisionMode = "hulls"): Promise<{ joints: JointInfo[] }> {
    return new Promise((resolve) => {
      this.#readyResolve = resolve;
      this.#worker.postMessage({ type: "init", spawn, collision });
    });
  }

  /** Kills the worker. The controller is unusable afterwards -- construct a
   * new one to reinitialize (e.g. to switch collision mode, which needs a
   * fresh model compile). */
  dispose(): void {
    this.#worker.terminate();
  }

  reset(spawn: Pose2D): void {
    this.#worker.postMessage({ type: "reset", spawn });
  }

  step(dt: number, vx: number, wz: number, joints: Record<string, number>): void {
    this.#worker.postMessage({ type: "step", dt, vx, wz, joints });
  }

  /** Live-retune the arm/head PD position-servo gains and torque clamp (see apartmentWorker.ts). */
  setGains(kp: number, kd: number, effortLimit: number): void {
    this.#worker.postMessage({ type: "set_gains", kp, kd, effortLimit });
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
