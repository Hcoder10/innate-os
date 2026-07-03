// On-screen joystick — ported from webapp/js/teleop/joystick.js. Pointer
// events with capture give one code path for mouse + touch. Values are
// normalized to [-1, 1] and flipped into robot frame (screen y grows
// downward) before setInput.

import type { LocalDriveController } from "./driveController";

const SIZE = 180;
const CENTER = SIZE / 2;
const OUTER_R = 84;
const KNOB_R = 13;
const MAX_DIST = OUTER_R - KNOB_R;
const SVG_NS = "http://www.w3.org/2000/svg";

function svgEl<K extends keyof SVGElementTagNameMap>(
  tag: K,
  attrs: Record<string, string>,
): SVGElementTagNameMap[K] {
  const el = document.createElementNS(SVG_NS, tag) as SVGElementTagNameMap[K];
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

export function createJoystick(parent: HTMLElement, driveController: LocalDriveController): { destroy: () => void } {
  const svg = svgEl("svg", {
    class: "joystick",
    viewBox: `0 0 ${SIZE} ${SIZE}`,
    width: String(SIZE),
    height: String(SIZE),
    role: "application",
    "aria-label": "Drive joystick",
  });

  svg.appendChild(svgEl("circle", { class: "joy-rim", cx: String(CENTER), cy: String(CENTER), r: String(OUTER_R) }));

  const tickIn = OUTER_R - 7;
  const tickOut = OUTER_R - 1;
  for (const [dx, dy] of [
    [0, -1],
    [0, 1],
    [-1, 0],
    [1, 0],
  ]) {
    svg.appendChild(
      svgEl("line", {
        class: "joy-tick",
        x1: String(CENTER + dx * tickIn),
        y1: String(CENTER + dy * tickIn),
        x2: String(CENTER + dx * tickOut),
        y2: String(CENTER + dy * tickOut),
      }),
    );
  }

  const trace = svgEl("line", {
    class: "joy-trace",
    x1: String(CENTER),
    y1: String(CENTER),
    x2: String(CENTER),
    y2: String(CENTER),
  });
  svg.appendChild(trace);

  const knobGroup = svgEl("g", { class: "joy-knob-group" });
  knobGroup.appendChild(svgEl("circle", { class: "joy-knob", cx: String(CENTER), cy: String(CENTER), r: String(KNOB_R) }));
  svg.appendChild(knobGroup);

  let pointerEngaged = false;

  function setKnob(dx: number, dy: number): void {
    knobGroup.style.transform = `translate(${dx}px, ${dy}px)`;
    trace.setAttribute("x2", String(CENTER + dx));
    trace.setAttribute("y2", String(CENTER + dy));
    svg.classList.toggle("at-edge", Math.hypot(dx, dy) >= MAX_DIST - 0.5);
  }

  function displacementFrom(e: PointerEvent): { dx: number; dy: number } {
    const rect = svg.getBoundingClientRect();
    const scale = SIZE / rect.width;
    let dx = (e.clientX - rect.left - rect.width / 2) * scale;
    let dy = (e.clientY - rect.top - rect.height / 2) * scale;
    const dist = Math.hypot(dx, dy);
    if (dist > MAX_DIST && dist > 0) {
      dx *= MAX_DIST / dist;
      dy *= MAX_DIST / dist;
    }
    return { dx, dy };
  }

  function update(e: PointerEvent): void {
    const { dx, dy } = displacementFrom(e);
    setKnob(dx, dy);
    driveController.setInput("joystick", dx / MAX_DIST, -dy / MAX_DIST, true);
  }

  function onPointerDown(e: PointerEvent): void {
    if (pointerEngaged) return;
    pointerEngaged = true;
    try {
      svg.setPointerCapture(e.pointerId);
    } catch {
      // Capture can fail for synthetic/stale pointers; degrade gracefully.
    }
    svg.classList.add("engaged");
    update(e);
  }

  function onPointerMove(e: PointerEvent): void {
    if (pointerEngaged) update(e);
  }

  function release(): void {
    if (!pointerEngaged) return;
    pointerEngaged = false;
    svg.classList.remove("engaged");
    setKnob(0, 0);
    driveController.setInput("joystick", 0, 0, false);
  }

  svg.addEventListener("pointerdown", onPointerDown);
  svg.addEventListener("pointermove", onPointerMove);
  svg.addEventListener("pointerup", release);
  svg.addEventListener("pointercancel", release);
  svg.addEventListener("lostpointercapture", release);

  // Mirror keyboard drive so the knob always shows what the robot was told.
  const unsubActive = driveController.onActiveChange((state) => {
    if (pointerEngaged) return;
    svg.classList.toggle("mirroring", state.source === "keyboard");
    if (state.source === "keyboard") {
      setKnob(state.x * MAX_DIST, -state.y * MAX_DIST);
    } else if (state.source === null) {
      setKnob(0, 0);
    }
  });

  parent.appendChild(svg);

  return {
    destroy() {
      release();
      unsubActive();
      svg.remove();
    },
  };
}
