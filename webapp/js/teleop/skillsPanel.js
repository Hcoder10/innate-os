// @ts-check
// Skills panel — a pane in the shared right dock (see rightDock.js) that mirrors
// the sim console's skill interface: the live roster from /brain/available_skills,
// a per-skill parameter form generated from each skill's input schema, and
// direct execution through the /execute_skill action (cancelable, with streamed
// feedback).
//
// The roster topic carries more than datasets/skillList.js consumes — every
// skill also ships `inputs`/`inputs_json` (schema {type, required, enum?,
// default?}), `guidelines`, and `in_training`. That schema drives the form.

import {
  AVAILABLE_SKILLS_TOPIC,
  EXECUTE_SKILL_ACTION,
  EXECUTE_SKILL_ACTION_TYPE,
  PINNED_SKILLS,
} from "../constants.js";

/**
 * @param {ReturnType<import("./rightDock.js").createRightDock>} dock The shared right dock controller.
 * @param {import("../rosClient.js").RosClient} rosClient
 * @param {ReturnType<import("./agentState.js").createAgentState>} agentState
 * @returns {{ destroy: () => void }}
 */
export function createSkillsPanel(dock, rosClient, agentState) {
  // Top pane of the dock; toggle sits a quarter down the camera's right edge.
  const { body } = dock.addPanel({ key: "skills", label: "Skills", togglePos: 0.25 });

  const head = document.createElement("div");
  head.className = "dock-head";
  const title = document.createElement("p");
  title.className = "microlabel";
  title.textContent = "skills";
  const count = document.createElement("span");
  count.className = "skills-count mono";
  const reload = document.createElement("button");
  reload.className = "skills-reload";
  reload.type = "button";
  reload.title = "Refresh roster";
  reload.textContent = "↻";
  head.append(title, count, reload);

  const listEl = document.createElement("div");
  listEl.className = "skills-list";

  body.append(head, listEl);

  // ---- state --------------------------------------------------------------

  /** @type {any[]} */
  let skills = [];
  let signature = "";
  /** @type {string | null} */
  let expandedId = null;
  /** Per-skill, per-param string values, kept across re-renders. @type {Map<string, Record<string, string>>} */
  const inputValues = new Map();
  /** Last/in-flight run. `done` marks the terminal state. @type {{ skillId: string, cancel: () => void, text: string, error: boolean, canceling: boolean, done: boolean } | null} */
  let run = null;

  // ---- skill input schema (mirrors the sim console) -----------------------

  /** @param {any} skill @returns {Record<string, any>} */
  function getSkillInputs(skill) {
    if (skill?.inputs && typeof skill.inputs === "object" && !Array.isArray(skill.inputs)) {
      return skill.inputs;
    }
    if (typeof skill?.inputs_json === "string" && skill.inputs_json) {
      try {
        const parsed = JSON.parse(skill.inputs_json);
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed;
      } catch {
        return {};
      }
    }
    return {};
  }

  /** @param {any} schema */
  function schemaType(schema) {
    const t = typeof schema === "string" ? schema : schema?.type;
    return String(t ?? "any").toLowerCase();
  }
  /** @param {any} schema */
  function isRequired(schema) {
    return typeof schema === "object" && schema?.required === true;
  }
  /** @param {any} schema @returns {any[]} */
  function enumValues(schema) {
    return typeof schema === "object" && Array.isArray(schema?.enum) ? schema.enum : [];
  }
  /** @param {string} t */
  const isNumeric = (t) => ["int", "integer", "number", "float", "double"].includes(t);
  /** @param {string} t */
  const isInt = (t) => t === "int" || t === "integer";
  /** @param {string} t */
  const isBool = (t) => t === "bool" || t === "boolean";
  /** @param {string} t */
  const isJson = (t) => ["json", "object", "dict"].includes(t);

  /**
   * Current string value for a param, falling back to the schema default.
   * @param {string} skillId @param {string} paramName @param {any} schema
   */
  function valueFor(skillId, paramName, schema) {
    const stored = inputValues.get(skillId)?.[paramName];
    if (stored !== undefined) return stored;
    if (typeof schema === "object" && schema?.default !== undefined) {
      return isJson(schemaType(schema)) ? JSON.stringify(schema.default) : String(schema.default);
    }
    return "";
  }
  /** @param {string} skillId @param {string} paramName @param {string} value */
  function setValue(skillId, paramName, value) {
    const map = inputValues.get(skillId) ?? {};
    map[paramName] = value;
    inputValues.set(skillId, map);
  }

  /**
   * Validate + coerce a skill's params into the inputs object the action wants.
   * @param {any} skill
   * @returns {{ inputs: Record<string, any> } | { error: string, param: string }}
   */
  function buildInputs(skill) {
    const schema = getSkillInputs(skill);
    /** @type {Record<string, any>} */
    const out = {};
    for (const [name, spec] of Object.entries(schema)) {
      const t = schemaType(spec);
      const raw = valueFor(skill.id, name, spec).trim();
      if (raw === "") {
        if (isRequired(spec)) return { error: "Required", param: name };
        continue; // optional + empty → let the skill default it
      }
      const options = enumValues(spec);
      if (options.length > 0) {
        const match = options.find((o) => String(o) === raw);
        if (match === undefined) return { error: "Not an allowed value", param: name };
        out[name] = match;
      } else if (isBool(t)) {
        out[name] = raw === "true";
      } else if (isInt(t)) {
        if (!/^-?\d+$/.test(raw)) return { error: "Whole number expected", param: name };
        out[name] = parseInt(raw, 10);
      } else if (isNumeric(t)) {
        const n = Number(raw);
        if (!Number.isFinite(n)) return { error: "Number expected", param: name };
        out[name] = n;
      } else if (isJson(t)) {
        try {
          out[name] = JSON.parse(raw);
        } catch {
          return { error: "Invalid JSON", param: name };
        }
      } else {
        out[name] = raw;
      }
    }
    return { inputs: out };
  }

  // ---- run lifecycle ------------------------------------------------------

  /** @param {any} skill */
  function startRun(skill) {
    const built = buildInputs(skill);
    if ("error" in built) {
      run = { skillId: skill.id, cancel: () => {}, text: `${built.param}: ${built.error}`, error: true, canceling: false, done: true };
      render();
      return;
    }
    const { promise, cancel } = rosClient.sendActionGoal(
      EXECUTE_SKILL_ACTION,
      EXECUTE_SKILL_ACTION_TYPE,
      { skill_type: skill.id, inputs: JSON.stringify(built.inputs) },
      {
        onFeedback: (values) => {
          if (!run || run.skillId !== skill.id || run.done) return;
          const fb = values?.feedback;
          if (typeof fb === "string" && fb) {
            run.text = fb;
            render();
          }
        },
      },
    );
    run = { skillId: skill.id, cancel, text: "Running…", error: false, canceling: false, done: false };
    render();

    promise.then(
      (values) => {
        if (run?.skillId !== skill.id) return;
        const ok = values?.success !== false && values?.success_type !== "failure";
        run = {
          skillId: skill.id,
          cancel: () => {},
          text: values?.message || (values?.success_type === "cancelled" ? "Cancelled" : ok ? "Done" : "Failed"),
          error: !ok,
          canceling: false,
          done: true,
        };
        render();
      },
      (err) => {
        if (run?.skillId !== skill.id) return;
        run = { skillId: skill.id, cancel: () => {}, text: err?.message || "Run failed", error: true, canceling: false, done: true };
        render();
      },
    );
  }

  function stopRun() {
    if (!run || run.canceling) return;
    run.canceling = true;
    run.text = "Stopping…";
    run.cancel();
    render();
  }

  // ---- rendering ----------------------------------------------------------

  function render() {
    count.textContent = String(skills.length);
    const frag = document.createDocumentFragment();
    for (const skill of skills) frag.appendChild(renderRow(skill));
    if (skills.length === 0) {
      const empty = document.createElement("p");
      empty.className = "skills-empty";
      empty.textContent = rosClient.state === "connected" ? "No skills available." : "Not connected.";
      frag.appendChild(empty);
    }
    listEl.replaceChildren(frag);
  }

  /** @param {any} skill */
  function renderRow(skill) {
    const isExpanded = expandedId === skill.id;
    const running = !!run && run.skillId === skill.id && !run.done;

    const row = document.createElement("div");
    row.className = "skill-card" + (isExpanded ? " expanded" : "");

    const top = document.createElement("div");
    top.className = "skill-top";
    const name = document.createElement("span");
    name.className = "skill-name";
    name.textContent = formatName(skill);
    const guidelines = skill.guidelines || skill.guidelines_when_running;
    if (guidelines) name.title = guidelines;
    const type = document.createElement("span");
    type.className = "skill-type mono";
    type.textContent = skill.type || "";

    // Active toggle — whether this skill is enabled for the current agent
    // (set_active_skills). Disabled until an agent is selected.
    const { currentDirective, activeSkills } = agentState.get();
    const activeToggle = document.createElement("button");
    activeToggle.type = "button";
    activeToggle.className = "skill-active" + (activeSkills.has(skill.id) ? " on" : "");
    activeToggle.disabled = !currentDirective;
    activeToggle.setAttribute("aria-label", "Toggle skill for agent");
    activeToggle.title = !currentDirective
      ? "Select an agent to choose its skills"
      : activeSkills.has(skill.id)
        ? "Enabled for the agent — click to disable"
        : "Disabled — click to enable for the agent";
    activeToggle.innerHTML = '<span class="skill-active-rail"><span class="skill-active-thumb"></span></span>';
    activeToggle.addEventListener("click", (e) => {
      e.stopPropagation();
      if (activeToggle.disabled) return;
      // Show "in flight" rather than optimistically flipping — the brain's
      // re-pulled active set (via agentState) settles it.
      activeToggle.classList.add("pending");
      activeToggle.disabled = true;
      agentState.toggleSkill(skill.id);
    });
    top.append(name, type, activeToggle);

    const runBtn = document.createElement("button");
    runBtn.type = "button";
    runBtn.className = "skill-run-toggle" + (isExpanded ? " active" : "");
    runBtn.textContent = isExpanded ? "Close" : "Run ▸";
    runBtn.disabled = !!skill.in_training;
    if (skill.in_training) runBtn.title = "Training in progress";
    runBtn.addEventListener("click", () => {
      expandedId = isExpanded ? null : skill.id;
      render();
    });

    row.append(top, runBtn);
    if (isExpanded) row.appendChild(renderForm(skill, running));
    return row;
  }

  /** @param {any} skill @param {boolean} running */
  function renderForm(skill, running) {
    const form = document.createElement("div");
    form.className = "skill-form";

    const schema = getSkillInputs(skill);
    const params = Object.entries(schema);
    if (params.length === 0) {
      const none = document.createElement("p");
      none.className = "skill-param-note";
      none.textContent = "No parameters.";
      form.appendChild(none);
    }
    for (const [paramName, spec] of params) {
      form.appendChild(renderParam(skill, paramName, spec));
    }

    const footer = document.createElement("div");
    footer.className = "skill-form-footer";

    const status = document.createElement("p");
    status.className = "skill-status" + (run && run.skillId === skill.id && run.error ? " error" : "");
    status.textContent = run && run.skillId === skill.id ? run.text : "Runs via /execute_skill.";

    const action = document.createElement("button");
    action.type = "button";
    if (running) {
      action.className = "skill-confirm stop";
      action.textContent = run?.canceling ? "Stopping" : "Stop";
      action.disabled = !!run?.canceling;
      action.addEventListener("click", stopRun);
    } else {
      action.className = "skill-confirm";
      action.textContent = "Confirm";
      const otherRunning = run && !run.done && run.skillId !== skill.id;
      action.disabled = !!otherRunning || rosClient.state !== "connected";
      action.addEventListener("click", () => startRun(skill));
    }

    footer.append(status, action);
    form.appendChild(footer);
    return form;
  }

  /** @param {any} skill @param {string} paramName @param {any} spec */
  function renderParam(skill, paramName, spec) {
    const t = schemaType(spec);
    const options = enumValues(spec);
    const value = valueFor(skill.id, paramName, spec);

    const rowEl = document.createElement("label");
    rowEl.className = "skill-param";

    const labelRow = document.createElement("span");
    labelRow.className = "skill-param-label";
    const pn = document.createElement("span");
    pn.textContent = paramName + (isRequired(spec) ? " *" : "");
    const pt = document.createElement("span");
    pt.className = "skill-param-type mono";
    pt.textContent = typeof spec === "string" ? spec : spec?.type ?? "any";
    labelRow.append(pn, pt);
    rowEl.appendChild(labelRow);

    if (options.length > 0) {
      const sel = document.createElement("select");
      sel.className = "skill-input mono";
      if (!isRequired(spec)) sel.appendChild(new Option("— unset —", ""));
      for (const opt of options) {
        const o = new Option(String(opt), String(opt));
        if (String(opt) === value) o.selected = true;
        sel.appendChild(o);
      }
      sel.value = value;
      sel.addEventListener("change", () => setValue(skill.id, paramName, sel.value));
      rowEl.appendChild(sel);
    } else if (isBool(t)) {
      const group = document.createElement("div");
      group.className = "skill-bool";
      for (const label of ["true", "false"]) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "skill-bool-opt" + (value === label ? " on" : "");
        b.textContent = label;
        b.addEventListener("click", () => {
          setValue(skill.id, paramName, label);
          render();
        });
        group.appendChild(b);
      }
      rowEl.appendChild(group);
    } else if (isJson(t)) {
      const ta = document.createElement("textarea");
      ta.className = "skill-input skill-textarea mono";
      ta.value = value;
      ta.placeholder = `${paramName}…`;
      ta.addEventListener("input", () => setValue(skill.id, paramName, ta.value));
      rowEl.appendChild(ta);
    } else {
      const inp = document.createElement("input");
      inp.className = "skill-input mono";
      inp.type = isNumeric(t) ? "number" : "text";
      inp.value = value;
      inp.placeholder = `${paramName}…`;
      inp.addEventListener("input", () => setValue(skill.id, paramName, inp.value));
      // Enter submits the skill (textarea is left alone so JSON can have newlines).
      inp.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !(run && !run.done)) {
          e.preventDefault();
          startRun(skill);
        }
      });
      rowEl.appendChild(inp);
    }
    return rowEl;
  }

  // ---- live data ----------------------------------------------------------

  const unsubSkills = rosClient.subscribe(AVAILABLE_SKILLS_TOPIC, (msg) => {
    /** @type {any[]} */
    const all = Array.isArray(msg?.skills) ? msg.skills : [];
    // Pinned skills float to the top in PINNED_SKILLS order; the rest keep their
    // roster order (mirrors the sim console's sortSkills).
    const next = all
      .filter((s) => s && s.id)
      .map((s, index) => ({ s, index }))
      .sort((a, b) => pinnedRank(a.s) - pinnedRank(b.s) || a.index - b.index)
      .map((entry) => entry.s);
    // The roster is latched and republishes on any change; avoid a re-render
    // (which would steal focus mid-typing) unless the set actually changed.
    const sig = next.map((s) => `${s.id}:${s.in_training ? 1 : 0}`).join("|");
    if (sig === signature) return;
    signature = sig;
    skills = next;
    if (expandedId && !skills.some((s) => s.id === expandedId)) expandedId = null;
    render();
  });

  reload.addEventListener("click", () => {
    // The roster is latched and always current on this live subscription, so a
    // reload just redraws (and clears the dedupe so the next push re-renders).
    signature = "";
    render();
  });

  const unsubState = rosClient.onStateChange(() => render());
  // Re-render the active toggles when the agent / its active-skill set changes.
  const unsubAgents = agentState.subscribe(() => render());
  render();

  return {
    destroy() {
      unsubSkills();
      unsubState();
      unsubAgents();
      if (run && !run.done) run.cancel();
    },
  };
}

/**
 * Position in PINNED_SKILLS (or the end if unpinned), matched on the last path
 * segment with "_"→" ", lowercased — the same basis the sim console uses.
 * @param {any} skill
 */
function pinnedRank(skill) {
  const segments = String(skill?.name || skill?.id || "").split("/");
  const name = (segments[segments.length - 1] || "").replace(/_/g, " ").trim().toLowerCase();
  const i = PINNED_SKILLS.findIndex((entry) => entry.trim().toLowerCase() === name);
  return i === -1 ? PINNED_SKILLS.length : i;
}

/** "pick_and_place/cup" → "Pick And Place / Cup". @param {any} skill */
function formatName(skill) {
  if (skill?.name) return skill.name;
  return String(skill?.id ?? "")
    .split("/")
    .map((part) => part.replace(/[_-]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()))
    .join(" / ");
}
