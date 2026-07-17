// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Pick tuning panel — per-stage sliders for pick_any_object's TUNABLE knobs.
// A wrench button (top-right, next to profiling) toggles it; "o" does too.
// Hidden by default so the cockpit stays clean for operators.
//
// Every slider publishes a one-key partial dict on the tuning topic; the
// running skill applies it mid-run and acks with a params debug event
// carrying its full live dict, which is what the sliders display. On open
// the panel publishes an empty dict — a pure "echo your params" request —
// so it syncs to the skill's real values (including overrides that survived
// from earlier runs) without mirroring any defaults in JS. Until that first
// ack arrives the sliders stay disabled: showing made-up numbers over a
// robot that acts on different ones is how mis-tunes happen.

import { PICK_DEBUG_TOPIC, PICK_TUNING_TOPIC } from "../constants.js";

const TOGGLE_KEY = "KeyO";

// Publishing every input event floods the topic (and the skill logs each
// applied dict); trailing-edge throttle per drag, plus a final on change.
const SLIDE_PUBLISH_MS = 125;

/**
 * Slider ranges per TUNABLE key, grouped by the same stages as the dict in
 * pick_any_object.py. Ranges/steps are UI affordances chosen around the
 * defaults and hardware limits (reach box, head tilt, servo trip points) —
 * the VALUES always come from the skill's params broadcasts, never from here.
 * @type {{ stage: string, keys: { [key: string]: [number, number, number] } }[]}
 */
const STAGES = [
  {
    stage: "find / localize",
    keys: {
      tilt_deg: [-40, 0, 1],
      settle_s: [0, 3, 0.1],
    },
  },
  {
    stage: "position",
    keys: {
      sweet_x: [0.2, 0.45, 0.005],
      box_y: [-0.1, 0.1, 0.005],
      box_half_px: [10, 120, 5],
      accept_frac: [0.2, 1, 0.05],
      box_steps: [1, 12, 1],
      bearing_go_deg: [1, 15, 0.5],
      follow_gain_ang: [0, 1, 0.05],
      follow_gain_lin: [0, 0.3, 0.01],
    },
  },
  {
    stage: "base odometry",
    keys: {
      rot_tol_deg: [0.5, 10, 0.5],
      rot_kp: [0.1, 3, 0.1],
      rot_wz_max: [0.1, 1.5, 0.05],
      rot_wz_min: [0.05, 0.5, 0.01],
      drive_tol_m: [0.005, 0.05, 0.005],
      drive_kp: [0.05, 1, 0.05],
      drive_v_max: [0.02, 0.3, 0.01],
      drive_v_min: [0.01, 0.1, 0.005],
    },
  },
  {
    stage: "wrist align",
    keys: {
      wrist_steps: [0, 6, 1],
      wrist_stop_z: [0.02, 0.12, 0.005],
      wrist_z_step: [0.005, 0.04, 0.005],
      wrist_move_s: [0.2, 2, 0.1],
      wrist_pitch: [0.3, 1.6, 0.02],
      wrist_box_u: [0, 640, 5],
      wrist_box_v: [0, 480, 5],
      wrist_half_px: [20, 160, 5],
      wrist_kx: [-0.2, 0.2, 0.005],
      wrist_ky: [-0.2, 0.2, 0.005],
      wrist_step_max: [0.01, 0.1, 0.005],
      wrist_settle_s: [0.2, 2, 0.1],
    },
  },
  {
    stage: "grasp",
    keys: {
      grasp_x_off: [0, 0.08, 0.005],
      hover_z: [0.08, 0.25, 0.005],
      hover_s: [0.5, 4, 0.25],
      descend_z1: [0.04, 0.15, 0.005],
      descend_z2: [0.03, 0.12, 0.005],
      descend_z3: [0.01, 0.08, 0.005],
      floor_z: [0, 0.03, 0.002],
      descend_s: [0.4, 3, 0.1],
      descend_abort_z: [0.05, 0.2, 0.005],
      arm_pitch: [0.8, 1.6, 0.02],
      close_strength: [0, 0.8, 0.05],
      close_s: [0.5, 3, 0.1],
      close_settle_s: [0, 2, 0.1],
      twist_rad: [0, 1.4, 0.05],
      lift_rad: [0, 1.4, 0.05],
    },
  },
];

/** Display decimals for a slider step (0.005 -> 3, 0.1 -> 1, 1 -> 0).
 *  @param {number} step */
function decimalsFor(step) {
  if (step >= 1) return 0;
  if (step >= 0.1) return 1;
  if (step >= 0.01) return 2;
  return 3;
}

/**
 * @param {HTMLElement} parent cockpit root — owns the toggle button + panel.
 * @param {import("../rosClient.js").RosClient} rosClient
 * @returns {{ destroy: () => void }}
 */
export function createPickPanel(parent, rosClient) {
  // Discrete toggle: a small glass wrench button, left of the profiling one.
  const toggleBtn = document.createElement("button");
  toggleBtn.type = "button";
  toggleBtn.className = "profiling-toggle picktune-panel-toggle";
  toggleBtn.title = "Pick tuning (o)";
  toggleBtn.setAttribute("aria-label", "Toggle pick tuning panel");
  toggleBtn.innerHTML =
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" ' +
    'stroke-width="2" stroke-linecap="round" aria-hidden="true">' +
    '<line x1="4" y1="7" x2="20" y2="7"/><circle cx="9" cy="7" r="2.2" fill="var(--glass)"/>' +
    '<line x1="4" y1="12" x2="20" y2="12"/><circle cx="15" cy="12" r="2.2" fill="var(--glass)"/>' +
    '<line x1="4" y1="17" x2="20" y2="17"/><circle cx="7" cy="17" r="2.2" fill="var(--glass)"/></svg>';

  const wrap = document.createElement("div");
  wrap.className = "picktune-panel";
  wrap.hidden = true;

  const head = document.createElement("div");
  head.className = "profiling-head";
  const title = document.createElement("span");
  title.className = "microlabel";
  title.textContent = "pick tuning";
  const hint = document.createElement("span");
  hint.className = "profiling-hint mono";
  hint.textContent = "o";
  head.append(title, hint);

  // Sync status: cleared by the first params ack from the skill.
  const note = document.createElement("div");
  note.className = "picktune-panel-note";
  note.textContent = "waiting for skill…";

  const body = document.createElement("div");
  body.className = "picktune-panel-body";

  wrap.append(head, note, body);
  parent.append(toggleBtn, wrap);

  /** @type {Map<string, { input: HTMLInputElement, value: HTMLElement, decimals: number }>} */
  const rows = new Map();
  let synced = false;
  /** Key being actively dragged — its row ignores param echoes so the thumb
   *  doesn't fight the ack of an in-flight value. @type {string | null} */
  let sliding = null;
  let lastSlidePublish = 0;

  for (const { stage, keys } of STAGES) {
    const groupTitle = document.createElement("div");
    groupTitle.className = "picktune-panel-stage microlabel";
    groupTitle.textContent = stage;
    body.append(groupTitle);

    for (const [key, [min, max, step]] of Object.entries(keys)) {
      const row = document.createElement("div");
      row.className = "picktune-panel-row";

      const label = document.createElement("span");
      label.className = "picktune-panel-key mono";
      label.textContent = key;

      const input = document.createElement("input");
      input.type = "range";
      input.min = String(min);
      input.max = String(max);
      input.step = String(step);
      input.disabled = true; // until the skill's first params ack

      const value = document.createElement("span");
      value.className = "picktune-panel-value mono";
      value.textContent = "—";

      const decimals = decimalsFor(step);
      rows.set(key, { input, value, decimals });

      input.addEventListener("input", () => {
        sliding = key;
        value.textContent = Number(input.value).toFixed(decimals);
        const now = performance.now();
        if (now - lastSlidePublish >= SLIDE_PUBLISH_MS) {
          lastSlidePublish = now;
          publishOverride(key, Number(input.value));
        }
      });
      // Release: the definitive value always lands, throttle or not.
      input.addEventListener("change", () => {
        sliding = null;
        publishOverride(key, Number(input.value));
      });

      row.append(label, input, value);
      body.append(row);
    }
  }

  /** @param {string} key @param {number} val */
  function publishOverride(key, val) {
    rosClient.publish(PICK_TUNING_TOPIC, { data: JSON.stringify({ [key]: val }) });
  }

  /** Ask the running skill to echo its live params (an empty partial dict
   *  applies nothing but still gets the params ack). */
  function requestSync() {
    rosClient.publish(PICK_TUNING_TOPIC, { data: "{}" });
  }

  /** Fold a params broadcast into the sliders. @param {any} params */
  function syncSliders(params) {
    if (!params || typeof params !== "object") return;
    for (const [key, row] of rows) {
      if (typeof params[key] !== "number" || key === sliding) continue;
      row.input.value = String(params[key]);
      row.input.disabled = false;
      row.value.textContent = params[key].toFixed(row.decimals);
    }
    if (!synced) {
      synced = true;
      note.hidden = true;
    }
  }

  // Params ride the same debug topic as the overlay events; the panel only
  // cares about the dict-bearing ones (run_start and the params acks).
  const unsubDebug = rosClient.subscribe(PICK_DEBUG_TOPIC, (msg) => {
    if (typeof msg?.data !== "string") return;
    /** @type {any} */ let ev;
    try {
      ev = JSON.parse(msg.data);
    } catch {
      return; // not JSON — ignore
    }
    if (ev.ev === "params" || ev.ev === "run_start") syncSliders(ev.params);
  });

  /** @param {boolean} open */
  function setOpen(open) {
    wrap.hidden = !open;
    toggleBtn.classList.toggle("active", open);
    toggleBtn.setAttribute("aria-pressed", String(open));
    if (open && !synced) requestSync();
  }

  function toggle() {
    setOpen(Boolean(wrap.hidden));
  }

  toggleBtn.addEventListener("click", toggle);

  /** @param {KeyboardEvent} e */
  function onKeyDown(e) {
    if (e.code !== TOGGLE_KEY || e.repeat || e.metaKey || e.ctrlKey || e.altKey) return;
    const el = document.activeElement;
    if (el instanceof HTMLElement && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable)) {
      return;
    }
    toggle();
  }

  window.addEventListener("keydown", onKeyDown);

  return {
    destroy() {
      unsubDebug();
      window.removeEventListener("keydown", onKeyDown);
      toggleBtn.remove();
      wrap.remove();
    },
  };
}
