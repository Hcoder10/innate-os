// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Right-side run detail pane. Shows the run selected in the dashboard (defaults
// to the running run): progress + ETA, status/timing/hardware, hyperparameters
// (from training_params), artifacts, and the same actions as the run row.
// Re-renders on every job_statuses tick so a running run stays live.

const STATUS = {
  UNKNOWN: 0,
  WAITING_FOR_APPROVAL: 1,
  APPROVED: 2,
  REJECTED: 3,
  BOOTING: 4,
  RUNNING: 5,
  DONE: 6,
  DOWNLOADED: 7,
  CANCELLED: 8,
};
/** @type {Record<number, string>} */
const LABEL = {
  0: "Unknown",
  1: "Pending approval",
  2: "Approved",
  3: "Rejected",
  4: "Booting",
  5: "Running",
  6: "Done",
  7: "Downloaded",
  8: "Cancelled",
};

/** @param {number} secs */
function fmtDur(secs) {
  if (secs < 60) return `${secs}s`;
  const m = Math.floor(secs / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h${String(m % 60).padStart(2, "0")}m`;
}

/** @param {{sec:number}|undefined} t */
function fmtTime(t) {
  const sec = t?.sec || 0;
  if (!sec) return "—";
  const d = new Date(sec * 1000);
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

/** Parse the run's training_params JSON (sent by the robot as a string).
 * @param {TrainingRunStatus} run @returns {Record<string, any>} */
function params(run) {
  if (!run.training_params_json) return {};
  try {
    return JSON.parse(run.training_params_json) || {};
  } catch {
    return {};
  }
}

/** Humanize an ENV-style key, e.g. LEARNING_RATE_BACKBONE -> "Learning rate backbone".
 * @param {string} k */
function humanize(k) {
  const s = k.toLowerCase().replace(/_/g, " ");
  return s.charAt(0).toUpperCase() + s.slice(1);
}

/**
 * @param {HTMLElement} parent
 * @param {import("../rosClient.js").RosClient} ros
 * @param {{ jobList: TrainingJobList | null, selected?: { skillDir: string, runId: number } | null }} store
 * @param {{ onClose: () => void, onOpenLogs: (skillDir: string, runId: number, live?: boolean) => void, actionsFor: (sk: TrainingSkillStatus, run: TrainingRunStatus) => HTMLElement[], infoFor: (sk: TrainingSkillStatus, run: TrainingRunStatus) => ({ downloaded: boolean, has_checkpoint: boolean, error_excerpt?: string } | undefined) }} opts
 * @returns {{ render: () => void, destroy: () => void }}
 */
export function createRunDetail(parent, ros, store, opts) {
  const pane = document.createElement("aside");
  pane.className = "run-detail";
  parent.appendChild(pane);

  /** Resolve the selected (sk, run) from the store, or null. */
  function resolve() {
    const sel = store.selected;
    if (!sel) return null;
    const sk = (store.jobList?.skills || []).find((s) => s.skill_dir === sel.skillDir);
    const run = sk?.runs?.find((r) => r.run_id === sel.runId);
    return sk && run ? { sk, run } : null;
  }

  /** @param {string} label @param {string} value @returns {HTMLElement} */
  function row(label, value) {
    const r = document.createElement("div");
    r.className = "rd-row";
    const k = document.createElement("span");
    k.className = "rd-k";
    k.textContent = label;
    const v = document.createElement("span");
    v.className = "rd-v mono";
    v.textContent = value;
    r.append(k, v);
    return r;
  }

  /** @param {string} text @returns {HTMLElement} */
  function section(text) {
    const h = document.createElement("div");
    h.className = "rd-section microlabel";
    h.textContent = text;
    return h;
  }

  function render() {
    const found = resolve();
    if (!found) {
      pane.classList.add("is-empty");
      const empty = document.createElement("div");
      empty.className = "rd-empty";
      empty.textContent = "Select a run to see details.";
      pane.replaceChildren(empty);
      return;
    }
    pane.classList.remove("is-empty");
    const { sk, run } = found;
    const frag = document.createDocumentFragment();

    // ── header ──────────────────────────────────────────────────────
    const head = document.createElement("div");
    head.className = "rd-head";
    const titleBox = document.createElement("div");
    const eyebrow = document.createElement("div");
    eyebrow.className = "microlabel";
    eyebrow.textContent = "Selected run";
    const title = document.createElement("div");
    title.className = "rd-title";
    title.textContent = `${sk.skill_name} / run #${run.run_id}`;
    titleBox.append(eyebrow, title);
    const close = document.createElement("button");
    close.type = "button";
    close.className = "rd-close";
    close.textContent = "✕";
    close.setAttribute("aria-label", "Close details");
    close.addEventListener("click", opts.onClose);
    head.append(titleBox, close);
    frag.appendChild(head);

    // Same derived outcome as the dashboard card: a finished run with no
    // checkpoint is a failed run, and this pane must not soften that.
    const info = run.status === STATUS.DOWNLOADED ? opts.infoFor(sk, run) : undefined;
    const failed = !!info && !info.has_checkpoint;
    const statusText = failed ? "Failed" : LABEL[run.status] || "Unknown";

    const statusRow = document.createElement("div");
    statusRow.className = "rd-statusrow";
    const dot = document.createElement("span");
    dot.className = failed ? "run-dot s-failed" : `run-dot s-${run.status}`;
    const statusLbl = document.createElement("span");
    statusLbl.textContent = statusText;
    statusRow.append(dot, statusLbl);
    if (run.daemon_state && run.status === STATUS.RUNNING) {
      const ds = document.createElement("span");
      ds.className = "run-daemon";
      ds.textContent = run.daemon_state;
      statusRow.append(ds);
    }
    frag.appendChild(statusRow);

    // ── progress ────────────────────────────────────────────────────
    const step = run.current_step || 0;
    const total = run.total_step || 0;
    if (run.status === STATUS.RUNNING || step > 0 || total > 0) {
      frag.appendChild(section("Progress"));
      const pct = total > 0 ? Math.min(100, Math.round((step / total) * 100)) : 0;
      const line = document.createElement("div");
      line.className = "rd-progress-line mono";
      line.textContent =
        total > 0 ? `${step.toLocaleString()} / ${total.toLocaleString()} steps · ${pct}%` : `${step.toLocaleString()} steps`;
      frag.appendChild(line);
      const bar = document.createElement("div");
      bar.className = total > 0 ? "tbar" : "tbar tbar-indeterminate";
      const fill = document.createElement("div");
      fill.className = "tbar-fill";
      if (total > 0) fill.style.width = `${pct}%`;
      bar.appendChild(fill);
      frag.appendChild(bar);
    }

    // ── details ─────────────────────────────────────────────────────
    frag.appendChild(section("Details"));
    frag.appendChild(row("Status", statusText));
    if (info) {
      frag.appendChild(
        row("Outcome", info.has_checkpoint ? "✓ checkpoint trained" : "✗ no checkpoint produced — check the logs"),
      );
      if (!info.has_checkpoint && info.error_excerpt) {
        frag.appendChild(row("Error", info.error_excerpt));
      }
    }
    frag.appendChild(row("Started", fmtTime(run.started_at)));
    const startSec = run.started_at?.sec || 0;
    const endSec = run.finished_at?.sec || 0;
    if (endSec && startSec) frag.appendChild(row("Duration", fmtDur(endSec - startSec)));
    else if (startSec) frag.appendChild(row("Elapsed", fmtDur(Math.max(0, Math.floor(Date.now() / 1000) - startSec))));
    if (run.instance_type) frag.appendChild(row("Hardware", run.instance_type));

    // ── hyperparameters (from training_params, when the robot forwards it) ──
    const p = params(run);
    const env = p.env && typeof p.env === "object" ? p.env : {};
    const hyperRows = [];
    if (p.preset) hyperRows.push(["Preset", String(p.preset)]);
    if (p.gpu_type) hyperRows.push(["GPU", `${p.gpu_type}${p.min_gpus ? ` ×${p.min_gpus}` : ""}`]);
    for (const [k, v] of Object.entries(env)) hyperRows.push([humanize(k), String(v)]);
    if (hyperRows.length) {
      frag.appendChild(section("Hyperparameters"));
      for (const [k, v] of hyperRows) frag.appendChild(row(k, v));
    }

    // ── actions ─────────────────────────────────────────────────────
    const actions = opts.actionsFor(sk, run);
    if (actions.length) {
      frag.appendChild(section("Actions"));
      const acts = document.createElement("div");
      acts.className = "rd-actions";
      acts.append(...actions);
      frag.appendChild(acts);
    }

    pane.replaceChildren(frag);
  }

  render();
  return {
    render,
    destroy() {
      pane.remove();
    },
  };
}
