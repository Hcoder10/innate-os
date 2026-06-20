// @ts-check
// Recorded-movement wizard — the "mimic"/replay flow, a port of the mobile
// app's RecordReplayScreen. The draft skill has already been created + activated
// by the record panel; this drives the single take and the promote-to-replay:
//   Record   → new_episode   (start timer)
//   Stop     → stop_episode + save_episode  (one take captured → review)
//   Re-record→ back to idle (records another take, latest wins)
//   Save     → save_as_replay_skill  (promotes the draft in place, exits)
// Abandoning before save deletes the draft directory (delete_skill), stopping an
// open episode first so the recorder isn't left writing into a deleted dir.
// Recording is gated on the leader arm, same as the learned flow.

import {
  RECORDER_NEW_EPISODE_SERVICE,
  RECORDER_STOP_EPISODE_SERVICE,
  RECORDER_SAVE_EPISODE_SERVICE,
  RECORDER_CANCEL_EPISODE_SERVICE,
  SAVE_AS_REPLAY_SKILL_SERVICE,
  DELETE_SKILL_SERVICE,
} from "../constants.js";

/**
 * @param {HTMLElement} host
 * @param {import("../rosClient.js").RosClient} ros
 * @param {{ name: string, guidelines: string, draftDir: string, onExit: (savedName: string | null) => void }} opts
 * @returns {{ setArmReady: (ready: boolean) => void, destroy: () => void }}
 */
export function createReplayWizard(host, ros, opts) {
  const wrap = document.createElement("div");
  wrap.className = "record-wizard";

  // ---- header: movement name + cancel -------------------------------------
  const head = document.createElement("div");
  head.className = "record-wizard-head";
  const nameEl = document.createElement("span");
  nameEl.className = "record-wizard-name";
  nameEl.textContent = opts.name;
  nameEl.title = opts.name;
  const cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.className = "record-wizard-cancel";
  cancelBtn.textContent = "Cancel";
  head.append(nameEl, cancelBtn);

  // ---- controls (phase-dependent) -----------------------------------------
  const controls = document.createElement("div");
  controls.className = "record-controls";
  const recordBtn = button("record-start", "● Record");
  const stopBtn = button("record-stop", "■ Stop");
  const reRecordBtn = button("record-discard", "Re-record");
  const saveBtn = button("record-save", "✓ Save skill");
  controls.append(recordBtn, stopBtn, reRecordBtn, saveBtn);

  const hint = document.createElement("p");
  hint.className = "record-hint microlabel";

  wrap.append(head, controls, hint);
  host.appendChild(wrap);

  // ---- state --------------------------------------------------------------
  /** @type {"idle" | "recording" | "review"} */
  let phase = "idle";
  let armReady = false;
  let busy = false; // one in-flight recorder call at a time
  let saved = false;
  let recordingOpen = false; // an episode is open on the recorder
  let takes = 0;
  let destroyed = false;

  // ---- timer --------------------------------------------------------------
  /** @type {number | null} */
  let startMs = null;
  let elapsedMs = 0;
  /** @type {number | null} */
  let timerId = null;

  function startTimer() {
    startMs = Date.now();
    elapsedMs = 0;
    if (timerId === null) timerId = window.setInterval(tickTimer, 500);
  }
  function tickTimer() {
    if (startMs !== null) elapsedMs = Date.now() - startMs;
    stopBtn.textContent = `■ Stop  ${formatTime(elapsedMs)}`;
  }
  function resetTimer() {
    if (timerId !== null) {
      clearInterval(timerId);
      timerId = null;
    }
    startMs = null;
    elapsedMs = 0;
  }

  function canRecord() {
    return phase === "idle" && armReady && !busy;
  }

  /** Run a guarded recorder call; surfaces failures and clears busy. @param {() => Promise<void>} fn */
  async function run(fn) {
    if (busy) return;
    busy = true;
    render();
    try {
      await fn();
    } catch (err) {
      console.error("[collect] replay step failed:", err);
    } finally {
      busy = false;
      render();
    }
  }

  recordBtn.addEventListener("click", () => {
    if (!canRecord()) return;
    run(async () => {
      const res = await ros.callService(RECORDER_NEW_EPISODE_SERVICE, {});
      if (res && res.success === false) throw new Error(res.message || "couldn't start");
      recordingOpen = true;
      phase = "recording";
      startTimer();
      tickTimer();
    });
  });

  stopBtn.addEventListener("click", () => {
    if (busy || phase !== "recording") return;
    run(async () => {
      const stopped = await ros.callService(RECORDER_STOP_EPISODE_SERVICE, {});
      if (stopped && stopped.success === false) throw new Error(stopped.message || "couldn't stop");
      recordingOpen = false;
      try {
        const savedEp = await ros.callService(RECORDER_SAVE_EPISODE_SERVICE, {});
        if (savedEp && savedEp.success === false) throw new Error(savedEp.message || "couldn't save take");
      } catch (err) {
        // Stop succeeded but the take didn't save → a stopped-but-unsaved episode
        // is stranded on the recorder. Cancel it and drop back to idle so the user
        // can re-record, instead of wedging in "recording" with a Stop button that
        // re-fires on a closed episode.
        await ros.callService(RECORDER_CANCEL_EPISODE_SERVICE, {}).catch(() => {});
        phase = "idle";
        resetTimer();
        throw err;
      }
      takes += 1;
      phase = "review";
      resetTimer();
    });
  });

  reRecordBtn.addEventListener("click", () => {
    if (busy || phase !== "review") return;
    phase = "idle";
    render();
  });

  saveBtn.addEventListener("click", () => {
    if (busy || phase !== "review") return;
    run(async () => {
      const res = await ros.callService(SAVE_AS_REPLAY_SKILL_SERVICE, {
        task_directory: opts.draftDir,
        name: opts.name,
        guidelines: opts.guidelines,
        episode_id: -1, // latest take
      });
      if (!res || res.success === false) throw new Error((res && res.message) || "couldn't save skill");
      saved = true; // keep the directory — it's a real skill now
      opts.onExit(opts.name);
    });
  });

  cancelBtn.addEventListener("click", () => {
    if (busy) return;
    if (takes > 0 && !window.confirm("Discard this recorded movement? The take won't be saved.")) return;
    opts.onExit(null); // record panel destroys us → abandon cleanup runs
  });

  // ---- render -------------------------------------------------------------
  function render() {
    recordBtn.hidden = phase !== "idle";
    stopBtn.hidden = phase !== "recording";
    reRecordBtn.hidden = phase !== "review";
    saveBtn.hidden = phase !== "review";

    recordBtn.textContent = takes > 0 ? "● Record again" : "● Record";
    recordBtn.disabled = !canRecord();
    stopBtn.disabled = busy;
    reRecordBtn.disabled = busy;
    saveBtn.disabled = busy;
    cancelBtn.disabled = busy;

    const msg = phase === "idle" && !armReady ? "Engage the leader arm to start recording" : "";
    hint.textContent = msg;
    hint.hidden = msg === "";
  }

  render();

  return {
    /** @param {boolean} ready */
    setArmReady(ready) {
      if (armReady === ready) return;
      armReady = ready;
      render();
    },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      resetTimer();
      // Abandon cleanup: delete the draft we created but never promoted. Stop an
      // open episode first so the recorder isn't writing into a deleted dir.
      if (!saved) {
        const stopFirst = recordingOpen
          ? ros.callService(RECORDER_STOP_EPISODE_SERVICE, {}).catch(() => {})
          : Promise.resolve();
        stopFirst.finally(() =>
          ros.callService(DELETE_SKILL_SERVICE, { skill_directory: opts.draftDir }).catch(() => {}),
        );
      }
      wrap.remove();
    },
  };
}

/** @param {string} cls @param {string} text */
function button(cls, text) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = `record-btn ${cls}`;
  b.textContent = text;
  return b;
}

/** Elapsed milliseconds → MM:SS. @param {number} ms */
function formatTime(ms) {
  const total = Math.floor(ms / 1000);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}
