// @ts-check
// Configure modal — pick a skill, set hyperparameters, and start a run. Start
// just validates and hands the request to the page (opts.onStart) which owns
// the sync→create_run orchestration, then closes. So the run still gets created
// even if this modal is dismissed mid-upload.

import { GET_TASK_METADATA_SERVICE } from "../constants.js";

const MIN_EPISODES = 5;

/** field key → {env var, label, default, hint} */
const FIELDS = [
  { key: "max_steps", env: "MAX_STEPS", label: "Max steps", def: "120000", hint: "10,000–500,000" },
  { key: "batch_size", env: "BATCH_SIZE", label: "Batch size", def: "96", hint: "8–256 (GPU memory)" },
  { key: "learning_rate", env: "LEARNING_RATE", label: "Learning rate", def: "5e-5", hint: "1e-6–1e-4" },
  { key: "learning_rate_backbone", env: "LEARNING_RATE_BACKBONE", label: "LR backbone", def: "5e-5", hint: "1e-6–1e-4" },
  { key: "chunk_size", env: "CHUNK_SIZE", label: "Chunk size", def: "30", hint: "10–100" },
];

/**
 * @param {HTMLElement} parent
 * @param {import("../rosClient.js").RosClient} ros
 * @param {{ skills: Skill[] }} store
 * @param {{ skill?: Skill, onClose: () => void, onStart: (dir: string, runParams: TrainingParams, episodeCount: number) => void }} opts
 * @returns {{ destroy: () => void }}
 */
export function createConfigurePanel(parent, ros, store, opts) {
  let episodeCount = -1; // -1 = unknown/loading
  let successfulCount = 0; // episodes not marked "failure"
  let metaSeq = 0;

  const backdrop = document.createElement("div");
  backdrop.className = "modal-backdrop";
  backdrop.addEventListener("click", () => opts.onClose());

  const panel = document.createElement("div");
  panel.className = "modal";
  panel.addEventListener("click", (e) => e.stopPropagation());

  const head = document.createElement("div");
  head.className = "modal-head";
  const title = document.createElement("h2");
  title.className = "modal-title";
  title.textContent = "Train skill";
  const close = document.createElement("button");
  close.type = "button";
  close.className = "modal-close";
  close.textContent = "✕";
  close.addEventListener("click", () => opts.onClose());
  head.append(title, close);

  const formEl = document.createElement("div");
  formEl.className = "modal-body";

  // skill selector — only learned skills are trainable (not replay/code/poses)
  const skillRow = field("Skill");
  const skillSel = document.createElement("select");
  skillSel.className = "modal-select";
  for (const s of store.skills) {
    if (s.type !== "learned") continue;
    const o = document.createElement("option");
    o.value = s.directory || "";
    o.textContent = s.name;
    skillSel.appendChild(o);
  }
  if (opts.skill && opts.skill.directory) skillSel.value = opts.skill.directory;
  skillRow.appendChild(skillSel);

  const epLine = document.createElement("p");
  epLine.className = "modal-epline";

  // hyperparameters
  /** @type {Record<string, HTMLInputElement>} */
  const inputs = {};
  const hyperWrap = document.createElement("div");
  hyperWrap.className = "modal-hyper";
  const hyperLabel = document.createElement("p");
  hyperLabel.className = "microlabel";
  hyperLabel.textContent = "hyperparameters";
  hyperWrap.appendChild(hyperLabel);
  for (const f of FIELDS) {
    const row = field(f.label, f.hint);
    const inp = document.createElement("input");
    inp.type = "text";
    inp.className = "modal-input";
    inp.value = f.def;
    inp.addEventListener("input", validate);
    inputs[f.key] = inp;
    row.insertBefore(inp, row.querySelector(".modal-hint"));
    hyperWrap.appendChild(row);
  }

  // curation: train on successful episodes only
  const successRow = document.createElement("label");
  successRow.className = "modal-check";
  const successChk = document.createElement("input");
  successChk.type = "checkbox";
  const successTxt = document.createElement("span");
  successTxt.textContent = "Train on successful episodes only";
  successRow.append(successChk, successTxt);
  successChk.addEventListener("change", () => {
    updateEpLine();
    validate();
  });

  const warn = document.createElement("p");
  warn.className = "modal-warn";

  const note = document.createElement("p");
  note.className = "modal-status";
  note.textContent = "Starts a cloud run. The dataset uploads first (you can close this).";

  const startBtn = document.createElement("button");
  startBtn.type = "button";
  startBtn.className = "modal-start";
  startBtn.textContent = "Start training run";
  startBtn.addEventListener("click", start);

  formEl.append(skillRow, epLine, hyperWrap, successRow, warn, note, startBtn);
  panel.append(head, formEl);
  backdrop.appendChild(panel);
  parent.appendChild(backdrop);

  skillSel.addEventListener("change", loadEpisodeCount);
  loadEpisodeCount();

  function currentDir() {
    return skillSel.value;
  }

  function loadEpisodeCount() {
    const dir = currentDir();
    episodeCount = -1;
    epLine.textContent = "Counting episodes…";
    validate();
    if (!dir) return;
    const seq = ++metaSeq;
    ros.callService(GET_TASK_METADATA_SERVICE, { task_directory: dir })
      .then((res) => {
        if (seq !== metaSeq) return;
        const meta = res?.json_metadata ? JSON.parse(res.json_metadata) : {};
        const eps = Array.isArray(meta.episodes) ? meta.episodes : [];
        episodeCount = eps.length;
        successfulCount = eps.filter((/** @type {any} */ e) => e && e.outcome !== "failure").length;
        updateEpLine();
        validate();
      })
      .catch(() => {
        if (seq !== metaSeq) return;
        episodeCount = 0;
        successfulCount = 0;
        epLine.textContent = "Couldn't read episode count";
        validate();
      });
  }

  /** Episodes that will actually be trained on (honors the success-only filter). */
  function effectiveCount() {
    return successChk.checked ? successfulCount : episodeCount;
  }

  function updateEpLine() {
    if (episodeCount < 0) return;
    const failed = episodeCount - successfulCount;
    const suffix = successChk.checked
      ? ` · training on ${successfulCount} successful`
      : failed > 0
        ? ` · ${failed} marked failed`
        : "";
    epLine.textContent = `${episodeCount} episode${episodeCount === 1 ? "" : "s"} recorded${suffix}`;
  }

  function minSteps() {
    const batch = parseInt(inputs.batch_size.value, 10) || 1;
    return effectiveCount() > 0 ? Math.ceil(effectiveCount() / batch) : 1;
  }

  /** @returns {string|null} validation error, or null if ok */
  function validationError() {
    if (!currentDir()) return "Select a skill.";
    const count = effectiveCount();
    if (episodeCount >= 0 && count < MIN_EPISODES) {
      const what = successChk.checked ? "successful episodes" : "episodes";
      return `At least ${MIN_EPISODES} ${what} required to train (have ${count}).`;
    }
    const steps = parseInt(inputs.max_steps.value, 10);
    if (!steps || steps < minSteps()) {
      return `Max steps must be ≥ ${minSteps()} (one epoch at batch size ${parseInt(inputs.batch_size.value, 10) || "?"}).`;
    }
    return null;
  }

  function validate() {
    const err = episodeCount < 0 ? null : validationError();
    warn.textContent = err || "";
    startBtn.disabled = episodeCount < 0 || err !== null;
  }

  function buildEnv() {
    /** @type {string[]} */
    const env = [];
    for (const f of FIELDS) {
      const v = inputs[f.key].value.trim();
      if (v) env.push(`${f.env}=${v}`);
    }
    if (successChk.checked) env.push("TRAIN_ON_SUCCESS_ONLY=1");
    return env;
  }

  function start() {
    if (validationError()) return;
    opts.onStart(currentDir(), { preset: "act-default", env: buildEnv(), extra_json: "" }, episodeCount);
    opts.onClose();
  }

  return {
    destroy() {
      metaSeq++;
      backdrop.remove();
    },
  };
}

/** @param {string} labelText @param {string} [hint] @returns {HTMLElement} */
function field(labelText, hint) {
  const row = document.createElement("label");
  row.className = "modal-field";
  const l = document.createElement("span");
  l.className = "modal-field-label";
  l.textContent = labelText;
  row.appendChild(l);
  if (hint) {
    const h = document.createElement("span");
    h.className = "modal-hint";
    h.textContent = hint;
    row.appendChild(h);
  }
  return row;
}
