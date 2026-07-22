// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Logs modal — two modes:
//  • downloaded run: browse the downloaded result files (served by the front
//    door from <skill_dir>/<run_id>/). process_output.jsonl renders as a
//    stdout/stderr stream; other files show raw.
//  • live run: poll the robot's get_run_logs service (best-effort in-memory
//    tail from the orchestrator) every few seconds while training.
// Error-ish lines are highlighted in both modes.

import { GET_RUN_LOGS_SERVICE } from "../constants.js";
import { copyText } from "../clipboard.js";

const PREFERRED = ["process_output.jsonl", "daemon.log", "output.log"];
const ERR_RE = /traceback|error|exception|runtimeerror|cuda error|failed|fatal|killed/i;
const LIVE_POLL_MS = 3000;

const ICON_COPY =
  '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
const ICON_CHECK =
  '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg>';

/**
 * @param {HTMLElement} parent
 * @param {string} skillDir
 * @param {number} runId
 * @param {{ onClose: () => void, ros?: import("../rosClient.js").RosClient, live?: boolean }} opts
 * @returns {{ destroy: () => void }}
 */
export function createLogsModal(parent, skillDir, runId, opts) {
  const q = `dir=${encodeURIComponent(skillDir)}&id=${runId}`;
  const live = !!opts.live;
  let fileSeq = 0;
  /** @type {ReturnType<typeof setInterval> | null} */
  let pollTimer = null;
  let polling = false;

  const backdrop = document.createElement("div");
  backdrop.className = "modal-backdrop";
  backdrop.addEventListener("click", () => opts.onClose());

  const modal = document.createElement("div");
  modal.className = "modal modal-lg";
  modal.addEventListener("click", (e) => e.stopPropagation());

  const head = document.createElement("div");
  head.className = "modal-head";
  const title = document.createElement("h2");
  title.className = "modal-title";
  title.textContent = live ? `Run #${runId} · live logs` : `Run #${runId} logs`;
  const fileSel = document.createElement("select");
  fileSel.className = "modal-select logs-filesel";
  fileSel.addEventListener("change", () => showFile(fileSel.value));
  const liveBadge = document.createElement("span");
  liveBadge.className = "logs-live-badge";
  liveBadge.textContent = "● live";
  const close = document.createElement("button");
  close.type = "button";
  close.className = "modal-close";
  close.textContent = "✕";
  close.addEventListener("click", () => opts.onClose());
  const copyBtn = document.createElement("button");
  copyBtn.type = "button";
  copyBtn.className = "logs-copy-btn";
  copyBtn.title = "Copy logs";
  copyBtn.innerHTML = ICON_COPY;
  copyBtn.addEventListener("click", copyLogs);
  const headTools = document.createElement("div");
  headTools.className = "logs-head-tools";
  headTools.append(live ? liveBadge : fileSel, copyBtn, close);
  head.append(title, headTools);

  const view = document.createElement("div");
  view.className = "logs-view";
  view.textContent = "Loading…";

  modal.append(head, view);
  backdrop.appendChild(modal);
  parent.appendChild(backdrop);

  if (live) startLive();
  else loadFiles();

  function copyLogs() {
    copyText(view.innerText).then(() => {
      copyBtn.innerHTML = ICON_CHECK;
      copyBtn.classList.add("copied");
      setTimeout(() => {
        copyBtn.innerHTML = ICON_COPY;
        copyBtn.classList.remove("copied");
      }, 1400);
    });
  }

  // ── Downloaded-files mode ───────────────────────────────────────────────
  function loadFiles() {
    fetch(`/run/info?${q}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`run/info ${r.status}`))))
      .then((info) => {
        const files = Array.isArray(info.files) ? info.files : [];
        if (files.length === 0) {
          view.textContent = "No result files for this run.";
          return;
        }
        files.sort(byPreference);
        for (const f of files) {
          const o = document.createElement("option");
          o.value = f;
          o.textContent = f;
          fileSel.appendChild(o);
        }
        showFile(files[0]);
      })
      .catch((err) => (view.textContent = `Couldn't list logs — ${err.message}`));
  }

  /** @param {string} name */
  function showFile(name) {
    const seq = ++fileSeq;
    view.textContent = "Loading…";
    fetch(`/run/log?${q}&file=${encodeURIComponent(name)}`)
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error(`run/log ${r.status}`))))
      .then((text) => {
        if (seq !== fileSeq) return;
        if (name.endsWith(".jsonl")) renderJsonl(text);
        else renderLines(text.split("\n"));
        view.scrollTop = view.scrollHeight; // jump to the end (errors are last)
      })
      .catch((err) => {
        if (seq !== fileSeq) return;
        view.textContent = `Couldn't load ${name} — ${err.message}`;
      });
  }

  // ── Live mode ───────────────────────────────────────────────────────────
  function startLive() {
    poll();
    pollTimer = setInterval(poll, LIVE_POLL_MS);
  }

  function poll() {
    if (polling || !opts.ros) return;
    polling = true;
    opts.ros
      .callService(GET_RUN_LOGS_SERVICE, { skill_dir: skillDir, run_id: runId })
      .then((res) => {
        polling = false;
        const lines = res && Array.isArray(res.lines) ? res.lines.map((/** @type {any} */ x) => String(x)) : [];
        if (res && res.success === false) {
          if (view.childElementCount === 0) view.textContent = `No live logs — ${res.message || "unavailable"}`;
          return;
        }
        if (lines.length === 0) {
          if (!view.querySelector(".log-line")) view.textContent = "Waiting for training output…";
          return;
        }
        // Keep the user pinned to the tail unless they've scrolled up to read.
        const atBottom = view.scrollHeight - view.scrollTop - view.clientHeight < 40;
        const rendered = renderLiveLines(lines);
        if (!rendered && !view.querySelector(".log-line")) view.textContent = "Waiting for training output…";
        if (atBottom) view.scrollTop = view.scrollHeight;
      })
      .catch((err) => {
        polling = false;
        if (view.childElementCount === 0) view.textContent = `Couldn't load live logs — ${err.message}`;
      });
  }

  // ── Rendering ───────────────────────────────────────────────────────────
  /** process_output.jsonl: one {ts, stream, line} per line. @param {string} text */
  function renderJsonl(text) {
    const frag = document.createDocumentFragment();
    for (const raw of text.split("\n")) {
      if (!raw.trim()) continue;
      let o;
      try {
        o = JSON.parse(raw);
      } catch {
        continue;
      }
      const line = String(o.line ?? "");
      const row = document.createElement("div");
      row.className = "log-line" + (o.stream === "stderr" ? " is-stderr" : "") + (ERR_RE.test(line) ? " is-err" : "");
      row.textContent = line;
      frag.appendChild(row);
    }
    view.replaceChildren(frag);
  }

  /** @param {string[]} lines */
  function renderLines(lines) {
    const frag = document.createDocumentFragment();
    for (const line of lines) {
      const row = document.createElement("div");
      row.className = "log-line" + (ERR_RE.test(line) ? " is-err" : "");
      row.textContent = line;
      frag.appendChild(row);
    }
    view.replaceChildren(frag);
  }

  /** Live lines arrive as the daemon's tailed buffer: "[process_output.jsonl]
   * {json}". Strip the file prefix, unwrap the JSONL record to its stdout/stderr
   * text, and drop the internal progress marker. @param {string[]} lines
   * @returns {boolean} whether any line was rendered */
  function renderLiveLines(lines) {
    const frag = document.createDocumentFragment();
    let count = 0;
    for (const raw of lines) {
      let s = raw;
      const m = /^\[[^\]]+\]\s(.*)$/.exec(s); // strip "[file] " prefix
      if (m) s = m[1];
      let text = s;
      let stream = "stdout";
      if (s.startsWith("{")) {
        try {
          const o = JSON.parse(s);
          if (typeof o.line === "string") {
            text = o.line;
            stream = o.stream === "stderr" ? "stderr" : "stdout";
          }
        } catch {
          /* not a JSON record — show as-is */
        }
      }
      if (text.includes("@@INNATE_PROGRESS")) continue; // internal marker
      const row = document.createElement("div");
      row.className = "log-line" + (stream === "stderr" ? " is-stderr" : "") + (ERR_RE.test(text) ? " is-err" : "");
      row.textContent = text;
      frag.appendChild(row);
      count++;
    }
    if (count > 0) view.replaceChildren(frag);
    return count > 0;
  }

  return {
    destroy() {
      fileSeq++;
      if (pollTimer) clearInterval(pollTimer);
      pollTimer = null;
      backdrop.remove();
    },
  };
}

/** Sort with the most useful files first. @param {string} a @param {string} b */
function byPreference(a, b) {
  const ia = PREFERRED.indexOf(a.split("/").pop() || a);
  const ib = PREFERRED.indexOf(b.split("/").pop() || b);
  const ra = ia === -1 ? 99 : ia;
  const rb = ib === -1 ? 99 : ib;
  return ra - rb || a.localeCompare(b);
}
