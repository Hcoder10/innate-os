// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Experiments panel — record named trajectory runs and overlay them on the
// map for side-by-side comparison (A/B tuning sessions). Each run stores the
// composed map-frame breadcrumb from the map widget plus the controller
// params active at the time, and persists in localStorage per map.

import { ros } from "../rosClient.js";

const STORE_KEY = "innate.navExperiments";
// Distinct on the map's grey ground; assigned round-robin at save time.
const RUN_COLORS = ["#2E7DD1", "#D97706", "#C0453C", "#2F9E63", "#8B5CF6", "#DB2777", "#0891B2", "#854D0E"];

/** @typedef {{ name: string, color: string, map: string | null, date: string, params: Record<string, number> | null, points: Array<{ x: number, y: number }>, visible: boolean }} Run */

/** @returns {Run[]} */
function load() {
  try {
    return JSON.parse(localStorage.getItem(STORE_KEY) || "[]");
  } catch {
    return [];
  }
}

/** @param {Run[]} runs */
function save(runs) {
  localStorage.setItem(STORE_KEY, JSON.stringify(runs));
}

/** Best-effort snapshot of the MPPI knobs so a run is self-describing. */
async function fetchParams() {
  try {
    const names = [
      "InnateFollowPath.az_max",
      "InnateFollowPath.ax_max",
      "InnateFollowPath.control_delay_steps",
    ];
    const res = await ros.callService("/controller_server/get_parameters", { names }, 5000);
    const vals = res?.values;
    if (!Array.isArray(vals) || vals.length !== 3) return null;
    const num = (/** @type {any} */ v) => (v.type === 2 ? v.integer_value : v.double_value);
    return { az_max: num(vals[0]), ax_max: num(vals[1]), delay_steps: num(vals[2]) };
  } catch {
    return null;
  }
}

/** @param {Run} run */
function runMeta(run) {
  const m = [];
  if (run.params) {
    const az = run.params.az_max;
    m.push(az > 5 ? "az=off" : `az=${az}`, `delay=${run.params.delay_steps}`);
  }
  const len = pathLen(run.points);
  m.push(`${len.toFixed(1)} m`);
  return m.join(" · ");
}

/** @param {Array<{ x: number, y: number }>} pts */
function pathLen(pts) {
  let l = 0;
  for (let i = 1; i < pts.length; i++) l += Math.hypot(pts[i].x - pts[i - 1].x, pts[i].y - pts[i - 1].y);
  return l;
}

/**
 * @param {HTMLElement} host
 * @param {ReturnType<import("./navStore.js").createNavStore>} store
 * @param {any} map the map widget (start/stopRecording, setRecordings)
 * @returns {{ destroy: () => void }}
 */
export function createExperimentsPanel(host, store, map) {
  const section = document.createElement("section");
  section.className = "nav-panel";
  const head = document.createElement("div");
  head.className = "telemetry-head";
  const label = document.createElement("span");
  label.className = "microlabel";
  label.textContent = "Experiments";
  const exportBtn = document.createElement("button");
  exportBtn.type = "button";
  exportBtn.className = "exp-export";
  exportBtn.textContent = "Export";
  exportBtn.title = "Download this map's recorded runs as JSON";
  head.append(label, exportBtn);

  const recRow = document.createElement("div");
  recRow.className = "exp-rec-row";
  const nameInput = document.createElement("input");
  nameInput.type = "text";
  nameInput.className = "exp-name";
  nameInput.placeholder = "run name (e.g. bare, accel-0.5)";
  const recBtn = document.createElement("button");
  recBtn.type = "button";
  recBtn.className = "exp-record";
  recBtn.textContent = "Record";
  recRow.append(nameInput, recBtn);

  // az_max presets: flip the sweep knob without leaving the page. Highlights
  // follow the controller's actual value (re-fetched after every set).
  const cfgRow = document.createElement("div");
  cfgRow.className = "exp-cfg-row";
  const cfgLabel = document.createElement("span");
  cfgLabel.className = "exp-cfg-label mono";
  cfgLabel.textContent = "az";
  cfgRow.appendChild(cfgLabel);
  const AZ_PRESETS = [
    { label: "0.5", value: 0.5 },
    { label: "1.0", value: 1.0 },
    { label: "2.0", value: 2.0 },
    { label: "off", value: 10.0 },
  ];
  /** @type {Map<number, HTMLButtonElement>} */
  const azBtns = new Map();
  for (const { label: l, value: v } of AZ_PRESETS) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "exp-az";
    b.textContent = l;
    b.title = `Set InnateFollowPath.az_max = ${v}`;
    b.addEventListener("click", () => setAz(v));
    azBtns.set(v, b);
    cfgRow.appendChild(b);
  }

  // Custom value: type any az_max and submit (Enter works too).
  const customInput = document.createElement("input");
  customInput.type = "number";
  customInput.step = "0.1";
  customInput.min = "0.1";
  customInput.className = "exp-az-custom";
  customInput.placeholder = "0.7";
  customInput.title = "Custom az_max (rad/s²)";
  const customBtn = document.createElement("button");
  customBtn.type = "button";
  customBtn.className = "exp-az";
  customBtn.textContent = "set";
  function submitCustom() {
    const v = parseFloat(customInput.value);
    if (!Number.isFinite(v) || v <= 0) return;
    setAz(v);
  }
  customBtn.addEventListener("click", submitCustom);
  customInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") submitCustom();
  });
  cfgRow.append(customInput, customBtn);

  async function syncAz() {
    const p = await fetchParams();
    for (const [v, b] of azBtns) {
      b.classList.toggle("is-active", p != null && Math.abs(p.az_max - v) < 1e-6);
    }
    const isPreset = p != null && AZ_PRESETS.some(({ value: v }) => Math.abs(p.az_max - v) < 1e-6);
    customBtn.classList.toggle("is-active", p != null && !isPreset);
    if (p == null) cfgLabel.textContent = "az ?";
    else cfgLabel.textContent = `az ${p.az_max > 5 ? "off" : p.az_max}`;
  }

  /** @param {number} v */
  async function setAz(v) {
    try {
      await ros.callService(
        "/controller_server/set_parameters",
        { parameters: [{ name: "InnateFollowPath.az_max", value: { type: 3, double_value: v } }] },
        5000,
      );
    } catch {
      // fall through — syncAz shows whatever the controller actually has
    }
    syncAz();
  }

  const status = document.createElement("div");
  status.className = "exp-status mono";
  status.hidden = true;

  const list = document.createElement("div");
  list.className = "exp-list";

  section.append(head, cfgRow, recRow, status, list);
  host.appendChild(section);
  syncAz();

  let runs = load();
  let recording = false;
  let recStart = 0;
  /** @type {ReturnType<typeof setInterval> | null} */
  let tick = null;

  const currentMap = () => store.state.currentMap ?? null;
  const mapRuns = () => runs.filter((r) => r.map === currentMap());

  function pushOverlays() {
    map.setRecordings(mapRuns().map((r) => ({ color: r.color, points: r.points, visible: r.visible })));
  }

  function renderList() {
    list.innerHTML = "";
    const rs = mapRuns();
    if (!rs.length) {
      const empty = document.createElement("div");
      empty.className = "maps-empty";
      empty.textContent = "no recorded runs on this map";
      list.appendChild(empty);
      return;
    }
    for (const run of rs) {
      const row = document.createElement("div");
      row.className = "exp-row";
      const swatch = document.createElement("span");
      swatch.className = "exp-swatch";
      swatch.style.background = run.color;
      const name = document.createElement("span");
      name.className = "exp-row-name";
      name.textContent = run.name;
      name.title = `${run.date} · ${runMeta(run)} · ${run.points.length} pts`;
      const meta = document.createElement("span");
      meta.className = "exp-row-meta mono";
      meta.textContent = runMeta(run);
      const eye = document.createElement("button");
      eye.type = "button";
      eye.className = "exp-eye";
      eye.textContent = run.visible ? "◉" : "◌";
      eye.title = run.visible ? "Hide this run" : "Show this run";
      eye.addEventListener("click", () => {
        run.visible = !run.visible;
        save(runs);
        renderList();
        pushOverlays();
      });
      const del = document.createElement("button");
      del.type = "button";
      del.className = "exp-del";
      del.textContent = "✕";
      del.title = "Delete this run";
      del.addEventListener("click", () => {
        runs = runs.filter((r) => r !== run);
        save(runs);
        renderList();
        pushOverlays();
      });
      row.append(swatch, name, meta, eye, del);
      list.appendChild(row);
    }
  }

  function setRecordingUi(on) {
    recording = on;
    recBtn.textContent = on ? "Stop & save" : "Record";
    recBtn.classList.toggle("is-recording", on);
    nameInput.disabled = on;
    status.hidden = !on;
  }

  async function start() {
    const color = RUN_COLORS[runs.length % RUN_COLORS.length];
    recStart = Date.now();
    map.startRecording(color);
    setRecordingUi(true);
    tick = setInterval(() => {
      const s = Math.round((Date.now() - recStart) / 1000);
      status.textContent = `recording · ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")} · ${map.recordingPointCount()} pts`;
    }, 1000);
  }

  async function stop() {
    if (tick) clearInterval(tick);
    tick = null;
    const points = map.stopRecording();
    setRecordingUi(false);
    if (points.length < 2) return; // nothing moved — drop silently
    const params = await fetchParams();
    const name = nameInput.value.trim() || `run ${new Date().toLocaleTimeString()}`;
    runs.push({
      name,
      color: RUN_COLORS[runs.length % RUN_COLORS.length],
      map: currentMap(),
      date: new Date().toISOString().slice(0, 16).replace("T", " "),
      params,
      points,
      visible: true,
    });
    save(runs);
    nameInput.value = "";
    renderList();
    pushOverlays();
  }

  recBtn.addEventListener("click", () => (recording ? stop() : start()));

  exportBtn.addEventListener("click", () => {
    const blob = new Blob([JSON.stringify(mapRuns(), null, 1)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `experiments_${(currentMap() || "map").replace(/[^a-zA-Z0-9_-]/g, "_")}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
  });

  // Runs are per-map: re-filter the overlay set when the active map changes.
  let lastMap = currentMap();
  const unsub = store.onChange(() => {
    if (currentMap() !== lastMap) {
      lastMap = currentMap();
      renderList();
      pushOverlays();
    }
  });

  renderList();
  pushOverlays();

  return {
    destroy() {
      if (tick) clearInterval(tick);
      if (recording) map.stopRecording();
      map.setRecordings([]);
      unsub();
      section.remove();
    },
  };
}
