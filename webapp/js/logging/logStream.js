// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Chronological log view. Renders the structured console records, scoped by the
// tree selection (launch / process / node / all), with a severity threshold,
// text search, a raw/clean toggle, and tail-style autoscroll. Records are
// pre-parsed by the bridge, so there's no text parsing here.

import { recSev, SEV_RANK, clockMs, sourceColor, launchLabel } from "./format.js";
import { copyToButton, ICON_COPY } from "../clipboard.js";

const BUFFER_MAX = 6000;
const DOM_MAX = 1500;

/** @param {any} rec */
function procKeyOf(rec) {
  if (rec.proc == null) return "(other)";
  return rec.pidx != null ? `${rec.proc}-${rec.pidx}` : rec.proc;
}

/**
 * @param {HTMLElement} parent
 * @param {ReturnType<typeof import("./consoleSource.js").createConsoleSource>} source
 * @param {{ onSourceClick?: (rec: any) => void }} [opts]
 * @returns {{ destroy: () => void, setScope: (scope: any) => void }}
 */
export function createLogStream(parent, source, opts) {
  /** @type {any[]} */
  let buffer = [];
  let started = false; // becomes true once the backfill batch is applied
  let paused = false;
  let raw = false;
  /** @type {{kind:string, pane?:string, procKey?:string, node?:string}} */
  let scope = { kind: "all" };
  const filter = { minSev: "all", text: "" };

  // ---- header: "LOGS" title + breadcrumb + controls --------------------
  const pageTitle = document.createElement("p");
  pageTitle.className = "microlabel logs-title";
  pageTitle.textContent = "logs";

  const bar = document.createElement("div");
  bar.className = "log-bar";

  const crumb = document.createElement("div");
  crumb.className = "logs-crumb";
  const scopeLabel = document.createElement("span");
  scopeLabel.className = "log-scope mono";
  scopeLabel.title = "Log lines are filtered to this source";
  const errBadge = document.createElement("span");
  errBadge.className = "logs-errbadge";
  errBadge.title = "Errors in the selected source, ignoring the level filter";
  errBadge.hidden = true;
  crumb.append(scopeLabel, errBadge);

  const sevSel = document.createElement("select");
  sevSel.className = "log-select";
  sevSel.title = "Minimum severity to show";
  for (const [v, label] of [["all", "All levels"], ["info", "Info+"], ["warn", "Warn+"], ["error", "Errors"]]) {
    const o = document.createElement("option");
    o.value = v; o.textContent = label; sevSel.appendChild(o);
  }
  sevSel.addEventListener("change", () => { filter.minSev = sevSel.value; rerender(); });

  const search = document.createElement("input");
  search.className = "log-search mono";
  search.type = "text";
  search.placeholder = "filter…";
  search.title = "Filter by node, process, launch file, or message text";
  search.spellcheck = false;
  search.addEventListener("input", () => { filter.text = search.value.trim().toLowerCase(); rerender(); });

  const spacer = document.createElement("div");
  spacer.className = "log-bar-spacer";

  const count = document.createElement("span");
  count.className = "log-count microlabel";
  count.title = "Lines shown / lines buffered";

  const rawBtn = document.createElement("button");
  rawBtn.className = "log-btn";
  rawBtn.type = "button";
  rawBtn.title = "Show the raw line as the process printed it, instead of the parsed message";
  rawBtn.addEventListener("click", () => { raw = !raw; syncRaw(); rerender(); });

  const pauseBtn = document.createElement("button");
  pauseBtn.className = "log-btn";
  pauseBtn.type = "button";
  pauseBtn.title = "Pause autoscroll — new lines keep buffering";
  pauseBtn.addEventListener("click", () => { paused = !paused; if (!paused) scrollToEnd(); syncPause(); });

  const clearBtn = document.createElement("button");
  clearBtn.className = "log-btn";
  clearBtn.type = "button";
  clearBtn.textContent = "Clear";
  clearBtn.title = "Clear the buffer in this browser — the robot's log is untouched";
  clearBtn.addEventListener("click", () => { buffer = []; list.replaceChildren(); updateCount(); });

  const copyBtn = document.createElement("button");
  copyBtn.className = "log-btn log-icon-btn";
  copyBtn.type = "button";
  copyBtn.title = "Copy shown logs";
  copyBtn.innerHTML = ICON_COPY;
  copyBtn.addEventListener("click", copyLogs);

  bar.append(crumb, sevSel, search, spacer, count, rawBtn, pauseBtn, clearBtn, copyBtn);

  // column header
  const cols = document.createElement("div");
  cols.className = "log-cols";
  for (const [c, label] of [["time", "Time"], ["level", "Level"], ["source", "Source"], ["msg", "Message"]]) {
    const h = document.createElement("span");
    h.className = `col-${c}`;
    h.textContent = label;
    cols.appendChild(h);
  }

  const list = document.createElement("div");
  list.className = "log-list mono";

  parent.append(pageTitle, bar, cols, list);
  syncPause();
  syncRaw();
  syncScope();

  // ---- filtering + rendering -------------------------------------------
  /** @param {any} rec */
  function inScope(rec) {
    switch (scope.kind) {
      case "all": return true;
      case "launch": return rec.pane === scope.pane;
      case "proc": return rec.pane === scope.pane && procKeyOf(rec) === scope.procKey;
      case "node":
        // Scoped to one process when pane is set; otherwise the node anywhere.
        if (scope.pane != null && (rec.pane !== scope.pane || procKeyOf(rec) !== scope.procKey)) return false;
        return (rec.node || "(process)") === scope.node;
      default: return true;
    }
  }

  /** @param {any} rec */
  function passes(rec) {
    if (!inScope(rec)) return false;
    if (filter.minSev !== "all" && SEV_RANK[recSev(rec)] < SEV_RANK[filter.minSev]) return false;
    if (filter.text) {
      const hay = `${rec.node || ""} ${rec.proc || ""} ${rec.pane} ${rec.text}`.toLowerCase();
      if (!hay.includes(filter.text)) return false;
    }
    return true;
  }

  /** @param {any} rec */
  function makeRow(rec) {
    const sev = recSev(rec);
    const row = document.createElement("div");
    row.className = `log-row sev-${sev}`;

    const time = document.createElement("span");
    time.className = "col-time";
    time.textContent = clockMs(rec.t);

    const lvl = document.createElement("span");
    lvl.className = `col-level lvl-${sev}`;
    lvl.textContent = rec.level || sev.toUpperCase();

    const src = document.createElement("span");
    src.className = "col-source";
    const source = rec.logger || rec.node || rec.proc || rec.pane;
    src.textContent = source;
    src.style.color = sourceColor(source);
    src.title = `${source} — click to show all logs from this source`;
    if (opts && opts.onSourceClick) {
      src.addEventListener("click", (e) => { e.stopPropagation(); /** @type {(rec: any) => void} */ (opts.onSourceClick)(rec); });
    }

    const msg = document.createElement("span");
    msg.className = "col-msg";
    msg.textContent = raw ? rec.text : (rec.msg != null ? rec.msg : rec.text);

    row.append(time, lvl, src, msg);
    return row;
  }

  /** @param {any} rec */
  function lineText(rec) {
    const sev = recSev(rec);
    const level = rec.level || sev.toUpperCase();
    const source = rec.logger || rec.node || rec.proc || rec.pane;
    const msg = raw ? rec.text : (rec.msg != null ? rec.msg : rec.text);
    return `${clockMs(rec.t)}  ${level}  ${source}  ${msg}`;
  }

  function copyLogs() {
    const text = buffer.filter(passes).map(lineText).join("\n");
    copyToButton(text, copyBtn, "active");
  }

  function atBottom() {
    return list.scrollHeight - list.scrollTop - list.clientHeight < 24;
  }
  function scrollToEnd() { list.scrollTop = list.scrollHeight; }

  /** @param {any} rec */
  function appendRow(rec) {
    if (!passes(rec)) return;
    const wasBottom = atBottom();
    list.appendChild(makeRow(rec));
    while (list.childElementCount > DOM_MAX) list.firstChild && list.removeChild(list.firstChild);
    if (!paused && wasBottom) scrollToEnd();
  }

  function rerender() {
    const frag = document.createDocumentFragment();
    for (const rec of buffer.filter(passes).slice(-DOM_MAX)) frag.appendChild(makeRow(rec));
    list.replaceChildren(frag);
    updateCount();
    if (!paused) scrollToEnd();
  }

  function updateCount() {
    let shown = 0;
    let errs = 0; // errors in the selected source, regardless of the level filter
    for (const r of buffer) {
      if (passes(r)) shown++;
      if (inScope(r) && recSev(r) === "error") errs++;
    }
    count.textContent = `${shown} / ${buffer.length}`;
    errBadge.hidden = errs === 0;
    errBadge.textContent = errs ? `${errs} ${errs === 1 ? "error" : "errors"}` : "";
  }

  function syncPause() {
    pauseBtn.textContent = paused ? "Paused" : "● Live";
    pauseBtn.classList.toggle("active", !paused);
  }
  function syncRaw() {
    rawBtn.textContent = raw ? "Raw" : "Clean";
    rawBtn.classList.toggle("active", raw);
  }
  function syncScope() {
    if (scope.kind === "all") { scopeLabel.textContent = "All logs"; return; }
    // Full hierarchy: launch file › process › node (whichever parts are known).
    const parts = [];
    if (scope.pane) parts.push(launchLabel(scope.pane));
    if (scope.procKey) parts.push(scope.procKey);
    if (scope.kind === "node" && scope.node) parts.push(scope.node);
    scopeLabel.textContent = "Source: " + (parts.join(" › ") || scope.node || "");
  }

  // ---- ingest -----------------------------------------------------------
  /** @param {any} rec */
  function push(rec) {
    buffer.push(rec);
    if (buffer.length > BUFFER_MAX) buffer.splice(0, buffer.length - BUFFER_MAX);
    if (started) {
      appendRow(rec);
      updateCount();
    }
  }

  const unsub = source.onRecord((rec) => push(rec));
  const unsubReady = source.onReady(() => {
    started = true;
    buffer.sort((a, b) => a.t - b.t); // backfill + any early live → time order
    rerender();
  });

  return {
    /** @param {any} s scope from the tree */
    setScope(s) { scope = s || { kind: "all" }; syncScope(); rerender(); },
    destroy() {
      unsub();
      unsubReady();
      bar.remove();
      list.remove();
    },
  };
}
