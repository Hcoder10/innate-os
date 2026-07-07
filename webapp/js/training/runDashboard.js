// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Training dashboard — all cloud runs grouped by skill, from the latest
// /innate_training/job_statuses snapshot. Header (train / filter / search) is
// built once; the body re-renders on each topic tick. Handles Download and
// reloads the brain once when a run finishes downloading this session.

import { DOWNLOAD_RESULTS_SERVICE, BRAIN_RELOAD_SERVICE, CANCEL_RUN_SERVICE } from "../constants.js";
import { renderRunningHero, STATUS_RUNNING } from "./runningHero.js";

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
const STAGE = ["Compressing", "Uploading", "Downloading", "Verifying", "Done", "Error"];

/** @param {number} s */
const isTerminal = (s) =>
  s === STATUS.REJECTED || s === STATUS.DONE || s === STATUS.DOWNLOADED || s === STATUS.CANCELLED;

/**
 * @param {HTMLElement} parent
 * @param {import("../rosClient.js").RosClient} ros
 * @param {{ jobList: TrainingJobList | null, skills: Skill[], statusFor: (d: string) => any, pending?: Map<string, any>, selected?: { skillDir: string, runId: number } | null }} store
 * @param {{ onTrain: (skill?: Skill) => void, onOpenLogs: (skillDir: string, runId: number, live?: boolean) => void, onSelect?: (skillDir: string, runId: number) => void, onRunInfo?: () => void }} opts
 * @returns {{ render: () => void, destroy: () => void }}
 */
export function createRunDashboard(parent, ros, store, opts) {
  // Per-run results info (downloaded?/has_checkpoint?), fetched lazily from the
  // front door for finished runs and cached. has_checkpoint == success.
  /** @type {Map<string, { downloaded: boolean, has_checkpoint: boolean, files: string[] }>} */
  const runInfo = new Map();
  /** @type {Set<string>} */
  const infoInflight = new Set();
  // ETA estimation: smoothed training step-rate per run, updated whenever the
  // observed current_step advances (the robot polls the orchestrator ~every 20s).
  /** @type {Map<string, { step: number, t: number, rate: number }>} run key → {last step, time(ms), steps/ms} */
  const etaTrack = new Map();
  const wrap = document.createElement("div");
  wrap.className = "training-wrap";

  // --- header (built once) -------------------------------------------------
  const head = document.createElement("div");
  head.className = "training-head";
  const title = document.createElement("h2");
  title.className = "training-title";
  title.textContent = "Training";
  const sub = document.createElement("p");
  sub.className = "training-sub";
  const titleBox = document.createElement("div");
  titleBox.append(title, sub);

  const tools = document.createElement("div");
  tools.className = "training-tools";
  const search = document.createElement("input");
  search.type = "search";
  search.className = "episodes-search";
  search.placeholder = "Search skills / #run";
  const filter = document.createElement("select");
  filter.className = "episodes-tool";
  for (const [val, text] of [
    ["all", "All"],
    ["active", "Active"],
    ["completed", "Completed"],
  ]) {
    const o = document.createElement("option");
    o.value = val;
    o.textContent = text;
    filter.appendChild(o);
  }
  const trainBtn = document.createElement("button");
  trainBtn.type = "button";
  trainBtn.className = "training-train-btn";
  trainBtn.textContent = "+ Train a skill";
  trainBtn.addEventListener("click", () => opts.onTrain());
  tools.append(search, filter, trainBtn);
  head.append(titleBox, tools);

  // "Running now" hero (re-rendered each tick, above the skill list).
  const heroHost = document.createElement("div");
  heroHost.className = "training-hero-host";

  const body = document.createElement("div");
  body.className = "training-body";

  wrap.append(head, heroHost, body);
  parent.appendChild(wrap);

  /** First running run + its skill, or null. @returns {{sk: TrainingSkillStatus, run: TrainingRunStatus}|null} */
  function findRunning() {
    for (const sk of store.jobList?.skills || []) {
      const run = (sk.runs || []).find((r) => r.status === STATUS_RUNNING);
      if (run) return { sk, run };
    }
    return null;
  }

  let query = "";
  search.addEventListener("input", () => {
    query = search.value.trim().toLowerCase();
    render();
  });
  filter.addEventListener("change", render);

  // Reload-the-brain-on-download: only when a run transitions to DOWNLOADED
  // *this session* (existing downloads seen on first render don't trigger it).
  /** @type {Map<string, number>} run key → last status */
  const prevStatus = new Map();
  let primed = false;

  function reloadOnDownloads() {
    const skills = store.jobList?.skills || [];
    for (const sk of skills) {
      for (const run of sk.runs || []) {
        const key = `${sk.skill_dir}#${run.run_id}`;
        const was = prevStatus.get(key);
        if (primed && was !== undefined && was !== STATUS.DOWNLOADED && run.status === STATUS.DOWNLOADED) {
          ros.callService(BRAIN_RELOAD_SERVICE, {}).catch((e) => console.error("[training] reload failed:", e));
        }
        prevStatus.set(key, run.status);
      }
    }
    primed = true; // first pass only records; subsequent passes can fire
  }

  /** Render (or clear) the "Running now" hero card above the skill list. */
  function renderHero() {
    const running = findRunning();
    if (!running) {
      heroHost.replaceChildren();
      return;
    }
    const { sk, run } = running;
    const key = `${sk.skill_dir}#${run.run_id}`;
    const eta = etaSeconds(key, run.current_step || 0, run.total_step || 0);
    heroHost.replaceChildren(
      renderRunningHero({
        sk,
        run,
        eta,
        actions: runActions(sk, run),
        onOpen: () => opts.onSelect?.(sk.skill_dir, run.run_id),
      }),
    );
  }

  function render() {
    reloadOnDownloads();
    renderHero();
    // Only skills present on this robot (non-empty skill_dir). The account's
    // job list can include skills submitted from other robots, which have no
    // local directory and can't be synced/downloaded here.
    const skills = [...(store.jobList?.skills || [])]
      .filter((s) => s.skill_dir)
      .sort((a, b) => a.skill_name.localeCompare(b.skill_name));
    const mode = filter.value;

    if (skills.length === 0) {
      sub.textContent = "no runs yet";
      const empty = document.createElement("div");
      empty.className = "training-empty";
      const p = document.createElement("p");
      p.textContent = "No training runs yet. Sync a skill's dataset and start a run to train a policy in the cloud.";
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "training-empty-btn";
      btn.textContent = "+ Train a skill";
      btn.addEventListener("click", () => opts.onTrain());
      empty.append(p, btn);
      body.replaceChildren(empty);
      return;
    }

    const frag = document.createDocumentFragment();
    let totalRuns = 0;
    let shownGroups = 0;
    for (const sk of skills) {
      const runs = [...(sk.runs || [])].sort((a, b) => b.run_id - a.run_id).filter((r) => {
        if (mode === "active") return !isTerminal(r.status);
        if (mode === "completed") return isTerminal(r.status);
        return true;
      });
      totalRuns += (sk.runs || []).length;
      const nameMatch = sk.skill_name.toLowerCase().includes(query);
      const visibleRuns = query && !nameMatch ? runs.filter((r) => `#${r.run_id}`.includes(query)) : runs;
      if (query && !nameMatch && visibleRuns.length === 0) continue;
      shownGroups++;
      frag.appendChild(buildGroup(sk, visibleRuns));
    }
    sub.textContent = `${skills.length} skill${skills.length === 1 ? "" : "s"} · ${totalRuns} run${totalRuns === 1 ? "" : "s"}`;
    if (shownGroups === 0) {
      body.replaceChildren(msg("No runs match the current filter."));
    } else {
      body.replaceChildren(frag);
    }
  }

  /** @param {TrainingSkillStatus} sk @param {TrainingRunStatus[]} runs */
  function buildGroup(sk, runs) {
    const group = document.createElement("div");
    group.className = "skill-group";

    const gh = document.createElement("div");
    gh.className = "skill-group-head";
    const gname = document.createElement("span");
    gname.className = "skill-group-name";
    gname.textContent = sk.skill_name;
    const gmeta = document.createElement("span");
    gmeta.className = "skill-group-meta";
    const nRuns = (sk.runs || []).length;
    gmeta.textContent = `${nRuns} run${nRuns === 1 ? "" : "s"}`;
    const gtrain = document.createElement("button");
    gtrain.type = "button";
    gtrain.className = "skill-group-train";
    gtrain.textContent = "Train ▸";
    gtrain.addEventListener("click", () => {
      const skill = store.skills.find((s) => s.directory === sk.skill_dir);
      opts.onTrain(skill || /** @type {Skill} */ ({ id: sk.training_skill_id, name: sk.skill_name, type: "learned", episode_count: sk.uploaded_episode_count, directory: sk.skill_dir }));
    });
    gh.append(gname, gmeta, gtrain);
    group.appendChild(gh);
    const dsRow = buildDatasetRow(sk);
    if (dsRow) group.appendChild(dsRow);

    if (runs.length === 0) {
      group.appendChild(msg("No runs in this view.", "skill-group-empty"));
    } else {
      for (const run of runs) group.appendChild(buildRun(sk, run));
    }
    return group;
  }

  /** Dataset row: the live upload bar while transferring and/or a "Starting
   * run…" hint when a run is queued. We deliberately do NOT show a
   * synced/unsynced badge when idle — the robot only knows that *a* sync ran,
   * not whether the local dataset has changed since, so any such claim is
   * misleading. Returns null when there's nothing reliable to show.
   * @param {TrainingSkillStatus} sk @returns {HTMLElement | null} */
  function buildDatasetRow(sk) {
    const transferring = sk.has_active_transfer && sk.active_transfer;
    const pending = !!(store.pending && store.pending.has(sk.skill_dir));
    if (!transferring && !pending) return null;

    const ds = document.createElement("div");
    ds.className = "skill-group-dataset";
    const lbl = document.createElement("span");
    lbl.className = "microlabel";
    lbl.textContent = "dataset";
    ds.appendChild(lbl);

    if (transferring) {
      ds.appendChild(transferBar(sk.active_transfer, "Uploading"));
    }
    // A queued "start training" for this skill (survives modal close).
    if (pending) {
      const p = document.createElement("span");
      p.className = "ds-pending";
      p.textContent = "Starting run…";
      ds.appendChild(p);
    }
    return ds;
  }

  /** Lazily fetch a finished run's results info; re-render once it lands.
   * @param {TrainingSkillStatus} sk @param {TrainingRunStatus} run */
  function ensureRunInfo(sk, run) {
    const key = `${sk.skill_dir}#${run.run_id}`;
    if (runInfo.has(key)) return runInfo.get(key);
    if (infoInflight.has(key)) return undefined;
    infoInflight.add(key);
    fetch(`/run/info?dir=${encodeURIComponent(sk.skill_dir)}&id=${run.run_id}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`run/info ${r.status}`))))
      .then((info) => {
        infoInflight.delete(key);
        if (info.downloaded) runInfo.set(key, info); // only cache once results exist
        render();
        opts.onRunInfo?.(); // the detail pane derives Failed from the same info
      })
      .catch(() => infoInflight.delete(key));
    return undefined;
  }

  /** @param {TrainingSkillStatus} sk @param {TrainingRunStatus} run */
  function buildRun(sk, run) {
    const row = document.createElement("div");
    row.className = "run-row";
    const sel = store.selected;
    if (sel && sel.skillDir === sk.skill_dir && sel.runId === run.run_id) row.classList.add("is-selected");
    // Click the row (but not a button/link in it) to open it in the detail pane.
    row.addEventListener("click", (e) => {
      if (/** @type {HTMLElement} */ (e.target).closest("button, a")) return;
      opts.onSelect?.(sk.skill_dir, run.run_id);
    });

    const left = document.createElement("div");
    left.className = "run-left";
    const id = document.createElement("span");
    id.className = "run-id mono";
    id.textContent = `#${run.run_id}`;
    const dot = document.createElement("span");
    dot.className = `run-dot s-${run.status}`;
    const label = document.createElement("span");
    label.className = "run-label";
    label.textContent = LABEL[run.status] || "Unknown";
    // Outcome for finished runs. A terminal run with no checkpoint IS a failed
    // training run — say "Failed" with a red dot, not a green "Downloaded"
    // with the outcome buried in a chip.
    const info = run.status === STATUS.DOWNLOADED ? ensureRunInfo(sk, run) : undefined;
    if (run.status === STATUS.DOWNLOADED) {
      const chip = document.createElement("span");
      if (!info) {
        chip.className = "run-outcome oc-checking";
        chip.textContent = "checking…";
      } else if (info.has_checkpoint) {
        chip.className = "run-outcome oc-ok";
        chip.textContent = "✓ trained";
      } else {
        label.textContent = "Failed";
        dot.className = "run-dot s-failed";
        chip.className = "run-outcome oc-fail";
        chip.textContent = "✗ no checkpoint — see Logs";
      }
      label.appendChild(chip);
    }
    if (run.daemon_state && !isTerminal(run.status)) {
      const ds = document.createElement("span");
      ds.className = "run-daemon";
      ds.textContent = run.daemon_state;
      label.append(ds);
    }
    left.append(id, dot, label);

    const mid = document.createElement("div");
    mid.className = "run-mid";
    const meta = document.createElement("span");
    meta.className = "run-meta mono";
    meta.textContent = runMeta(run);
    mid.appendChild(meta);
    if (run.error_message) {
      const err = document.createElement("span");
      err.className = "run-error";
      err.textContent = run.error_message;
      mid.appendChild(err);
    } else if (info && !info.has_checkpoint && info.error_excerpt) {
      // No orchestrator message — show the exception line dug out of the
      // downloaded training logs, so the card says WHY it failed.
      const err = document.createElement("span");
      err.className = "run-error";
      err.textContent = info.error_excerpt;
      err.title = info.error_excerpt;
      mid.appendChild(err);
    }
    // Progress: download transfer (determinate) takes precedence; then a
    // determinate step bar once the trainer reports progress; else indeterminate.
    if (run.has_active_transfer && run.active_transfer) {
      mid.appendChild(transferBar(run.active_transfer, "Downloading"));
    } else if (run.status === STATUS.RUNNING && ((run.current_step || 0) > 0 || (run.total_step || 0) > 0)) {
      const key = `${sk.skill_dir}#${run.run_id}`;
      const eta = etaSeconds(key, run.current_step || 0, run.total_step || 0);
      mid.appendChild(stepBar(run, eta));
    } else if (run.status === STATUS.RUNNING || run.status === STATUS.BOOTING || run.status === STATUS.APPROVED) {
      mid.appendChild(runningBar());
    }

    const actions = document.createElement("div");
    actions.className = "run-actions";
    actions.append(...runActions(sk, run));

    row.append(left, mid, actions);
    return row;
  }

  /** Smoothed ETA in seconds for a running job, from how fast current_step
   * advances between snapshots. Returns null until a rate can be estimated.
   * @param {string} key @param {number} step @param {number} total */
  function etaSeconds(key, step, total) {
    const now = Date.now();
    const prev = etaTrack.get(key);
    let rate = prev?.rate || 0; // steps per ms
    if (prev && step > prev.step && now > prev.t) {
      const inst = (step - prev.step) / (now - prev.t);
      rate = prev.rate > 0 ? prev.rate * 0.7 + inst * 0.3 : inst; // EMA
      etaTrack.set(key, { step, t: now, rate });
    } else if (!prev || step < prev.step) {
      // first sighting, or a step reset (e.g. a fresh run reusing the key)
      etaTrack.set(key, { step, t: now, rate: 0 });
      rate = 0;
    }
    // (step === prev.step → unchanged snapshot: keep the existing rate/time)
    if (rate > 0 && total > step) return (total - step) / rate / 1000;
    return null;
  }

  /** @param {TrainingSkillStatus} sk @param {TrainingRunStatus} run @returns {HTMLElement[]} */
  function runActions(sk, run) {
    /** @type {HTMLElement[]} */
    const els = [];
    // Cancel any pre-terminal run (stops training + reaps the GPU instance).
    if (!isTerminal(run.status)) {
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "run-btn run-cancel";
      cancel.textContent = "Cancel";
      cancel.addEventListener("click", () => {
        if (!window.confirm(`Cancel run #${run.run_id}? This stops training and frees the GPU.`)) return;
        cancel.disabled = true;
        cancel.textContent = "Cancelling…";
        ros.callService(CANCEL_RUN_SERVICE, { skill_dir: sk.skill_dir, run_id: run.run_id })
          .then((res) => {
            if (res && res.success === false) throw new Error(res.message || "cancel failed");
          })
          .catch((err) => {
            cancel.disabled = false;
            cancel.textContent = "Cancel";
            window.alert(`Couldn't cancel: ${err.message}`);
          });
      });
      els.push(cancel);
    }
    const downloadable = run.status === STATUS.DONE || run.status === STATUS.DOWNLOADED;
    if (downloadable || run.has_active_transfer || run.transfer_done) {
      const dl = document.createElement("button");
      dl.type = "button";
      dl.className = "run-btn";
      dl.disabled = !downloadable || run.has_active_transfer;
      dl.textContent = run.has_active_transfer
        ? "Downloading…"
        : run.status === STATUS.DOWNLOADED
          ? "Re-download"
          : "Download";
      dl.addEventListener("click", () => {
        dl.disabled = true;
        ros.callService(DOWNLOAD_RESULTS_SERVICE, { skill_dir: sk.skill_dir, run_id: run.run_id }).catch((err) => {
          dl.disabled = false;
          window.alert(`Download failed: ${err.message}`);
        });
      });
      els.push(dl);
    }
    // Live logs while training — tail the orchestrator's in-memory log buffer.
    if (run.status === STATUS.RUNNING) {
      const live = document.createElement("button");
      live.type = "button";
      live.className = "run-btn";
      live.textContent = "Live logs";
      live.addEventListener("click", () => opts.onOpenLogs(sk.skill_dir, run.run_id, true));
      els.push(live);
    }
    // Logs — once results are downloaded, the run dir exists on the robot.
    if (run.status === STATUS.DOWNLOADED || run.transfer_done) {
      const logs = document.createElement("button");
      logs.type = "button";
      logs.className = "run-btn";
      logs.textContent = "Logs";
      logs.addEventListener("click", () => opts.onOpenLogs(sk.skill_dir, run.run_id));
      els.push(logs);
    }
    // W&B link — only when the run carries a URL (Phase 2 plumbing; hidden until then).
    if (run.wandb_url) {
      const wb = document.createElement("a");
      wb.className = "run-btn run-wandb";
      wb.href = run.wandb_url;
      wb.target = "_blank";
      wb.rel = "noopener";
      wb.textContent = "W&B";
      els.push(wb);
    }
    return els;
  }

  return {
    render,
    /** Build the action buttons for a run (reused by the detail pane). */
    actionsFor: runActions,
    /** Results info for a finished run (cached; triggers a fetch when absent).
     * Reused by the detail pane so both surfaces derive the same outcome. */
    infoFor: ensureRunInfo,
    destroy() {
      wrap.remove();
    },
  };
}

/** @param {TrainingRunStatus} run */
function runMeta(run) {
  const parts = [];
  if (run.instance_type) parts.push(run.instance_type);
  const start = run.started_at?.sec || 0;
  const end = run.finished_at?.sec || 0;
  if (start && end && end >= start) parts.push(fmtDur(end - start));
  else if (start) parts.push(`${fmtDur(Math.max(0, Math.floor(Date.now() / 1000) - start))} elapsed`);
  return parts.join(" · ");
}

/** @param {TransferProgress} t @param {string} verb @returns {HTMLElement} */
function transferBar(t, verb) {
  // Every stage (incl. the per-batch Verify) reports file_index/file_total, so
  // the bar tracks the real position — during a mid-upload verify it holds at
  // that batch's spot (e.g. 25/200) rather than jumping to 100% or 0.
  const pct = t.file_total > 0 ? Math.min(100, Math.round((t.file_index / t.file_total) * 100)) : 0;
  const wrap = document.createElement("div");
  wrap.className = "tbar";
  const fill = document.createElement("div");
  fill.className = "tbar-fill";
  fill.style.width = `${pct}%`;
  const lbl = document.createElement("span");
  lbl.className = "tbar-label mono";
  const stage = STAGE[t.stage] || verb;
  if (t.file_total <= 0) lbl.textContent = `${verb}…`;
  else lbl.textContent = `${stage} ${t.file_index}/${t.file_total} · ${pct}%`;
  wrap.append(fill, lbl);
  return wrap;
}

/** Determinate training-progress bar with step count + ETA. Falls back to a
 * step-only indeterminate bar when the total isn't known yet.
 * @param {TrainingRunStatus} run @param {number|null} eta @returns {HTMLElement} */
function stepBar(run, eta) {
  const step = run.current_step || 0;
  const total = run.total_step || 0;
  const wrap = document.createElement("div");
  const lbl = document.createElement("span");
  lbl.className = "tbar-label mono";
  if (total > 0) {
    const pct = Math.min(100, Math.round((step / total) * 100));
    wrap.className = "tbar";
    const fill = document.createElement("div");
    fill.className = "tbar-fill";
    fill.style.width = `${pct}%`;
    wrap.appendChild(fill);
    lbl.textContent =
      `Step ${step.toLocaleString()}/${total.toLocaleString()} · ${pct}%` +
      (eta != null ? ` · ~${fmtDur(Math.round(eta))} left` : "");
  } else {
    wrap.className = "tbar tbar-indeterminate";
    const fill = document.createElement("div");
    fill.className = "tbar-fill";
    wrap.appendChild(fill);
    lbl.textContent = `Step ${step.toLocaleString()}`;
  }
  wrap.appendChild(lbl);
  return wrap;
}

/** @returns {HTMLElement} indeterminate running bar */
function runningBar() {
  const wrap = document.createElement("div");
  wrap.className = "tbar tbar-indeterminate";
  const fill = document.createElement("div");
  fill.className = "tbar-fill";
  wrap.appendChild(fill);
  return wrap;
}

/** @param {string} text @param {string} [cls] @returns {HTMLElement} */
function msg(text, cls) {
  const p = document.createElement("p");
  p.className = cls || "datasets-empty";
  p.textContent = text;
  return p;
}

/** @param {number} secs @returns {string} */
function fmtDur(secs) {
  if (secs < 60) return `${secs}s`;
  const m = Math.floor(secs / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h${String(m % 60).padStart(2, "0")}m`;
}
