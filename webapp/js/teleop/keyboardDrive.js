// @ts-check
// Keyboard drive: WASD/arrows with ramping, so taps nudge and holds glide.
//
// W/↑ = forward, S/↓ = back, A/← = left, D/→ = right. Opposing keys cancel,
// the combined vector is clamped to unit magnitude, Shift scales to 0.4 for
// precision work. A 50 ms tick ramps toward the target (~350 ms up, ~150 ms
// down); when no keys are held and the vector decays below epsilon the loop
// stops and the source disengages (DriveController emits the single zero).
//
// Guards: key repeats ignored, typing contexts (TTS bar) ignored, arrows
// preventDefault'ed so the page never scrolls, and blur / hidden-tab clears
// held keys immediately — Cmd+Tab eats keyup events, so a latched key must
// never survive focus loss.

const TICK_MS = 50;
const RAMP_UP_MS = 350;
const RAMP_DOWN_MS = 150;
const PRECISION_SCALE = 0.4;
const EPSILON = 0.02;

/** @type {Map<string, "up" | "down" | "left" | "right">} */
const KEY_DIRS = new Map([
  ["KeyW", "up"],
  ["ArrowUp", "up"],
  ["KeyS", "down"],
  ["ArrowDown", "down"],
  ["KeyA", "left"],
  ["ArrowLeft", "left"],
  ["KeyD", "right"],
  ["ArrowRight", "right"],
]);

function isTypingContext() {
  const el = document.activeElement;
  return (
    el instanceof HTMLElement &&
    (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable)
  );
}

/**
 * @param {import("../driveController.js").DriveController} driveController
 * @returns {{ onChange: (cb: (s: KeyboardDriveState) => void) => () => void, destroy: () => void }}
 */
export function createKeyboardDrive(driveController) {
  /** @type {Set<"up" | "down" | "left" | "right">} */
  const held = new Set();
  let shift = false;
  let vx = 0;
  let vy = 0;
  let engaged = false;
  /** @type {number | null} */
  let timer = null;
  /** @type {Set<(s: KeyboardDriveState) => void>} */
  const listeners = new Set();

  function emit() {
    /** @type {KeyboardDriveState} */
    const snapshot = {
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

  function targetVector() {
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

  /**
   * Move one axis toward its target by the ramp rate for this tick.
   * @param {number} current
   * @param {number} target
   */
  function approach(current, target) {
    const rampMs = Math.abs(target) > Math.abs(current) ? RAMP_UP_MS : RAMP_DOWN_MS;
    const step = TICK_MS / rampMs;
    const delta = target - current;
    if (Math.abs(delta) <= step) return target;
    return current + Math.sign(delta) * step;
  }

  function tick() {
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

  function startLoop() {
    if (timer !== null) return;
    engaged = true;
    timer = setInterval(tick, TICK_MS);
    tick();
  }

  /** Stop the loop and disengage (DriveController publishes the one zero). */
  function stopLoop() {
    if (timer !== null) {
      clearInterval(timer);
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

  /** Immediate halt: focus loss may have eaten the keyup. */
  function clearHeld() {
    held.clear();
    shift = false;
    stopLoop();
  }

  /** @param {KeyboardEvent} e */
  function onKeyDown(e) {
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

  /** @param {KeyboardEvent} e */
  function onKeyUp(e) {
    // No typing guard here: a key pressed before focusing an input must
    // still release. The ramp-down loop handles the rest.
    if (e.key === "Shift") shift = false;
    const dir = KEY_DIRS.get(e.code);
    if (dir) held.delete(dir);
    emit();
  }

  function onBlurOrHide() {
    clearHeld();
    driveController.haltAll();
  }

  function onVisibility() {
    if (document.visibilityState === "hidden") onBlurOrHide();
  }

  function onFocusIn() {
    // Keys held while the operator clicks into the TTS bar must not keep
    // driving under the typing.
    if (isTypingContext() && held.size > 0) clearHeld();
  }

  window.addEventListener("keydown", onKeyDown);
  window.addEventListener("keyup", onKeyUp);
  window.addEventListener("blur", onBlurOrHide);
  document.addEventListener("visibilitychange", onVisibility);
  document.addEventListener("focusin", onFocusIn);

  return {
    /**
     * @param {(s: KeyboardDriveState) => void} cb
     * @returns {() => void} unsubscribe
     */
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

/**
 * WASD hint chips (inverted-T) that light while keys are held and tint
 * amber when keyboard drive is actually moving the robot.
 * @param {HTMLElement} parent
 * @param {{ onChange: (cb: (s: KeyboardDriveState) => void) => () => void }} keyboardDrive
 * @returns {{ destroy: () => void }}
 */
export function createWasdChips(parent, keyboardDrive) {
  const wrap = document.createElement("div");
  wrap.className = "wasd";
  wrap.setAttribute("aria-hidden", "true");

  /** @type {Record<"up" | "left" | "down" | "right", HTMLElement>} */
  const chips = {
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

  /** @param {string} label */
  function chip(label) {
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
