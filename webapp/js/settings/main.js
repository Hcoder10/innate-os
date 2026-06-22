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
 * @property {number|boolean} value Current form value.
 * @property {boolean} savedOverridden  Last-saved (on-disk) state.
 * @property {number|boolean} savedValue
 * @property {HTMLInputElement} input
 * @property {HTMLElement} row
 */
/** @type {Entry[]} */
const entries = [];

let saveBtn = /** @type {HTMLButtonElement} */ (document.createElement("button"));
let resetAllBtn = /** @type {HTMLButtonElement} */ (document.createElement("button"));
let dirtyEl = /** @type {HTMLElement} */ (document.createElement("span"));
let statusEl = /** @type {HTMLElement} */ (document.createElement("span"));

const STYLE = `
.settings-page { position: absolute; inset: 0; display: flex; flex-direction: column; }
.settings-scroll { flex: 1; overflow-y: auto; min-height: 0; }
.settings-wrap { max-width: 760px; margin: 0 auto; padding: 24px 28px 32px; }
.settings-wrap .page-title { margin: 0 0 4px; }
.settings-note { color: var(--muted, #8a90a0); font-size: 13px; margin: 2px 0 18px; }
.set-group { margin: 26px 0 6px; }
.set-group-h { font-size: 12px; letter-spacing: .06em; text-transform: uppercase; color: var(--muted, #8a90a0); margin: 0 0 2px; }
.set-group-note { color: var(--muted, #8a90a0); font-size: 12px; margin: 0 0 10px; }
.set-row { display: flex; align-items: center; gap: 16px; padding: 11px 12px; border-radius: 10px; border: 1px solid transparent; }
.set-row.saved { border-color: rgba(224,145,58,.5); background: rgba(224,145,58,.08); }
.set-row.dirty { border-color: var(--primary, #401FFB); background: rgba(64,31,251,.10); }
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
.set-save { padding: 9px 18px; border-radius: 9px; border: none; background: var(--primary, #401FFB); color: #fff; font: inherit; font-weight: 600; cursor: pointer; }
.set-save:disabled { opacity: .45; cursor: default; }
.set-reset-all { margin-left: auto; padding: 8px 14px; border-radius: 9px; border: 1px solid var(--hairline, #2a2f3a);
  background: none; color: var(--text, #e7e7ea); font: inherit; cursor: pointer; }
.set-reset-all:disabled { opacity: .4; cursor: default; }
.set-dirty { font-size: 13px; color: var(--primary, #7569FD); }
.set-status { font-size: 13px; }
.set-status.ok { color: #3ecf8e; }
.set-status.err { color: #ff6b6b; }
.set-status.muted { color: var(--muted, #8a90a0); }
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

/** Has this entry changed since the last save? */
function isDirty(/** @type {Entry} */ e) {
  if (e.overridden !== e.savedOverridden) return true;
  return e.overridden && e.value !== e.savedValue;
}

/** Repaint every row + the footer (dirty count, Save enabled). */
function recompute() {
  let dirty = 0;
  for (const e of entries) {
    const d = isDirty(e);
    if (d) dirty++;
    e.row.classList.toggle("dirty", d);
    e.row.classList.toggle("saved", !d && e.overridden);
  }
  dirtyEl.textContent = dirty ? `${dirty} unsaved change${dirty === 1 ? "" : "s"}` : "";
  saveBtn.disabled = dirty === 0;
  resetAllBtn.disabled = !entries.some((e) => e.overridden);
}

/** Reset every knob to its default in the form (you still click Save to commit). */
function resetAll() {
  for (const e of entries) {
    e.overridden = false;
    e.value = e.knob.default;
    if (e.knob.type === "bool") e.input.checked = Boolean(e.knob.default);
    else e.input.value = String(e.knob.default);
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
    const h = document.createElement("h2");
    h.className = "set-group-h";
    h.textContent = group.section;
    g.appendChild(h);
    if (group.note) {
      const gn = document.createElement("p");
      gn.className = "set-group-note";
      gn.textContent = group.note;
      g.appendChild(gn);
    }
    for (const knob of group.knobs) g.appendChild(buildRow(knob));
    wrap.appendChild(g);
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

  const input = document.createElement("input");
  if (knob.type === "bool") {
    input.type = "checkbox";
    input.checked = Boolean(knob.default);
  } else {
    input.type = "number";
    input.step = knob.type === "int" ? "1" : "0.05";
    input.value = String(knob.default);
  }
  ctl.appendChild(input);

  const unit = document.createElement("span");
  unit.className = "set-unit";
  unit.textContent = knob.unit || "";
  ctl.appendChild(unit);

  const def = document.createElement("span");
  def.className = "set-default";
  def.textContent = "default " + String(knob.default);
  ctl.appendChild(def);

  const reset = document.createElement("button");
  reset.className = "set-reset";
  reset.textContent = "reset";
  ctl.appendChild(reset);

  row.appendChild(ctl);

  /** @type {Entry} */
  const entry = {
    knob,
    overridden: false,
    value: knob.default,
    savedOverridden: false,
    savedValue: knob.default,
    input,
    row,
  };
  entries.push(entry);

  input.addEventListener(knob.type === "bool" ? "change" : "input", () => {
    entry.value = knob.type === "bool" ? input.checked : Number(input.value);
    entry.overridden = true;
    recompute();
  });

  reset.addEventListener("click", () => {
    entry.overridden = false;
    entry.value = knob.default;
    if (knob.type === "bool") input.checked = Boolean(knob.default);
    else input.value = String(knob.default);
    recompute();
  });

  return row;
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
    if (v !== undefined) {
      const val = e.knob.type === "bool" ? Boolean(v) : Number(v);
      e.overridden = e.savedOverridden = true;
      e.value = e.savedValue = val;
      if (e.knob.type === "bool") e.input.checked = Boolean(val);
      else e.input.value = String(val);
    }
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
