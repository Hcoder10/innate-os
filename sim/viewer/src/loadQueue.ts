// Bounded-concurrency loader: cap in-flight fetches so the big Sala room can't
// starve the small ones, and we don't thrash the keep-alive-less sim server
// (every request is a fresh TLS handshake) -- while still using enough
// parallelism to fill the pipe. Jobs run FIFO, so enqueuing the robot before
// the rooms gives the robot the first slots. Byte progress from every job is
// aggregated into one overall figure for a single loading bar.
//
// The three/DOM imports below are type-only (erased at runtime), so LoadQueue
// runs in bare node -- see loadQueue.test.ts.

import type { GLTF, GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import type { Group } from "three";

export interface LoadProgress {
  /** Bytes downloaded so far, summed across every tracked job. */
  loaded: number;
  /** Best current estimate of the total: max(seeded estimate, sum of known sizes, loaded). */
  total: number;
}

/** Feed one job's download progress: bytes so far and, when known, its size. */
export type ByteReport = (loaded: number, total: number) => void;

export class LoadQueue {
  readonly #limit: number;
  readonly #onProgress?: (p: LoadProgress) => void;
  #active = 0;
  #pending: Array<() => void> = [];
  #loaded = 0;
  #estimated = 0; // seeded denominator, before any Content-Length is known
  #keyLoaded = new Map<string, number>(); // last loaded bytes reported, per job
  #keyTotal = new Map<string, number>(); // real size once its Content-Length lands

  constructor(limit = 3, onProgress?: (p: LoadProgress) => void) {
    this.#limit = Math.max(1, limit);
    this.#onProgress = onProgress;
  }

  /** Seed the denominator so the bar has a width before any bytes arrive. */
  setEstimatedTotal(bytes: number): void {
    this.#estimated = bytes;
    this.#emit();
  }

  /**
   * Enqueue a job; it runs once a slot frees (≤ limit at a time), FIFO. `job`
   * gets a reporter to feed download progress; `key` identifies the job so
   * repeated reports (loaders fire onProgress many times per file) accumulate
   * instead of double-counting.
   */
  add<T>(job: (report: ByteReport) => Promise<T>, key: string): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      const run = () => {
        this.#active++;
        const report: ByteReport = (loaded, total) => this.#report(key, loaded, total);
        job(report)
          .then(resolve, reject)
          .finally(() => {
            this.#active--;
            this.#pending.shift()?.();
          });
      };
      if (this.#active < this.#limit) run();
      else this.#pending.push(run);
    });
  }

  #report(key: string, loaded: number, total: number): void {
    const prev = this.#keyLoaded.get(key) ?? 0;
    this.#loaded += loaded - prev;
    this.#keyLoaded.set(key, loaded);
    if (total > 0) this.#keyTotal.set(key, total);
    this.#emit();
  }

  #emit(): void {
    let known = 0;
    for (const t of this.#keyTotal.values()) known += t;
    const total = Math.max(this.#estimated, known, this.#loaded);
    this.#onProgress?.({ loaded: this.#loaded, total });
  }
}

/** Load a GLB through the queue, forwarding its byte progress. Resolves the
 * root Object3D (gltf.scene). The server sends Content-Length, so ev.total is
 * a real size, not 0. */
export function queuedGLB(queue: LoadQueue, loader: GLTFLoader, url: string, key = url): Promise<Group> {
  return queue.add(
    (report) =>
      new Promise<Group>((resolve, reject) =>
        loader.load(
          url,
          (gltf: GLTF) => resolve(gltf.scene),
          (ev: ProgressEvent) => report(ev.loaded, ev.total),
          reject,
        ),
      ),
    key,
  );
}
