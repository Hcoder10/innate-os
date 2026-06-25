// @ts-check
// Settings page — a guided editor over config/settings.yaml. The catalog (knobs,
// defaults, docs) lives in catalog.js; this renders a row per knob showing its
// default and current value, lets you override or reset, and saves over the
// proxy's /settings WebSocket (which edits settings.yaml surgically, so hand
// comment/uncomment over SSH keeps working). Doesn't use rosbridge — it talks to
// the proxy directly (GET /settings to read, /settings WS to write).
//
// Per-row state: a *saved* override (non-default, persisted) shows orange; an
// *unsaved* edit (differs from what's saved) shows blue. Save is enabled only
// while there are unsaved changes.

import { initShell } from "../shell.js";
import { CATALOG } from "./catalog.js";

initShell("settings", "../");
const stage = /** @type {HTMLElement} */ (document.getElementById("stage"));

/**
 * @typedef {Object} Entry
 * @property {import("./catalog.js").Knob} knob
 * @property {boolean} overridden   Current form state: is this knob overridden?
 * @property {*} value Current form value.
 * @property {boolean} savedOverridden  Last-saved (on-disk) state.
 * @property {*} savedValue
 * @property {() => void} render  Push value + overridden state into the DOM control.
 * @property {HTMLElement} row
 */
/** @type {Entry[]} */
const entries = [];

/**
 * @typedef {Object} GroupUI
 * @property {HTMLElement} section  The collapsible group <section>.
 * @property {HTMLButtonElement} tab  Its clickable header.
 * @property {HTMLElement} dot  Unsaved-changes indicator on the header.
 * @property {Entry[]} entries  Knob entries belonging to this group.
 */
/** @type {GroupUI[]} */
const sections = [];

let saveBtn = /** @type {HTMLButtonElement} */ (document.createElement("button"));
let resetAllBtn = /** @type {HTMLButtonElement} */ (document.createElement("button"));
let dirtyEl = /** @type {HTMLElement} */ (document.createElement("span"));
let statusEl = /** @type {HTMLElement} */ (document.createElement("span"));

const STYLE = `
.settings-page { position: absolute; inset: 0; display: flex; flex-direction: column; }
.settings-scroll { flex: 1; overflow-y: auto; min-height: 0; }
.settings-wrap { max-width: 760px; margin: 0 auto; padding: 24px 28px 32px; }
.settings-wrap .page-title { margin: 0 0 4px; font-size: 26px; font-weight: 600; letter-spacing: -.02em; }
.settings-note { color: var(--muted, #8a90a0); font-size: 13px; margin: 2px 0 22px; }
.set-group { border-bottom: 1px solid var(--hairline, #2a2f3a); }
.set-group:last-of-type { border-bottom: none; }
.set-group-h { display: flex; align-items: center; gap: 12px; width: 100%; box-sizing: border-box;
  font: inherit; font-size: 14px; font-weight: 600; text-transform: none; letter-spacing: 0;
  color: var(--muted, #8a90a0); background: none; border: none; cursor: pointer;
  padding: 15px 4px; margin: 0; text-align: left; transition: color .15s ease; }
.set-group-h:hover, .set-group.open .set-group-h { color: var(--text, #e7e7ea); }
.set-group-chev { flex: none; color: var(--muted, #8a90a0); transition: transform .2s ease, color .2s ease; }
.set-group.open .set-group-chev { transform: rotate(90deg); color: var(--primary, #7569FD); }
.set-group-count { margin-left: auto; font-size: 12px; font-weight: 500; color: var(--muted, #8a90a0); font-variant-numeric: tabular-nums; }
.set-group-dot { flex: none; width: 6px; height: 6px; border-radius: 50%; background: var(--primary, #7569FD); display: none; }
.set-group-dot.show { display: block; }
.set-group-body { display: none; }
.set-group.open .set-group-body { display: block; }
.set-group-body-inner { padding: 0 4px 14px; }
.set-group-note { color: var(--muted, #8a90a0); font-size: 12px; margin: 0 0 10px; }
.set-row { display: flex; align-items: center; gap: 16px; padding: 11px 12px; border-radius: 10px; border: 1px solid transparent;
  transition: background .15s ease, border-color .15s ease; }
.set-row:hover { background: rgba(255,255,255,.025); }
.set-row.saved { border-color: rgba(224,145,58,.45); background: rgba(224,145,58,.07); }
.set-row.dirty { border-color: rgba(117,105,253,.6); background: rgba(64,31,251,.10); }
.set-info { flex: 1; min-width: 0; }
.set-label { font-size: 14px; font-weight: 600; }
.set-doc { display: block; color: var(--muted, #8a90a0); font-size: 12px; margin-top: 2px; }
.set-ctl { display: flex; align-items: center; gap: 10px; flex-shrink: 0; }
.set-ctl input[type=number] { width: 84px; padding: 6px 8px; text-align: right; border-radius: 8px;
  border: 1px solid var(--hairline, #2a2f3a); background: var(--panel, #111114); color: inherit; font: inherit; }
.set-ctl input[type=checkbox] { width: 18px; height: 18px; }
.set-unit { color: var(--muted, #8a90a0); font-size: 12px; width: 34px; }
.set-default { color: var(--muted, #8a90a0); font-size: 12px; width: 96px; text-align: right; }
.set-reset { font-size: 12px; background: none; border: none; color: var(--primary, #7569FD); cursor: pointer; padding: 4px; visibility: hidden; }
.set-row.saved .set-reset, .set-row.dirty .set-reset { visibility: visible; }
.set-bar { flex: none; display: flex; align-items: center; gap: 14px;
  padding: 12px 28px; background: var(--panel, #111114); border-top: 1px solid var(--hairline, #2a2f3a); }
.set-save { padding: 9px 20px; border-radius: 9px; border: none; color: #fff; font: inherit; font-weight: 600; cursor: pointer;
  background: var(--primary, #401FFB); transition: filter .15s ease, opacity .2s ease; }
.set-save:not(:disabled):hover { filter: brightness(1.12); }
.set-save:disabled { opacity: .4; cursor: default; }
.set-reset-all { margin-left: auto; padding: 8px 14px; border-radius: 9px; border: 1px solid var(--hairline, #2a2f3a);
  background: none; color: var(--text, #e7e7ea); font: inherit; cursor: pointer; }
.set-reset-all:disabled { opacity: .4; cursor: default; }
.set-dirty { font-size: 13px; color: var(--primary, #7569FD); }
.set-status { font-size: 13px; }
.set-status.ok { color: #3ecf8e; }
.set-status.err { color: #ff6b6b; }
.set-status.muted { color: var(--muted, #8a90a0); }
.set-ctl input.set-text { padding: 6px 8px; border-radius: 8px; border: 1px solid var(--hairline, #2a2f3a);
  background: var(--panel, #111114); color: inherit; font: inherit; }
.set-ctl > input.set-text { width: 200px; }
.set-default { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.set-row-list { flex-direction: column; align-items: stretch; }
.set-row-list .set-ctl { flex-direction: column; align-items: stretch; gap: 8px; margin-top: 10px; }
.set-list { display: flex; flex-direction: column; gap: 8px; }
.set-list-item { display: flex; align-items: center; gap: 8px; }
.set-list-item input.set-text { flex: 1; min-width: 0; }
.set-list-rm { flex: none; background: none; border: none; color: var(--muted, #8a90a0); cursor: pointer; font-size: 15px; line-height: 1; padding: 4px 8px; }
.set-list-rm:hover { color: #ff6b6b; }
.set-list-add { align-self: flex-start; font-size: 12px; background: none; border: 1px dashed var(--hairline, #2a2f3a);
  color: var(--text, #e7e7ea); border-radius: 8px; padding: 6px 10px; cursor: pointer; }
.set-list-meta { display: flex; align-items: center; gap: 10px; }
.set-slider { width: 120px; accent-color: var(--primary, #401FFB); }
.set-slider-read { font-size: 13px; font-variant-numeric: tabular-nums; min-width: 62px; text-align: right; }
.set-slider-read .mx { color: var(--muted, #8a90a0); }
`;

/** Walk a path into the nested overrides dict; undefined if absent. */
function lookup(/** @type {any} */ obj, /** @type {string[]} */ p) {
  let cur = obj;
  for (const k of p) {
    if (cur == null || typeof cur !== "object" || !(k in cur)) return undefined;
    cur = cur[k];
  }
  return cur;
}

function setStatus(/** @type {string} */ msg, /** @type {string} */ cls = "muted") {
  statusEl.textContent = msg;
  statusEl.className = "set-status " + cls;
}

/** Value equality that handles list knobs (arrays compared by content). */
function valuesEqual(/** @type {import("./catalog.js").Knob} */ knob, /** @type {any} */ a, /** @type {any} */ b) {
  if (knob.type === "list") return JSON.stringify(a) === JSON.stringify(b);
  return a === b;
}

/** A fresh copy of a knob's default (list defaults must not share the catalog array). */
function cloneDefault(/** @type {import("./catalog.js").Knob} */ knob) {
  return knob.type === "list" ? /** @type {string[]} */ (knob.default).slice() : knob.default;
}

/** "default …" label for a knob's default value. */
function defaultLabel(/** @type {import("./catalog.js").Knob} */ knob) {
  if (knob.type === "list") {
    const arr = /** @type {string[]} */ (knob.default);
    return arr.length ? "default " + arr.join(", ") : "default (none)";
  }
  return "default " + String(knob.default);
}

/**
 * Coerce an on-disk override value to the knob's JS type.
 * @returns {*}
 */
function coerceLoaded(/** @type {import("./catalog.js").Knob} */ knob, /** @type {any} */ v) {
  if (knob.type === "bool") return Boolean(v);
  if (knob.type === "int" || knob.type === "float") return Number(v);
  if (knob.type === "list") return Array.isArray(v) ? v.map(String) : [];
  return String(v);
}

/** Has this entry changed since the last save? */
function isDirty(/** @type {Entry} */ e) {
  if (e.overridden !== e.savedOverridden) return true;
  return e.overridden && !valuesEqual(e.knob, e.value, e.savedValue);
}

/** Repaint every row + the section dots + the footer (dirty count, Save enabled). */
function recompute() {
  let dirty = 0;
  for (const e of entries) {
    const d = isDirty(e);
    if (d) dirty++;
    e.row.classList.toggle("dirty", d);
    e.row.classList.toggle("saved", !d && e.overridden);
  }
  for (const s of sections) s.dot.classList.toggle("show", s.entries.some(isDirty));
  dirtyEl.textContent = dirty ? `${dirty} unsaved change${dirty === 1 ? "" : "s"}` : "";
  saveBtn.disabled = dirty === 0;
  resetAllBtn.disabled = !entries.some((e) => e.overridden);
}

/** Reset every knob to its default in the form (you still click Save to commit). */
function resetAll() {
  for (const e of entries) {
    e.overridden = false;
    e.value = cloneDefault(e.knob);
    e.render();
  }
  recompute();
}

function build() {
  const style = document.createElement("style");
  style.textContent = STYLE;
  document.head.appendChild(style);

  const page = document.createElement("div");
  page.className = "settings-page";

  const scroll = document.createElement("div");
  scroll.className = "settings-scroll";

  const wrap = document.createElement("div");
  wrap.className = "settings-wrap";

  const title = document.createElement("h1");
  title.className = "page-title";
  title.textContent = "Settings";
  wrap.appendChild(title);

  const note = document.createElement("p");
  note.className = "settings-note";
  note.textContent =
    "Tunable parameter overrides. Blank = the robot's built-in default. Changes save to config/settings.yaml; restart the robot to apply.";
  wrap.appendChild(note);

  for (const group of CATALOG) {
    const g = document.createElement("section");
    g.className = "set-group";

    const h = document.createElement("button");
    h.className = "set-group-h";
    const chev = document.createElement("span");
    chev.className = "set-group-chev";
    chev.innerHTML =
      '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="9,6 15,12 9,18"/></svg>';
    const labelEl = document.createElement("span");
    labelEl.textContent = group.section;
    const dot = document.createElement("span");
    dot.className = "set-group-dot";
    dot.title = "Unsaved changes in this section";
    const count = document.createElement("span");
    count.className = "set-group-count";
    count.textContent = String(group.knobs.length);
    h.append(chev, labelEl, dot, count);
    h.addEventListener("click", () => g.classList.toggle("open"));
    g.appendChild(h);

    const body = document.createElement("div");
    body.className = "set-group-body";
    const inner = document.createElement("div");
    inner.className = "set-group-body-inner";
    if (group.note) {
      const gn = document.createElement("p");
      gn.className = "set-group-note";
      gn.textContent = group.note;
      inner.appendChild(gn);
    }
    const start = entries.length;
    for (const knob of group.knobs) inner.appendChild(buildRow(knob));
    body.appendChild(inner);
    g.appendChild(body);
    wrap.appendChild(g);

    sections.push({ section: g, tab: h, dot, entries: entries.slice(start) });
  }

  scroll.appendChild(wrap);
  page.appendChild(scroll);

  const bar = document.createElement("div");
  bar.className = "set-bar";
  saveBtn.className = "set-save";
  saveBtn.textContent = "Save";
  saveBtn.disabled = true;
  saveBtn.addEventListener("click", onSave);
  dirtyEl.className = "set-dirty";
  resetAllBtn.className = "set-reset-all";
  resetAllBtn.textContent = "Reset all to defaults";
  resetAllBtn.disabled = true;
  resetAllBtn.addEventListener("click", resetAll);
  bar.appendChild(saveBtn);
  bar.appendChild(dirtyEl);
  bar.appendChild(statusEl);
  bar.appendChild(resetAllBtn);
  page.appendChild(bar);

  stage.appendChild(page);
  setStatus("Loading current values…");
}

function buildRow(/** @type {import("./catalog.js").Knob} */ knob) {
  const row = document.createElement("div");
  row.className = "set-row";
  if (knob.type === "list") row.classList.add("set-row-list");

  const info = document.createElement("div");
  info.className = "set-info";
  const label = document.createElement("span");
  label.className = "set-label";
  label.textContent = knob.label;
  const doc = document.createElement("span");
  doc.className = "set-doc";
  doc.textContent = knob.doc;
  info.append(label, doc);
  row.appendChild(info);

  const ctl = document.createElement("div");
  ctl.className = "set-ctl";

  /** @type {Entry} */
  const entry = {
    knob,
    overridden: false,
    value: cloneDefault(knob),
    savedOverridden: false,
    savedValue: cloneDefault(knob),
    render: () => {},
    row,
  };

  if (knob.type === "list") buildListControl(ctl, entry);
  else buildScalarControl(ctl, entry);

  row.appendChild(ctl);
  entries.push(entry);
  return row;
}

/** Checkbox / slider / text / number control (bool, bounded numeric, string, number). */
function buildScalarControl(/** @type {HTMLElement} */ ctl, /** @type {Entry} */ entry) {
  const knob = entry.knob;
  // A numeric knob with a known hard maximum renders as a slider so the ceiling is visible.
  const isSlider = (knob.type === "int" || knob.type === "float") && knob.max !== undefined;

  if (knob.type === "bool") {
    const input = document.createElement("input");
    input.type = "checkbox";
    ctl.appendChild(input);
    entry.render = () => {
      input.checked = Boolean(entry.value);
    };
    input.addEventListener("change", () => {
      entry.value = input.checked;
      entry.overridden = true;
      recompute();
    });
  } else if (isSlider) {
    const slider = document.createElement("input");
    slider.type = "range";
    slider.className = "set-slider";
    slider.min = String(knob.min ?? 0);
    slider.max = String(knob.max);
    slider.step = String(knob.step ?? (knob.type === "int" ? 1 : 1));
    ctl.appendChild(slider);

    // "<value> / <max>" — the max is always shown so the ceiling is obvious.
    const read = document.createElement("span");
    read.className = "set-slider-read";
    const cur = document.createElement("span");
    const mx = document.createElement("span");
    mx.className = "mx";
    mx.textContent = " / " + knob.max;
    read.append(cur, mx);
    ctl.appendChild(read);

    entry.render = () => {
      slider.value = String(entry.value);
      cur.textContent = String(entry.value);
    };
    slider.addEventListener("input", () => {
      entry.value = Number(slider.value);
      entry.overridden = true;
      cur.textContent = String(entry.value);
      recompute();
    });
  } else if (knob.type === "string") {
    const input = document.createElement("input");
    input.type = "text";
    input.className = "set-text";
    ctl.appendChild(input);
    entry.render = () => {
      input.value = String(entry.value);
    };
    input.addEventListener("input", () => {
      entry.value = input.value;
      entry.overridden = true;
      recompute();
    });
  } else {
    const input = document.createElement("input");
    input.type = "number";
    input.step = knob.type === "int" ? "1" : "0.05";
    ctl.appendChild(input);
    entry.render = () => {
      input.value = String(entry.value);
    };
    input.addEventListener("input", () => {
      entry.value = Number(input.value);
      entry.overridden = true;
      recompute();
    });
  }

  const unit = document.createElement("span");
  unit.className = "set-unit";
  unit.textContent = knob.unit || "";
  ctl.appendChild(unit);

  const def = document.createElement("span");
  def.className = "set-default";
  def.textContent = defaultLabel(knob);
  def.title = def.textContent;
  ctl.appendChild(def);

  const reset = document.createElement("button");
  reset.className = "set-reset";
  reset.textContent = "reset";
  reset.addEventListener("click", () => {
    entry.overridden = false;
    entry.value = cloneDefault(knob);
    entry.render();
    recompute();
  });
  ctl.appendChild(reset);

  entry.render(); // initialise the control from entry.value (the default at build time)
}

/** Editable list of text rows for `list` knobs (e.g. extra agent/skill dirs). */
function buildListControl(/** @type {HTMLElement} */ ctl, /** @type {Entry} */ entry) {
  const knob = entry.knob;
  const list = document.createElement("div");
  list.className = "set-list";
  ctl.appendChild(list);

  const addBtn = document.createElement("button");
  addBtn.className = "set-list-add";
  addBtn.textContent = "+ Add directory";
  ctl.appendChild(addBtn);

  const meta = document.createElement("div");
  meta.className = "set-list-meta";
  const def = document.createElement("span");
  def.className = "set-default";
  def.textContent = defaultLabel(knob);
  const reset = document.createElement("button");
  reset.className = "set-reset";
  reset.textContent = "reset";
  meta.append(def, reset);
  ctl.appendChild(meta);

  // Read the text rows back into the entry; an all-blank list means "no override".
  function commit() {
    const vals = Array.from(list.querySelectorAll("input"))
      .map((i) => /** @type {HTMLInputElement} */ (i).value.trim())
      .filter(Boolean);
    entry.value = vals;
    entry.overridden = vals.length > 0;
    recompute();
  }

  function addItem(/** @type {string} */ value) {
    const item = document.createElement("div");
    item.className = "set-list-item";
    const input = document.createElement("input");
    input.type = "text";
    input.className = "set-text";
    input.placeholder = "/absolute/path";
    input.value = value;
    input.addEventListener("input", commit);
    const rm = document.createElement("button");
    rm.className = "set-list-rm";
    rm.textContent = "✕";
    rm.title = "Remove";
    rm.addEventListener("click", () => {
      item.remove();
      commit();
    });
    item.append(input, rm);
    list.appendChild(item);
  }

  entry.render = () => {
    list.textContent = "";
    const arr = Array.isArray(entry.value) ? entry.value : [];
    for (const v of arr) addItem(String(v));
  };

  addBtn.addEventListener("click", () => addItem(""));
  reset.addEventListener("click", () => {
    entry.overridden = false;
    entry.value = cloneDefault(knob);
    entry.render();
    recompute();
  });

  entry.render();
}

async function load() {
  let data;
  try {
    const res = await fetch("/settings", { cache: "no-store" });
    data = await res.json();
  } catch {
    setStatus("Couldn't read current settings — showing defaults.", "err");
    return;
  }
  const overrides = (data && data.overrides) || {};
  for (const e of entries) {
    const v = lookup(overrides, e.knob.path);
    if (v === undefined) continue;
    const val = coerceLoaded(e.knob, v);
    // An on-disk empty list is no real override; fall back to the default state.
    const active = e.knob.type === "list" ? val.length > 0 : true;
    e.overridden = e.savedOverridden = active;
    e.value = e.savedValue = active ? val : cloneDefault(e.knob);
    e.render();
  }
  // Auto-expand groups that have an override so customized values aren't hidden.
  for (const s of sections) {
    if (s.entries.some((e) => e.overridden)) s.section.classList.add("open");
  }
  recompute();
  setStatus("");
}

function saveOverWs(/** @type {any} */ payload) {
  return new Promise((resolve, reject) => {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/settings`);
    let settled = false;
    const settle = (/** @type {boolean} */ ok, /** @type {any} */ arg) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      try {
        ws.close();
      } catch {
        /* already closing */
      }
      (ok ? resolve : reject)(arg);
    };
    // Without this, a half-open socket (upgrade accepted, no reply, never closed)
    // would leave the promise pending and Save disabled for the page's life.
    const timer = setTimeout(() => settle(false, new Error("timed out — is the robot reachable?")), 12000);
    ws.addEventListener("open", () => ws.send(JSON.stringify(payload)));
    ws.addEventListener("message", (ev) => {
      try {
        settle(true, JSON.parse(ev.data));
      } catch {
        settle(true, { ok: false, message: "bad response" });
      }
    });
    ws.addEventListener("error", () => settle(false, new Error("connection failed")));
    ws.addEventListener("close", () => settle(false, new Error("closed before reply")));
  });
}

async function onSave() {
  const sets = [];
  const clears = [];
  // Snapshot exactly what we send, keyed by entry index. The success handler
  // stamps saved-state from THIS snapshot, not the live entries — otherwise a
  // field edited during the WS round-trip would be mis-marked as saved while
  // the file holds the older value.
  const snapshot = entries.map((e) => ({ overridden: e.overridden, value: e.value }));
  for (const e of entries) {
    if (e.overridden) sets.push({ path: e.knob.path, value: e.value, type: e.knob.type });
    else clears.push(e.knob.path);
  }
  saveBtn.disabled = true;
  setStatus("Saving…");
  try {
    const res = await saveOverWs({ sets, clears });
    if (res && res.ok) {
      entries.forEach((e, i) => {
        e.savedOverridden = snapshot[i].overridden;
        e.savedValue = snapshot[i].value;
      });
      recompute();
      setStatus("Saved — restart the robot to apply.", "ok");
    } else {
      setStatus("Save failed: " + ((res && res.message) || "unknown error"), "err");
      recompute();
    }
  } catch (err) {
    setStatus("Save failed: " + (err instanceof Error ? err.message : String(err)), "err");
    recompute();
  }
}

build();
load();
