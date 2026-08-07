// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Log Sources panel. The same console stream pivoted three ways:
//   • Launch file → process → node/logger   (the launch hierarchy)
//   • Process     → node/logger             (flat list of processes)
//   • Node / Logger                         (flat list of loggers)
// Each row carries a type icon (launch file / process / node / logger), a count,
// and a colour that rolls up the worst severity in its subtree. Clicking a row
// scopes the log view; "All logs" resets.

import { recSev, launchLabel } from "./format.js";
import { NODES_SERVICE } from "../constants.js";

const REFRESH_MS = 5000;

/** Inline icons (24-viewBox, currentColor). @type {Record<string, string>} */
const ICON = {
  all: '<path d="M4 6.5h16M4 12h16M4 17.5h16"/>',
  file: '<path d="M6 2.5h7l5 5V21.5H6z"/><path d="M13 2.5V8h5"/>',
  process:
    '<rect x="6.5" y="6.5" width="11" height="11" rx="2"/>' +
    '<path d="M10 3.5v3M14 3.5v3M10 17.5v3M14 17.5v3M3.5 10h3M3.5 14h3M17.5 10h3M17.5 14h3"/>',
  node: '<circle cx="12" cy="12" r="7"/><circle cx="12" cy="12" r="2.4" fill="currentColor" stroke="none"/>',
  logger: '<circle cx="12" cy="12" r="5.5"/>',
};

/** @param {string} type @returns {string} svg markup */
function icon(type) {
  return `<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON[type] || ICON.node}</svg>`;
}

/** @param {any} rec */
function procKeyOf(rec) {
  if (rec.proc == null) return "(other)";
  return rec.pidx != null ? `${rec.proc}-${rec.pidx}` : rec.proc;
}

function newStats() { return { n: 0, warn: 0, error: 0 }; }
/** @param {{n:number,warn:number,error:number}} s @param {string} sev */
function bump(s, sev) { s.n += 1; if (sev === "error") s.error += 1; else if (sev === "warn") s.warn += 1; }
/** @param {{warn:number,error:number}} s */
function worst(s) { return s.error > 0 ? "error" : s.warn > 0 ? "warn" : "ok"; }

/**
 * @param {HTMLElement} parent
 * @param {ReturnType<typeof import("./consoleSource.js").createConsoleSource>} source
 * @param {import("../rosClient.js").RosClient} ros
 * @param {{ onSelect: (scope: any) => void }} opts
 * @returns {{ destroy: () => void, selectFromRecord: (rec: any) => void }}
 */
export function createSources(parent, source, ros, opts) {
  const wrap = document.createElement("div");
  wrap.className = "sources";

  // header: title + mode switcher
  const head = document.createElement("div");
  head.className = "sources-head";
  const title = document.createElement("p");
  title.className = "microlabel";
  title.textContent = "log sources";
  const modeRow = document.createElement("div");
  modeRow.className = "sources-modes";
  /** @type {Record<string, HTMLButtonElement>} */
  const modeBtns = {};
  /** @type {Record<string, string>} */
  const MODE_HINTS = {
    launch: "Group logs by the launch file that started each process",
    proc: "Group logs by OS process",
    node: "Flat list of ROS nodes and loggers, merged across launch files",
  };
  for (const [m, label, ic] of [["launch", "Launch file", "file"], ["proc", "Process", "process"], ["node", "Node / Logger", "node"]]) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "sources-mode";
    b.title = MODE_HINTS[m];
    b.innerHTML = `${icon(ic)}<span>${label}</span>`;
    b.addEventListener("click", () => setMode(m));
    modeBtns[m] = b;
    modeRow.appendChild(b);
  }

  const filterInput = document.createElement("input");
  filterInput.className = "sources-filter mono";
  filterInput.type = "text";
  filterInput.placeholder = "filter sources…";
  filterInput.title = "Filter the tree — a branch is kept if it or any child matches";
  filterInput.spellcheck = false;
  filterInput.addEventListener("input", () => { filterText = filterInput.value.trim().toLowerCase(); render(); });

  head.append(title, modeRow, filterInput);

  const listEl = document.createElement("div");
  listEl.className = "sources-list";
  wrap.append(head, listEl);
  parent.appendChild(wrap);

  // ---- model ------------------------------------------------------------
  /** @type {Map<string, any>} pane → launch */
  const launches = new Map();
  /** @type {Set<string>} ROS node basenames (roster) — to tell node from logger */
  const roster = new Set();
  /** @type {Set<string>} expanded row keys */
  const expanded = new Set();
  let mode = "launch";
  let selectedKey = "all";
  let filterText = "";
  let dirty = false;

  // ---- filter matching (substring; a branch is kept if it or a descendant hits) ----
  /** @param {string} s */
  const hits = (s) => !filterText || (s || "").toLowerCase().includes(filterText);
  /** @param {any} N */
  const nodeHit = (N) => hits(N.node);
  /** @param {any} P */
  const procSelfHit = (P) => hits(P.proc) || hits(P.key);
  /** @param {any} P */
  const procHasNode = (P) => { for (const N of P.nodes.values()) if (nodeHit(N)) return true; return false; };
  /** @param {any} L */
  const launchHasMatch = (L) => {
    if (hits(L.label)) return true;
    for (const P of L.procs.values()) if (procSelfHit(P) || procHasNode(P)) return true;
    return false;
  };

  /** @param {any} rec */
  function ingest(rec) {
    const pane = rec.pane;
    if (!pane) return;
    let L = launches.get(pane);
    if (!L) { L = { pane, label: launchLabel(pane), stats: newStats(), procs: new Map() }; launches.set(pane, L); }
    const procKey = procKeyOf(rec);
    let P = L.procs.get(procKey);
    if (!P) { P = { key: procKey, proc: rec.proc != null ? rec.proc : "(other)", stats: newStats(), nodes: new Map() }; L.procs.set(procKey, P); }
    const nodeKey = rec.node || "(process)";
    let N = P.nodes.get(nodeKey);
    if (!N) { N = { node: nodeKey, stats: newStats() }; P.nodes.set(nodeKey, N); }
    const sev = recSev(rec);
    bump(L.stats, sev); bump(P.stats, sev); bump(N.stats, sev);
    dirty = true;
  }

  /** @param {string} name @returns {"node"|"logger"} */
  function leafType(name) {
    if (name === "(process)") return "logger";
    return roster.has(name) ? "node" : "logger";
  }

  // ---- row builder ------------------------------------------------------
  /**
   * @param {object} o
   * @param {string} o.key @param {number} o.depth @param {string} o.type
   * @param {string} o.label @param {{n:number,warn:number,error:number}} o.stats
   * @param {boolean|null} o.expandable @param {(()=>void)|null} o.onToggle
   * @param {()=>void} o.onSelect @param {string} [o.sub] @param {boolean} [o.wordy]
   */
  function row(o) {
    const el = document.createElement("div");
    el.className = "src-row" + (o.key === selectedKey ? " selected" : "");
    el.style.paddingLeft = `${8 + o.depth * 16}px`;
    el.addEventListener("click", o.onSelect);

    const caret = document.createElement("button");
    caret.type = "button";
    caret.className = "src-caret";
    if (o.expandable === null) {
      caret.classList.add("leaf");
    } else {
      // Chevron icon; CSS rotates it 90° when the branch is open.
      caret.innerHTML =
        '<svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg>';
      if (o.expandable) caret.classList.add("open");
      caret.addEventListener("click", (e) => { e.stopPropagation(); o.onToggle && o.onToggle(); });
    }

    const ic = document.createElement("span");
    ic.className = `src-icon sev-${worst(o.stats)}`;
    ic.innerHTML = icon(o.type);

    const name = document.createElement("span");
    name.className = "src-name";
    name.textContent = o.label;
    if (o.sub) {
      const sub = document.createElement("span");
      sub.className = "src-sub";
      sub.textContent = o.sub;
      name.appendChild(sub);
    }

    // Top-level rows get a worded badge ("11 errors" / "3 warnings"); nested
    // rows stay compact with a plain number.
    const count = document.createElement("span");
    count.className = "src-count";
    const e = o.stats.error;
    const w = o.stats.warn;
    if (e) {
      count.textContent = o.wordy ? `${e} ${e === 1 ? "error" : "errors"}` : String(e);
      count.classList.add("c-error");
    } else if (w) {
      count.textContent = o.wordy ? `${w} ${w === 1 ? "warning" : "warnings"}` : String(w);
      count.classList.add("c-warn");
    } else {
      count.textContent = String(o.stats.n);
    }
    if (o.wordy && (e || w)) count.classList.add("badge");

    el.append(caret, ic, name, count);
    el.title = `${o.label} — ${o.stats.n} lines · ${o.stats.error} err · ${o.stats.warn} warn`;
    return el;
  }

  /**
   * @param {string} key @param {number} depth @param {string} name
   * @param {{n:number,warn:number,error:number}} stats @param {any} scope @param {boolean} wordy
   */
  function leafRow(key, depth, name, stats, scope, wordy) {
    return row({
      key, depth, type: leafType(name), label: name, stats, wordy: !!wordy,
      expandable: null, onToggle: null, onSelect: () => select(key, scope),
    });
  }

  // ---- render modes -----------------------------------------------------
  function render() {
    const frag = document.createDocumentFragment();

    // All logs
    const total = newStats();
    for (const L of launches.values()) { total.n += L.stats.n; total.warn += L.stats.warn; total.error += L.stats.error; }
    frag.appendChild(row({
      key: "all", depth: 0, type: "all", label: "All logs", stats: total, wordy: true,
      expandable: null, onToggle: null, onSelect: () => select("all", { kind: "all" }),
    }));

    if (mode === "launch") renderLaunch(frag);
    else if (mode === "proc") renderProc(frag);
    else renderNode(frag);

    listEl.replaceChildren(frag);
    for (const [m, b] of Object.entries(modeBtns)) b.classList.toggle("active", m === mode);
  }

  /** @param {DocumentFragment} frag */
  function renderLaunch(frag) {
    for (const L of [...launches.values()].sort((a, b) => a.label.localeCompare(b.label))) {
      if (filterText && !launchHasMatch(L)) continue;
      const lKey = `L:${L.pane}`;
      const lOpen = filterText ? true : expanded.has(lKey); // filtering reveals matches
      frag.appendChild(row({
        key: lKey, depth: 0, type: "file", label: L.label, stats: L.stats, wordy: true,
        expandable: lOpen, onToggle: () => toggle(lKey),
        onSelect: () => select(lKey, { kind: "launch", pane: L.pane }),
      }));
      if (!lOpen) continue;
      const lHit = hits(L.label);
      for (const P of sortedProcs(L)) {
        if (filterText && !(lHit || procSelfHit(P) || procHasNode(P))) continue;
        const pKey = `P:${L.pane}|${P.key}`;
        const pOpen = filterText ? true : expanded.has(pKey);
        frag.appendChild(row({
          key: pKey, depth: 1, type: "process", label: P.proc, stats: P.stats,
          expandable: P.nodes.size > 0 ? pOpen : null, onToggle: () => toggle(pKey),
          onSelect: () => select(pKey, { kind: "proc", pane: L.pane, procKey: P.key }),
        }));
        if (!pOpen) continue;
        for (const N of sortedNodes(P)) {
          if (filterText && !(lHit || procSelfHit(P) || nodeHit(N))) continue;
          const nKey = `N:${L.pane}|${P.key}|${N.node}`;
          frag.appendChild(leafRow(nKey, 2, N.node, N.stats, { kind: "node", pane: L.pane, procKey: P.key, node: N.node }, false));
        }
      }
    }
  }

  /** @param {DocumentFragment} frag */
  function renderProc(frag) {
    /** @type {{pane:string,label:string,P:any}[]} */
    const procs = [];
    for (const L of launches.values()) for (const P of L.procs.values()) procs.push({ pane: L.pane, label: L.label, P });
    procs.sort((a, b) => a.P.proc.localeCompare(b.P.proc) || a.P.key.localeCompare(b.P.key));
    for (const { pane, label, P } of procs) {
      const lHit = hits(label);
      if (filterText && !(lHit || procSelfHit(P) || procHasNode(P))) continue;
      const pKey = `P:${pane}|${P.key}`;
      const pOpen = filterText ? true : expanded.has(pKey);
      frag.appendChild(row({
        key: pKey, depth: 0, type: "process", label: P.proc, sub: label, stats: P.stats, wordy: true,
        expandable: P.nodes.size > 0 ? pOpen : null, onToggle: () => toggle(pKey),
        onSelect: () => select(pKey, { kind: "proc", pane, procKey: P.key }),
      }));
      if (!pOpen) continue;
      for (const N of sortedNodes(P)) {
        if (filterText && !(lHit || procSelfHit(P) || nodeHit(N))) continue;
        const nKey = `N:${pane}|${P.key}|${N.node}`;
        frag.appendChild(leafRow(nKey, 1, N.node, N.stats, { kind: "node", pane, procKey: P.key, node: N.node }, false));
      }
    }
  }

  /** @param {DocumentFragment} frag */
  function renderNode(frag) {
    /** @type {Map<string, {n:number,warn:number,error:number}>} */
    const agg = new Map();
    for (const L of launches.values()) for (const P of L.procs.values()) for (const N of P.nodes.values()) {
      let s = agg.get(N.node);
      if (!s) { s = newStats(); agg.set(N.node, s); }
      s.n += N.stats.n; s.warn += N.stats.warn; s.error += N.stats.error;
    }
    for (const name of [...agg.keys()].sort((a, b) => a.localeCompare(b))) {
      if (filterText && !hits(name)) continue;
      frag.appendChild(leafRow(`G:${name}`, 0, name, /** @type {{n:number,warn:number,error:number}} */ (agg.get(name)), { kind: "node", node: name }, true));
    }
  }

  /** @param {any} L */
  const sortedProcs = (L) => [...L.procs.values()].sort((a, b) => a.proc.localeCompare(b.proc) || a.key.localeCompare(b.key));
  /** @param {any} P */
  const sortedNodes = (P) => [...P.nodes.values()].sort((a, b) => {
    if (a.node === "(process)") return 1;
    if (b.node === "(process)") return -1;
    return a.node.localeCompare(b.node);
  });

  // ---- interactions -----------------------------------------------------
  /** @param {string} key */
  function toggle(key) { if (expanded.has(key)) expanded.delete(key); else expanded.add(key); render(); }
  /** @param {string} key @param {any} scope */
  function select(key, scope) { selectedKey = key; render(); opts.onSelect(scope); }
  /** @param {string} m */
  function setMode(m) { if (m === mode) return; mode = m; render(); }

  /** Select the launch/process/node this record belongs to, expanding the tree to reveal it. */
  /** @param {any} rec */
  function selectFromRecord(rec) {
    const pane = rec && rec.pane;
    if (!pane || !launches.has(pane)) return;
    const procKey = procKeyOf(rec);
    const nodeKey = rec.node || "(process)";
    mode = "launch";
    expanded.add(`L:${pane}`);
    expanded.add(`P:${pane}|${procKey}`);
    select(`N:${pane}|${procKey}|${nodeKey}`, { kind: "node", pane, procKey, node: nodeKey });
  }

  // ---- roster (node vs logger) -----------------------------------------
  function fetchRoster() {
    ros.callService(NODES_SERVICE, {})
      .then((v) => {
        const names = Array.isArray(v && v.nodes) ? v.nodes : [];
        let changed = false;
        for (const full of names) {
          const base = String(full).replace(/^\//, "").split("/").pop();
          if (base && !roster.has(base)) { roster.add(base); changed = true; }
        }
        if (changed) render();
      })
      .catch(() => { /* transient */ });
  }

  // ---- wire -------------------------------------------------------------
  const unsub = source.onRecord((rec) => ingest(rec));
  const unsubReady = source.onReady(() => render());
  fetchRoster();
  render();
  const refresh = setInterval(fetchRoster, REFRESH_MS);
  const tick = setInterval(() => { if (dirty) { dirty = false; render(); } }, 1200);

  return {
    selectFromRecord,
    destroy() {
      clearInterval(refresh);
      clearInterval(tick);
      unsub();
      unsubReady();
      wrap.remove();
    },
  };
}
