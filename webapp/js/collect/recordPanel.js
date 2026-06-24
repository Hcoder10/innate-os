// @ts-check
// Recording HUD — the Collect page's net-new UI, overlaid on the reused Teleop
// cockpit. It hosts skill selection + creation and two recording flows that
// mirror the mobile app's "Add skill" chooser:
//
//   Learned skill     — record many episodes into a training dataset, reviewed
//                       on the Datasets page (this panel's idle→recording→
//                       completed loop; new/stop/save/cancel_episode).
//   Recorded movement — record one motion the robot replays deterministically;
//                       handed off to the replay wizard (save_as_replay_skill).
//
// Picking a learned skill points the recorder at it once via
// activate_physical_primitive. Recording is gated on the leader arm being
// engaged & streaming — the webapp equivalent of the mobile app's
// `isArmPublishing && armUpdateRate > 0` — fed in through setArmReady().

import {
  AVAILABLE_SKILLS_TOPIC,
  ROBOT_INFO_TOPIC,
  ACTIVATE_PHYSICAL_PRIMITIVE_SERVICE,
  CREATE_PHYSICAL_SKILL_SERVICE,
  RECORDER_NEW_EPISODE_SERVICE,
  RECORDER_STOP_EPISODE_SERVICE,
  RECORDER_SAVE_EPISODE_SERVICE,
  RECORDER_CANCEL_EPISODE_SERVICE,
  RECORDER_STATUS_TOPIC,
} from "../constants.js";
import { createReplayWizard } from "./replayWizard.js";

const NEW_VALUE = "__new__";

/**
 * @param {HTMLElement} parent
 * @param {import("../rosClient.js").RosClient} ros
 * @param {{ modalRoot?: HTMLElement, onHeadControl?: (allowed: boolean) => void }} [opts]
 *   modalRoot is a full-bleed container for the new-skill modal (defaults to
 *   parent). onHeadControl(allowed) toggles head-tilt availability: false for
 *   learned-policy recording (fixed viewpoint), true in the recorded-movement
 *   wizard (head motion is captured/replayed).
 * @returns {{ setArmReady: (ready: boolean) => void, destroy: () => void }}
 */
export function createRecordPanel(parent, ros, opts = {}) {
  const modalRoot = opts.modalRoot || parent;
  const wrap = document.createElement("div");
  wrap.className = "record-panel";

  // Two faces of the HUD: the learned multi-episode flow and the replay wizard.
  const learnedHost = document.createElement("div");
  learnedHost.className = "record-learned";
  const wizardHost = document.createElement("div");
  wizardHost.className = "record-wizardhost";
  wizardHost.hidden = true;
  wrap.append(learnedHost, wizardHost);

  // ---- learned: top row (skill picker + episode count + review link) ------
  const top = document.createElement("div");
  top.className = "record-top";

  const sel = document.createElement("select");
  sel.className = "record-skill";
  sel.setAttribute("aria-label", "Skill to record into");

  const count = document.createElement("span");
  count.className = "record-count mono";

  const link = document.createElement("a");
  link.className = "record-link";
  link.href = "../datasets/index.html";
  link.textContent = "Review →";
  link.title = "Browse, label and delete episodes on the Datasets page";

  top.append(sel, count, link);

  // ---- learned: controls + gate hint --------------------------------------
  const controls = document.createElement("div");
  controls.className = "record-controls";
  const startBtn = button("record-start", "● Record");
  const stopBtn = button("record-stop", "■ Stop");
  const saveBtn = button("record-save", "Save");
  const discardBtn = button("record-discard", "Discard");
  controls.append(startBtn, stopBtn, saveBtn, discardBtn);

  const hint = document.createElement("p");
  hint.className = "record-hint microlabel";

  learnedHost.append(top, controls, hint);

  // Orphaned-recording recovery banner. Shown when the robot reports an episode
  // still open while this panel is idle — typically a recording that outlived a
  // dropped connection (on reconnect we get the latched recorder status). Sits
  // above both faces of the HUD so it's visible in either mode.
  const orphanBar = document.createElement("div");
  orphanBar.className = "record-orphan";
  orphanBar.hidden = true;
  const orphanMsg = document.createElement("span");
  orphanMsg.className = "record-orphan-msg";
  const orphanDiscard = button("record-orphan-discard", "Discard recording");
  const orphanDismiss = button("record-orphan-dismiss", "✕");
  orphanDismiss.title = "Dismiss";
  orphanBar.append(orphanMsg, orphanDiscard, orphanDismiss);
  wrap.prepend(orphanBar);

  parent.appendChild(wrap);

  // ---- state --------------------------------------------------------------
  /** @type {Skill[]} */
  let skills = [];
  /** @type {string | null} */
  let selectedDir = null;
  let selectedName = "";
  let savedCount = 0;
  let activated = false;
  let activating = false;
  let armReady = false;
  /** @type {"idle" | "recording" | "completed"} */
  let recState = "idle";
  let busy = false; // one in-flight new/stop/save/cancel at a time
  let activateSeq = 0;
  let digitalSkills = false; // robot supports recorded-movement (replay) skills
  /** @type {string} flashed after a replay skill is saved, until next render */
  let flash = "";
  // Orphaned open episode on the robot (from the latched recorder status).
  let orphanOpen = false;
  let orphanDir = "";
  let orphanBusy = false;

  /** @type {{ setArmReady: (r: boolean) => void, destroy: () => void } | null} */
  let wizard = null;

  // Deep link from the Datasets page: ?dir=<task_directory>. Selected as soon
  // as a matching skill appears in the roster, then cleared so it fires once.
  let pendingDir = new URLSearchParams(location.search).get("dir");

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

  // ---- learned skill selection --------------------------------------------

  /** @param {string} dir @param {string} name @param {number} epCount */
  function applySelection(dir, name, epCount) {
    flash = "";
    selectedDir = dir;
    selectedName = name;
    savedCount = epCount;
    activate();
    renderSelect();
    render();
  }

  /** Point the recorder at the selected dataset (mobile: DatasetSection.handleRecord). */
  function activate() {
    if (!selectedDir) return;
    const dir = selectedDir;
    const seq = ++activateSeq;
    activated = false;
    activating = true;
    render();
    ros.callService(ACTIVATE_PHYSICAL_PRIMITIVE_SERVICE, { task_directory: dir })
      .then((res) => {
        if (seq !== activateSeq) return; // a newer selection superseded this
        if (res && res.success === false) throw new Error(res.message || "activate failed");
        activated = true;
      })
      .catch((err) => {
        if (seq !== activateSeq) return;
        activated = false;
        console.error("[collect] activate failed:", err);
      })
      .finally(() => {
        if (seq !== activateSeq) return;
        activating = false;
        render();
      });
  }

  function renderSelect() {
    const frag = document.createDocumentFragment();
    frag.appendChild(option("", "Select skill…"));
    // A freshly created dataset may not be in the roster yet — keep it visible.
    if (selectedDir && !skills.some((s) => s.directory === selectedDir)) {
      frag.appendChild(option(selectedDir, selectedName || selectedDir));
    }
    for (const s of skills) {
      const n = s.episode_count ?? 0;
      frag.appendChild(option(s.directory || "", `${s.name} · ${n} ep${n === 1 ? "" : "s"}`));
    }
    frag.appendChild(option(NEW_VALUE, "+ New skill…"));
    sel.replaceChildren(frag);
    sel.value = selectedDir ?? "";
  }

  sel.addEventListener("change", () => {
    const v = sel.value;
    if (v === NEW_VALUE) {
      sel.value = selectedDir ?? ""; // revert; the modal drives the real change
      openNewSkillModal();
      return;
    }
    if (v === "") {
      selectedDir = null;
      selectedName = "";
      activated = false;
      activateSeq++; // cancel any in-flight activation
      activating = false;
      render();
      return;
    }
    const skill = skills.find((s) => s.directory === v);
    if (skill && skill.directory) applySelection(skill.directory, skill.name, skill.episode_count ?? 0);
  });

  const unsubSkills = ros.subscribe(AVAILABLE_SKILLS_TOPIC, (msg) => {
    const all = Array.isArray(msg?.skills) ? /** @type {Skill[]} */ (msg.skills) : [];
    // Learned datasets only — replay skills are finished, not recorded into.
    skills = all
      .filter((s) => s && s.directory && s.type !== "replay")
      .sort((a, b) => a.name.localeCompare(b.name));
    // Honor a pending deep link once its skill shows up.
    if (pendingDir) {
      const match = skills.find((s) => s.directory === pendingDir);
      if (match && match.directory) {
        pendingDir = null;
        applySelection(match.directory, match.name, match.episode_count ?? 0);
        return; // applySelection already re-rendered
      }
    }
    renderSelect();
    render();
  });

  const unsubInfo = ros.subscribe(ROBOT_INFO_TOPIC, (payload) => {
    if (typeof payload?.data !== "string") return;
    try {
      /** @type {RobotInfo} */
      const info = JSON.parse(payload.data);
      const next = info.supports_digital_skills === true;
      if (next !== digitalSkills) {
        digitalSkills = next;
        chooserRerender?.(); // refresh the type chooser if it's open
      }
    } catch {
      // ignore malformed payloads
    }
  });

  // ---- new-skill modal: type chooser → name (+ guidelines for replay) ------

  /** @type {HTMLElement | null} */
  let modal = null;
  /** @type {(() => void) | null} */
  let chooserRerender = null;

  function openNewSkillModal() {
    if (modal) return;
    const backdrop = document.createElement("div");
    backdrop.className = "modal-backdrop";
    backdrop.addEventListener("click", closeModal);

    const panel = document.createElement("div");
    panel.className = "modal";
    panel.addEventListener("click", (e) => e.stopPropagation());

    const head = document.createElement("div");
    head.className = "modal-head";
    const title = document.createElement("h2");
    title.className = "modal-title";
    title.textContent = "New skill";
    const close = document.createElement("button");
    close.type = "button";
    close.className = "modal-close";
    close.textContent = "✕";
    close.addEventListener("click", closeModal);
    head.append(title, close);

    const body = document.createElement("div");
    body.className = "modal-body";

    panel.append(head, body);
    backdrop.appendChild(panel);
    modalRoot.appendChild(backdrop);
    modal = backdrop;

    showChooser();

    /** Step 1 — pick the skill type (mobile: AddSkillScreen). */
    function showChooser() {
      chooserRerender = showChooser;
      title.textContent = "New skill";
      body.replaceChildren(
        choiceCard(
          "Learned skill",
          "Record several demonstrations, then train the robot to do it on its own.",
          false,
          () => showNameStep("learned"),
        ),
        choiceCard(
          "Recorded movement",
          digitalSkills
            ? "Record one movement and the robot plays it back exactly. No training."
            : "Update your robot's software to record movements.",
          !digitalSkills,
          () => showNameStep("replay"),
        ),
      );
    }

    /** Step 2 — name the skill (+ guidelines for a recorded movement). */
    function showNameStep(/** @type {"learned" | "replay"} */ kind) {
      chooserRerender = null;
      title.textContent = kind === "replay" ? "Recorded movement" : "Learned skill";

      const back = document.createElement("button");
      back.type = "button";
      back.className = "modal-back";
      back.textContent = "← Back";
      back.addEventListener("click", showChooser);

      const field = document.createElement("label");
      field.className = "modal-field";
      const flabel = document.createElement("span");
      flabel.className = "modal-field-label";
      flabel.textContent = "Name";
      const nameInput = document.createElement("input");
      nameInput.className = "modal-input";
      nameInput.type = "text";
      nameInput.placeholder = kind === "replay" ? "e.g. wave hello" : "e.g. pick up the cube";
      nameInput.autocomplete = "off";
      field.append(flabel, nameInput);

      /** @type {HTMLTextAreaElement | null} */
      let notes = null;
      /** @type {HTMLElement[]} */
      const children = [back, field];
      if (kind === "replay") {
        const gField = document.createElement("label");
        gField.className = "modal-field";
        const gLabel = document.createElement("span");
        gLabel.className = "modal-field-label";
        gLabel.textContent = "When should the robot do this? (optional)";
        notes = document.createElement("textarea");
        notes.className = "modal-input modal-textarea";
        notes.placeholder = "e.g. when greeting a person it sees";
        gField.append(gLabel, notes);
        children.push(gField);
      }

      const warn = document.createElement("p");
      warn.className = "modal-warn";
      const submitBtn = document.createElement("button");
      submitBtn.type = "button";
      submitBtn.className = "modal-start";
      submitBtn.textContent = kind === "replay" ? "Set up recording" : "Create skill";
      children.push(warn, submitBtn);

      function submit() {
        const name = nameInput.value.trim();
        if (!name) {
          warn.textContent = "Give the skill a name.";
          return;
        }
        if (skills.some((s) => slug(s.name) === slug(name))) {
          warn.textContent = "A skill with this name already exists.";
          return;
        }
        submitBtn.disabled = true;
        nameInput.disabled = true;
        if (notes) notes.disabled = true;
        warn.textContent = "";
        ros.callService(CREATE_PHYSICAL_SKILL_SERVICE, { name })
          .then((res) => {
            if (!res || res.success === false || !res.skill_directory) {
              throw new Error((res && res.message) || "Couldn't create skill");
            }
            if (kind === "replay") {
              return enterReplay(res.skill_directory, name, notes ? notes.value.trim() : "");
            }
            closeModal();
            applySelection(res.skill_directory, name, 0);
          })
          .catch((err) => {
            warn.textContent = err instanceof Error ? err.message : "Couldn't create skill";
            submitBtn.disabled = false;
            nameInput.disabled = false;
            if (notes) notes.disabled = false;
          });
      }
      submitBtn.addEventListener("click", submit);
      nameInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          submit();
        }
      });

      body.replaceChildren(...children);
      nameInput.focus();
    }
  }

  function closeModal() {
    chooserRerender = null;
    if (modal) {
      modal.remove();
      modal = null;
    }
  }

  // ---- recorded-movement wizard handoff -----------------------------------

  /** Create+activate the draft, then swap the HUD into the replay wizard.
   * @param {string} draftDir @param {string} name @param {string} guidelines */
  function enterReplay(draftDir, name, guidelines) {
    return ros.callService(ACTIVATE_PHYSICAL_PRIMITIVE_SERVICE, { task_directory: draftDir })
      .then((res) => {
        if (res && res.success === false) throw new Error(res.message || "couldn't set up recording");
        closeModal();
        // Pause the learned flow's selection so its services don't compete.
        activateSeq++;
        learnedHost.hidden = true;
        wizardHost.hidden = false;
        opts.onHeadControl?.(true); // head motion is part of a recorded movement
        wizard = createReplayWizard(wizardHost, ros, {
          name,
          guidelines,
          draftDir,
          onExit: (savedName) => {
            if (wizard) {
              wizard.destroy();
              wizard = null;
            }
            opts.onHeadControl?.(false); // back to learned mode — lock the head
            wizardHost.hidden = true;
            learnedHost.hidden = false;
            flash = savedName ? `Saved “${savedName}” — use the mobile app to run this skill` : "";
            // Re-activate the previously selected learned skill, if any.
            if (selectedDir) activate();
            renderSelect();
            render();
          },
        });
        wizard.setArmReady(armReady);
      });
  }

  // ---- learned recording state machine (mobile: RecordEpisodeScreen) ------

  function canRecord() {
    return recState === "idle" && !!selectedDir && activated && armReady && !activating && !busy;
  }

  startBtn.addEventListener("click", () => {
    if (!canRecord()) return;
    busy = true;
    render();
    ros.callService(RECORDER_NEW_EPISODE_SERVICE, {})
      .then((res) => {
        if (res && res.success === false) throw new Error(res.message || "couldn't start");
        recState = "recording";
        startTimer();
        tickTimer();
      })
      .catch((err) => console.error("[collect] start episode failed:", err))
      .finally(() => {
        busy = false;
        render();
      });
  });

  stopBtn.addEventListener("click", () => {
    if (busy || recState !== "recording") return;
    busy = true;
    render();
    ros.callService(RECORDER_STOP_EPISODE_SERVICE, {})
      .then((res) => {
        if (res && res.success === false) throw new Error(res.message || "couldn't stop");
        recState = "completed";
        resetTimer();
      })
      .catch((err) => console.error("[collect] stop episode failed:", err))
      .finally(() => {
        busy = false;
        render();
      });
  });

  saveBtn.addEventListener("click", () => {
    if (busy || recState !== "completed") return;
    busy = true;
    render();
    ros.callService(RECORDER_SAVE_EPISODE_SERVICE, {})
      .then((res) => {
        if (res && res.success === false) throw new Error(res.message || "couldn't save");
        savedCount += 1;
        recState = "idle";
        resetTimer();
      })
      .catch((err) => console.error("[collect] save episode failed:", err))
      .finally(() => {
        busy = false;
        render();
      });
  });

  discardBtn.addEventListener("click", () => {
    if (busy || recState !== "completed") return;
    busy = true;
    render();
    ros.callService(RECORDER_CANCEL_EPISODE_SERVICE, {})
      .then((res) => {
        if (res && res.success === false) throw new Error(res.message || "couldn't discard");
        recState = "idle";
        resetTimer();
      })
      .catch((err) => console.error("[collect] discard episode failed:", err))
      .finally(() => {
        busy = false;
        render();
      });
  });

  // ---- orphaned-recording recovery (latched recorder status) --------------

  /** @param {{ task_directory?: string, episode_number?: string, status?: string }} msg */
  function onRecorderStatus(msg) {
    const status = msg?.status;
    // While our own new/stop/save/cancel is in flight, our optimistic
    // transitions own the state — don't fight them.
    if (!busy) {
      // The robot can change the episode state without us driving it: it
      // auto-stops a recording at the max-length limit ("stopped"), and another
      // client or a failure can save/cancel. Reconcile so the HUD never wedges
      // (e.g. stuck on "Stop" with the timer running and Save unreachable).
      if (recState === "recording" && status === "stopped") {
        recState = "completed"; // surface Save / Discard
        resetTimer();
        render();
      } else if (
        (recState === "recording" || recState === "completed") &&
        (status === "saved" || status === "cancelled" || status === "idle")
      ) {
        recState = "idle"; // the open episode is gone — don't strand the HUD on it
        resetTimer();
        render();
      }
    }

    // An episode is genuinely OPEN only when it's recording or
    // stopped-but-unsaved. The recorder publishes "active" for two different
    // cases: a skill merely being selected/activated (no episode — empty
    // episode_number), and an episode actually recording (episode_number set).
    // Requiring an episode_number is what stops the banner firing just from
    // picking a skill. "stopped" always carries one.
    const episodeOpen = status === "stopped" || (status === "active" && !!msg?.episode_number);
    if (episodeOpen && recState === "idle" && !busy) {
      orphanOpen = true;
      orphanDir = msg.task_directory || "";
    } else if (!episodeOpen) {
      orphanOpen = false;
    }
    renderOrphan();
  }

  function discardOrphan() {
    if (orphanBusy) return;
    orphanBusy = true;
    renderOrphan();
    // cancel_episode discards an active OR stopped episode on the recorder; the
    // recorder then publishes "cancelled", which clears the banner via the sub.
    ros.callService(RECORDER_CANCEL_EPISODE_SERVICE, {})
      .then((res) => {
        if (res && res.success === false) throw new Error(res.message || "couldn't discard");
        orphanOpen = false;
      })
      .catch((err) => console.error("[collect] discard orphaned episode failed:", err))
      .finally(() => {
        orphanBusy = false;
        renderOrphan();
      });
  }

  function renderOrphan() {
    orphanBar.hidden = !orphanOpen;
    if (!orphanOpen) return;
    const which = orphanDir ? skills.find((s) => s.directory === orphanDir)?.name : "";
    orphanMsg.textContent = which
      ? `The robot still has an unfinished recording open for “${which}” — discard it to start a new episode.`
      : "The robot still has an unfinished recording open — discard it to start a new episode.";
    orphanDiscard.disabled = orphanBusy;
    orphanDiscard.textContent = orphanBusy ? "Discarding…" : "Discard recording";
  }

  orphanDiscard.addEventListener("click", discardOrphan);
  orphanDismiss.addEventListener("click", () => {
    orphanOpen = false;
    renderOrphan();
  });

  const unsubStatus = ros.subscribe(RECORDER_STATUS_TOPIC, onRecorderStatus);

  // ---- render -------------------------------------------------------------

  function render() {
    // Locking the picker mid-episode keeps the recorder pointed where it began.
    sel.disabled = recState !== "idle" || activating || busy;

    // Review deep-links to the selected skill on the Datasets page.
    link.href = selectedDir
      ? `../datasets/index.html?dir=${encodeURIComponent(selectedDir)}`
      : "../datasets/index.html";

    count.textContent = `${savedCount} episode${savedCount === 1 ? "" : "s"}`;

    startBtn.hidden = recState !== "idle";
    stopBtn.hidden = recState !== "recording";
    saveBtn.hidden = recState !== "completed";
    discardBtn.hidden = recState !== "completed";

    startBtn.disabled = !canRecord();
    stopBtn.disabled = busy;
    saveBtn.disabled = busy;
    discardBtn.disabled = busy;

    if (recState === "idle") {
      const msg = flash
        ? flash
        : !selectedDir
          ? "Select or create a skill to record into"
          : activating
            ? "Preparing skill…"
            : !activated
              ? "Couldn't prepare skill — reselect it"
              : !armReady
                ? "Engage the leader arm to start recording"
                : "";
      hint.textContent = msg;
      hint.hidden = msg === "";
      hint.classList.toggle("ok", !!flash);
    } else {
      hint.hidden = true;
      hint.classList.remove("ok");
    }
  }

  renderSelect();
  render();
  opts.onHeadControl?.(false); // learned recording starts with head locked

  return {
    /** @param {boolean} ready */
    setArmReady(ready) {
      if (armReady === ready) return;
      armReady = ready;
      if (wizard) wizard.setArmReady(ready);
      render();
    },
    destroy() {
      activateSeq++;
      resetTimer();
      closeModal();
      if (wizard) {
        wizard.destroy(); // abandons an unsaved draft
        wizard = null;
      }
      unsubSkills();
      unsubInfo();
      unsubStatus();
      wrap.remove();
    },
  };
}

/** A type-chooser card (mobile: AddSkillScreen OptionCard).
 * @param {string} titleText @param {string} desc @param {boolean} disabled @param {() => void} onClick */
function choiceCard(titleText, desc, disabled, onClick) {
  const card = document.createElement("button");
  card.type = "button";
  card.className = "collect-choice";
  card.disabled = disabled;
  const t = document.createElement("span");
  t.className = "collect-choice-title";
  t.textContent = titleText;
  const d = document.createElement("span");
  d.className = "collect-choice-desc";
  d.textContent = desc;
  card.append(t, d);
  if (!disabled) card.addEventListener("click", onClick);
  return card;
}

/** @param {string} cls @param {string} text */
function button(cls, text) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = `record-btn ${cls}`;
  b.textContent = text;
  return b;
}

/** @param {string} value @param {string} text */
function option(value, text) {
  const o = document.createElement("option");
  o.value = value;
  o.textContent = text;
  return o;
}

/** Slug for name-collision checks (mobile: RecordReplayScreen.slug). @param {string} s */
function slug(s) {
  return s
    .replace(/[^a-zA-Z0-9\s-]/g, "")
    .replace(/\s+/g, "-")
    .replace(/^-+|-+$/g, "")
    .toLowerCase();
}

/** Elapsed milliseconds → MM:SS. @param {number} ms */
function formatTime(ms) {
  const total = Math.floor(ms / 1000);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}
