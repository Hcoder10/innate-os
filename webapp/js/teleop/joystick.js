// @ts-check
// On-screen joystick. Pointer events with capture give one code path for
// mouse + touch (touch-action: none in CSS). The math is ported from the
// mobile stick — knob clamped to outer radius minus knob radius, values
// normalized to that distance, -y flip into robot frame before setInput —
// but the rendering is original: a hairline rim with four cardinal ticks,
// a small dot for the knob, a faint displacement trace while engaged.
//
// The heartbeat lives in DriveController, NOT here: this module only latches
// values and reports engage/release.

const SIZE = 180;
const CENTER = SIZE / 2;
const OUTER_R = 84;
const KNOB_R = 13;
const MAX_DIST = OUTER_R - KNOB_R;
const SVG_NS = "http://www.w3.org/2000/svg";

/**
 * @param {string} tag
 * @param {Record<string, string>} attrs
 */
function svgEl(tag, attrs) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

/**
 * @param {HTMLElement} parent
 * @param {import("../driveController.js").DriveController} driveController
 * @returns {{ destroy: () => void }}
 */
export function createJoystick(parent, driveController) {
  const svg = /** @type {SVGSVGElement} */ (
    svgEl("svg", {
      class: "joystick",
      viewBox: `0 0 ${SIZE} ${SIZE}`,
      width: String(SIZE),
      height: String(SIZE),
      role: "application",
      "aria-label": "Drive joystick",
    })
  );

  svg.appendChild(svgEl("circle", { class: "joy-rim", cx: String(CENTER), cy: String(CENTER), r: String(OUTER_R) }));

  // Four faint cardinal ticks just inside the rim.
  const tickIn = OUTER_R - 7;
  const tickOut = OUTER_R - 1;
  for (const [dx, dy] of [[0, -1], [0, 1], [-1, 0], [1, 0]]) {
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

  // Knob in a translated group so CSS can transition the release snap-back.
  const knobGroup = /** @type {SVGGElement} */ (svgEl("g", { class: "joy-knob-group" }));
  knobGroup.appendChild(svgEl("circle", { class: "joy-knob", cx: String(CENTER), cy: String(CENTER), r: String(KNOB_R) }));
  svg.appendChild(knobGroup);

  let pointerEngaged = false;

  /**
   * Place the knob at a screen-frame displacement from center (clamped).
   * @param {number} dx
   * @param {number} dy
   */
  function setKnob(dx, dy) {
    knobGroup.style.transform = `translate(${dx}px, ${dy}px)`;
    trace.setAttribute("x2", String(CENTER + dx));
    trace.setAttribute("y2", String(CENTER + dy));
    svg.classList.toggle("at-edge", Math.hypot(dx, dy) >= MAX_DIST - 0.5);
  }

  /**
   * @param {PointerEvent} e
   * @returns {{ dx: number, dy: number }} clamped screen-frame displacement
   */
  function displacementFrom(e) {
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

  /** @param {PointerEvent} e */
  function update(e) {
    const { dx, dy } = displacementFrom(e);
    setKnob(dx, dy);
    // Normalize and flip into robot frame (screen y grows downward).
    driveController.setInput("joystick", dx / MAX_DIST, -dy / MAX_DIST, true);
  }

  /** @param {PointerEvent} e */
  function onPointerDown(e) {
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

  /** @param {PointerEvent} e */
  function onPointerMove(e) {
    if (pointerEngaged) update(e);
  }

  function release() {
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
