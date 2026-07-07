// @ts-check
// SPDX-License-Identifier: Apache-2.0
// Copyright (c) 2026 Innate Inc
// Training page entry — a global dashboard of cloud training runs, grouped by
// skill, driven by the latched /innate_training/job_statuses topic. Disconnected:
// the shared connect card. Connected: the dashboard, plus a Configure drawer for
// setting hyperparameters and starting a run (which auto-syncs first).

import { ros } from "../rosClient.js";
import { mountPage } from "../pageMount.js";
import { JOB_STATUSES_TOPIC, AVAILABLE_SKILLS_TOPIC, START_TRAINING_SERVICE } from "../constants.js";
import { createRunDashboard } from "./runDashboard.js";
import { createRunDetail } from "./runDetail.js";
import { createConfigurePanel } from "./configurePanel.js";
import { createLogsModal } from "./logsModal.js";

const STATUS_RUNNING = 5;

/** @param {HTMLElement} stage */
export function mount(stage) {
  return mountPage(stage, "training", buildView);
}

/**
 * Small shared store: the latest job list + skill roster, with an update
 * subscription so the dashboard and the configure panel both stay live.
 * @param {HTMLElement} root
 */
function buildView(root) {
  const store = {
    /** @type {TrainingJobList | null} */
    jobList: null,
    /** @type {Skill[]} */
    skills: [],
    /** Run open in the detail pane. @type {{ skillDir: string, runId: number } | null} */
    selected: null,
    /** Whether the user has explicitly chosen a run (suppresses auto-select). */
    _userPicked: false,
    /** @type {Set<() => void>} */
    _listeners: new Set(),
    /** @param {string | null} skillDir @param {number} [runId] */
    selectRun(skillDir, runId) {
      this.selected = skillDir == null ? null : { skillDir, runId: /** @type {number} */ (runId) };
      // Any explicit pick or close hands selection control to the user, so
      // autoSelect stops managing it (it won't reopen a closed pane).
      this._userPicked = true;
      this._emit();
    },
    /** @param {string} dir @returns {TrainingSkillStatus | null} */
    statusFor(dir) {
      return this.jobList?.skills?.find((s) => s.skill_dir === dir) || null;
    },
    /** @param {() => void} cb @returns {() => void} */
    onUpdate(cb) {
      this._listeners.add(cb);
      return () => this._listeners.delete(cb);
    },
    _emit() {
      for (const cb of [...this._listeners]) {
        try {
          cb();
        } catch (err) {
          console.error("[training] listener threw:", err);
        }
      }
    },

    // Skills with a just-issued "start training" — purely for the optimistic
    // "Starting run…" hint. The robot owns the actual submit→upload→create_run
    // (start_training service), so this is cosmetic and the page can close.
    // Value = run count at request time, used to clear the hint once it lands.
    /** @type {Map<string, number>} */
    pending: new Map(),
    /** @param {string} dir @param {TrainingParams} runParams @param {number} _episodeCount */
    startTraining(dir, runParams, _episodeCount) {
      const st = this.statusFor(dir);
      this.pending.set(dir, st?.runs?.length ?? 0);
      ros.callService(START_TRAINING_SERVICE, { skill_dir: dir, run_params: runParams }).catch((err) => {
        console.error("[training] start_training failed:", err);
        this.pending.delete(dir);
        this._emit();
      });
      this._emit();
    },
    _prunePending() {
      for (const [dir, base] of this.pending) {
        const st = this.statusFor(dir);
        // Clear the hint once the upload visibly starts or a new run appears.
        if (st && (st.has_active_transfer || (st.runs?.length ?? 0) > base)) this.pending.delete(dir);
      }
    },
  };

  const dashHost = document.createElement("div");
  dashHost.className = "training-dash";
  const detailHost = document.createElement("div");
  detailHost.className = "training-detail";
  const drawerHost = document.createElement("div");
  root.append(dashHost, detailHost, drawerHost);

  /** @type {{ destroy: () => void } | null} */
  let panel = null;
  function closeConfigure() {
    if (panel) {
      panel.destroy();
      panel = null;
    }
  }
  /** @param {Skill} [skill] preselected skill */
  function openConfigure(skill) {
    closeConfigure();
    panel = createConfigurePanel(drawerHost, ros, store, {
      skill,
      onClose: closeConfigure,
      onStart: (dir, runParams, episodeCount) => store.startTraining(dir, runParams, episodeCount),
    });
  }

  /** @type {{ destroy: () => void } | null} */
  let logs = null;
  function closeLogs() {
    if (logs) {
      logs.destroy();
      logs = null;
    }
  }
  /** @param {string} skillDir @param {number} runId @param {boolean} [live] */
  function openLogs(skillDir, runId, live) {
    closeLogs();
    logs = createLogsModal(drawerHost, skillDir, runId, { onClose: closeLogs, ros, live: !!live });
  }

  const dashboard = createRunDashboard(dashHost, ros, store, {
    onTrain: openConfigure,
    onOpenLogs: openLogs,
    onSelect: (dir, runId) => store.selectRun(dir, runId),
    onRunInfo: () => detail.render(), // outcome may flip Downloaded → Failed
  });
  const detail = createRunDetail(detailHost, ros, store, {
    onClose: () => store.selectRun(null),
    onOpenLogs: openLogs,
    actionsFor: dashboard.actionsFor,
    infoFor: dashboard.infoFor,
  });
  const unsubRender = store.onUpdate(() => {
    dashboard.render();
    detail.render();
  });

  // Auto-select the running run until the user picks one explicitly, so the
  // detail pane is populated by default (and tracks a fresh run starting).
  function autoSelect() {
    if (store._userPicked) return;
    let running = null;
    for (const sk of store.jobList?.skills || []) {
      const run = (sk.runs || []).find((r) => r.status === STATUS_RUNNING);
      if (run) {
        running = { skillDir: sk.skill_dir, runId: run.run_id };
        break;
      }
    }
    const cur = store.selected;
    if (running && (!cur || cur.skillDir !== running.skillDir || cur.runId !== running.runId)) {
      store.selected = running;
    } else if (!running && cur) {
      store.selected = null;
    }
  }

  const unsubJobs = ros.subscribe(JOB_STATUSES_TOPIC, (msg) => {
    store.jobList = /** @type {TrainingJobList} */ (msg);
    store._prunePending(); // clear "Starting run…" hints that have landed
    autoSelect();
    store._emit();
  });
  const unsubSkills = ros.subscribe(AVAILABLE_SKILLS_TOPIC, (msg) => {
    const all = Array.isArray(msg?.skills) ? /** @type {Skill[]} */ (msg.skills) : [];
    store.skills = all.filter((s) => s && s.directory).sort((a, b) => a.name.localeCompare(b.name));
    store._emit();
  });

  return {
    destroy() {
      unsubRender();
      unsubJobs();
      unsubSkills();
      closeConfigure();
      closeLogs();
      detail.destroy();
      dashboard.destroy();
      root.innerHTML = "";
    },
  };
}
