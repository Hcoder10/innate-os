// Keyboard drive — ported from webapp/js/teleop/keyboardDrive.js.
//
// W/↑ = forward, S/↓ = back, A/← = left, D/→ = right. Opposing keys cancel,
// the combined vector is clamped to unit magnitude, Shift scales to 0.75 for
// precision work. A 50ms tick ramps toward the target (~350ms up, ~150ms
// down); when no keys are held and the vector decays below epsilon the loop
// stops and the source disengages (LocalDriveController emits the single
// zero).
//
// Guards: key repeats ignored, typing contexts ignored, arrows
// preventDefault'ed so the page never scrolls, and blur / hidden-tab clears
// held keys immediately.

import type { LocalDriveController } from "./driveController";

const TICK_MS = 50;
const RAMP_UP_MS = 350;
const RAMP_DOWN_MS = 150;
const PRECISION_SCALE = 0.75;
const EPSILON = 0.02;

type Direction = "up" | "down" | "left" | "right";

const KEY_DIRS = new Map<string, Direction>([
  ["KeyW", "up"],
  ["ArrowUp", "up"],
  ["KeyS", "down"],
  ["ArrowDown", "down"],
  ["KeyA", "left"],
  ["ArrowLeft", "left"],
  ["KeyD", "right"],
  ["ArrowRight", "right"],
]);

export interface KeyboardDriveState {
  held: Record<Direction, boolean>;
  x: number;
  y: number;
  engaged: boolean;
}

function isTypingContext(): boolean {
  const el = document.activeElement;
  return el instanceof HTMLElement && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable);
}

export interface KeyboardDrive {
  onChange(cb: (s: KeyboardDriveState) => void): () => void;
  destroy(): void;
}

export function createKeyboardDrive(driveController: LocalDriveController): KeyboardDrive {
  const held = new Set<Direction>();
  let shift = false;
  let vx = 0;
  let vy = 0;
  let engaged = false;
  let timer: number | null = null;
  const listeners = new Set<(s: KeyboardDriveState) => void>();

  function emit(): void {
    const snapshot: KeyboardDriveState = {
      held: {
        up: held.has("up"),
        down: held.has("down"),
        left: held.has("left"),
        right: held.has("right"),
      },
      x: vx,
      y: vy,
      engaged,
    };
    for (const cb of [...listeners]) cb(snapshot);
  }

  function targetVector(): { x: number; y: number } {
    let tx = (held.has("right") ? 1 : 0) - (held.has("left") ? 1 : 0);
    let ty = (held.has("up") ? 1 : 0) - (held.has("down") ? 1 : 0);
    const mag = Math.hypot(tx, ty);
    if (mag > 1) {
      tx /= mag;
      ty /= mag;
    }
    const scale = shift ? PRECISION_SCALE : 1;
    return { x: tx * scale, y: ty * scale };
  }

  function approach(current: number, target: number): number {
    const rampMs = Math.abs(target) > Math.abs(current) ? RAMP_UP_MS : RAMP_DOWN_MS;
    const step = TICK_MS / rampMs;
    const delta = target - current;
    if (Math.abs(delta) <= step) return target;
    return current + Math.sign(delta) * step;
  }

  function tick(): void {
    const target = targetVector();
    vx = approach(vx, target.x);
    vy = approach(vy, target.y);

    if (held.size === 0 && Math.hypot(vx, vy) < EPSILON) {
      stopLoop();
      return;
    }
    driveController.setInput("keyboard", vx, vy, true);
    emit();
  }

  function startLoop(): void {
    if (timer !== null) return;
    engaged = true;
    timer = window.setInterval(tick, TICK_MS);
    tick();
  }

  function stopLoop(): void {
    if (timer !== null) {
      window.clearInterval(timer);
      timer = null;
    }
    vx = 0;
    vy = 0;
    if (engaged) {
      engaged = false;
      driveController.setInput("keyboard", 0, 0, false);
    }
    emit();
  }

  function clearHeld(): void {
    held.clear();
    shift = false;
    stopLoop();
  }

  function onKeyDown(e: KeyboardEvent): void {
    if (e.key === "Shift") {
      shift = true;
      return;
    }
    const dir = KEY_DIRS.get(e.code);
    if (!dir) return;
    if (isTypingContext()) return;
    if (e.code.startsWith("Arrow")) e.preventDefault();
    if (e.repeat) return;
    shift = e.shiftKey;
    held.add(dir);
    startLoop();
  }

  function onKeyUp(e: KeyboardEvent): void {
    if (e.key === "Shift") shift = false;
    const dir = KEY_DIRS.get(e.code);
    if (dir) held.delete(dir);
    emit();
  }

  function onBlurOrHide(): void {
    clearHeld();
    driveController.haltAll();
  }

  function onVisibility(): void {
    if (document.visibilityState === "hidden") onBlurOrHide();
  }

  function onFocusIn(): void {
    if (isTypingContext() && held.size > 0) clearHeld();
  }

  window.addEventListener("keydown", onKeyDown);
  window.addEventListener("keyup", onKeyUp);
  window.addEventListener("blur", onBlurOrHide);
  document.addEventListener("visibilitychange", onVisibility);
  document.addEventListener("focusin", onFocusIn);

  return {
    onChange(cb) {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    destroy() {
      clearHeld();
      listeners.clear();
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
      window.removeEventListener("blur", onBlurOrHide);
      document.removeEventListener("visibilitychange", onVisibility);
      document.removeEventListener("focusin", onFocusIn);
    },
  };
}

/** WASD hint chips (inverted-T) that light while keys are held and tint
 *  amber (accent) when keyboard drive is actually moving the robot. */
export function createWasdChips(parent: HTMLElement, keyboardDrive: KeyboardDrive): { destroy: () => void } {
  const wrap = document.createElement("div");
  wrap.className = "wasd";
  wrap.setAttribute("aria-hidden", "true");

  const chips: Record<"up" | "left" | "down" | "right", HTMLElement> = {
    up: chip("W"),
    left: chip("A"),
    down: chip("S"),
    right: chip("D"),
  };
  const top = document.createElement("div");
  top.className = "wasd-row";
  top.append(chips.up);
  const bottom = document.createElement("div");
  bottom.className = "wasd-row";
  bottom.append(chips.left, chips.down, chips.right);
  wrap.append(top, bottom);

  const hint = document.createElement("div");
  hint.className = "microlabel wasd-hint";
  hint.textContent = "shift = slow";
  wrap.append(hint);

  function chip(label: string): HTMLElement {
    const el = document.createElement("span");
    el.className = "wasd-chip";
    el.textContent = label;
    return el;
  }

  const unsub = keyboardDrive.onChange((s) => {
    chips.up.classList.toggle("held", s.held.up);
    chips.down.classList.toggle("held", s.held.down);
    chips.left.classList.toggle("held", s.held.left);
    chips.right.classList.toggle("held", s.held.right);
    wrap.classList.toggle("driving", s.engaged);
  });

  parent.appendChild(wrap);
  return {
    destroy() {
      unsub();
      wrap.remove();
    },
  };
}
