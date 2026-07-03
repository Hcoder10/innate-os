// LocalDriveController — input arbitration between the on-screen joystick and
// keyboard drive, ported from webapp/js/driveController.js. The wire-publish
// half of that module doesn't apply here (nothing to publish to yet — see
// Mode B); this keeps only the arbitration: the joystick wins while engaged,
// keyboard drive resumes the moment the stick is released.

export type DriveSource = "joystick" | "keyboard";

export interface DriveState {
  source: DriveSource | null;
  x: number;
  y: number;
  engaged: boolean;
}

export class LocalDriveController {
  #inputs: Record<DriveSource, { x: number; y: number; engaged: boolean }> = {
    joystick: { x: 0, y: 0, engaged: false },
    keyboard: { x: 0, y: 0, engaged: false },
  };
  #active: DriveState = { source: null, x: 0, y: 0, engaged: false };
  #listeners = new Set<(state: DriveState) => void>();

  constructor() {
    window.addEventListener("blur", () => this.haltAll());
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") this.haltAll();
    });
  }

  /** Latch an input source's vector. Robot frame: y > 0 = forward. */
  setInput(source: DriveSource, x: number, y: number, engaged: boolean): void {
    const input = this.#inputs[source];
    input.x = x;
    input.y = y;
    input.engaged = engaged;
    this.#recompute();
  }

  haltAll(): void {
    this.#inputs.joystick = { x: 0, y: 0, engaged: false };
    this.#inputs.keyboard = { x: 0, y: 0, engaged: false };
    this.#recompute();
  }

  /** Current arbitrated state (joystick > keyboard > idle). */
  get active(): DriveState {
    return this.#active;
  }

  /** Fires immediately with the current state, then on every transition. */
  onActiveChange(cb: (state: DriveState) => void): () => void {
    this.#listeners.add(cb);
    cb(this.#active);
    return () => this.#listeners.delete(cb);
  }

  #recompute(): void {
    const { joystick, keyboard } = this.#inputs;
    const source: DriveSource | null = joystick.engaged ? "joystick" : keyboard.engaged ? "keyboard" : null;
    const input = source ? this.#inputs[source] : null;
    const next: DriveState = input
      ? { source, x: input.x, y: input.y, engaged: true }
      : { source: null, x: 0, y: 0, engaged: false };
    const prev = this.#active;
    const changed =
      next.source !== prev.source || next.x !== prev.x || next.y !== prev.y || next.engaged !== prev.engaged;
    this.#active = next;
    if (changed) {
      for (const cb of [...this.#listeners]) cb(next);
    }
  }
}
