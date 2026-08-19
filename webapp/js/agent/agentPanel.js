// @ts-check
// Agent panel — the liquid-glass control column on the Agent page. One place to
// run the autonomous brain: pick a directive, Start/Stop it, watch its live
// thinking traces + active skill + chat, and message it a goal.
//
// Data sources (all real ROS, same as the teleop dock panes this replaces):
//   - directive roster / current / brain-active: agentState (get_available_directives)
//   - start/stop:   agentState.setDirective(id) / setDirective("")  (set_brain_active)
//   - thoughts/chat: /brain/chat_out (robot, robot_thoughts, robot_anticipation, system, …)
//   - user input:    /brain/chat_in
//   - active skill:  /brain/skill_status_update
//
// The thought-grouping + skill-run rendering here is the canonical chat stream
// (it originated in the old teleop chat pane, since removed).

import { copyText } from "../clipboard.js";
import {
  findMatchingOutputIndex,
  readCompactActivity,
  SKILL_OUTPUT_LINK_WINDOW_SEC,
  writeCompactActivity,
} from "./activityView.js";
import { createMicStream } from "./micStream.js";
import {
  AGENT_STATUS_TOPIC,
  CHAT_IN_TOPIC,
  CHAT_OUT_TOPIC,
  GET_AVAILABLE_DIRECTIVES_SERVICE,
  GET_CHAT_HISTORY_SERVICE,
  RESET_BRAIN_SERVICE,
  SET_BRAIN_ACTIVE_SERVICE,
  SKILL_STATUS_UPDATE_TOPIC,
} from "../constants.js";

/**
 * @param {HTMLElement} root cockpit root — the panel mounts as a right-edge overlay.
 * @param {import("../rosClient.js").RosClient} rosClient
 * @param {ReturnType<typeof import("../teleop/agentState.js").createAgentState>} agentState
 * @param {{
 *   onView: (view: "live" | "brain") => void,
 *   enableMic?: boolean,
 *   onMicState?: (state: {on: boolean, busy: boolean, level: number, waveform: number[], error: string | null}) => void
 * }} opts
 *   onView is the stage-view switch, owned by the page; setView reflects a flip
 *   the page made itself (chip, Esc). enableMic connects the browser microphone
 *   in sim, where the robot has no physical microphone (see micStream.js).
 * @returns {{
 *   destroy: () => void,
 *   setView: (view: "live" | "brain") => void,
 *   startMic: () => Promise<void>,
 *   stopMic: () => void
 * }}
 */
export function createAgentPanel(root, rosClient, agentState, opts) {
  const selfOrigin = crypto.randomUUID?.() ?? `web-${Date.now()}-${Math.random()}`;
  const mic = opts.enableMic
    ? createMicStream(rosClient, (state) => opts.onMicState?.(state))
    : null;

  const panel = document.createElement("section");
  panel.className = "overlay agent-panel";

  // ---- header -------------------------------------------------------------
  const head = document.createElement("div");
  head.className = "agent-head";
  const titleEl = document.createElement("span");
  titleEl.className = "agent-title";
  titleEl.textContent = "Agent";
  const stateDot = document.createElement("span");
  stateDot.className = "agent-state-dot";
  stateDot.title = `green while the brain is active — ${AGENT_STATUS_TOPIC}`;
  // Live/Brain switch: the panel is the agent; these pick which stage shows
  // behind it — the robot's eyes, or its loop instrumented turn by turn.
  const viewSwitch = document.createElement("div");
  viewSwitch.className = "agent-views";
  viewSwitch.setAttribute("role", "group");
  viewSwitch.setAttribute("aria-label", "Stage view");
  const liveBtn = viewButton("Live", "The robot's live camera view");
  const brainBtn = viewButton("Brain", "Brain monitor — the agent loop, turn by turn (Esc returns)");
  viewSwitch.append(liveBtn, brainBtn);
  liveBtn.addEventListener("click", () => opts.onView("live"));
  brainBtn.addEventListener("click", () => opts.onView("brain"));
  /** @param {"live" | "brain"} view */
  function setView(view) {
    liveBtn.classList.toggle("active", view === "live");
    brainBtn.classList.toggle("active", view === "brain");
    liveBtn.setAttribute("aria-pressed", String(view === "live"));
    brainBtn.setAttribute("aria-pressed", String(view === "brain"));
  }
  setView("live");
  // Phones dock the panel as a bottom sheet (CSS); this expands it upward.
  const expandBtn = document.createElement("button");
  expandBtn.type = "button";
  expandBtn.className = "agent-expand";
  expandBtn.setAttribute("aria-label", "Expand panel");
  expandBtn.textContent = "\u25b4";
  expandBtn.onclick = () => {
    const expanded = panel.classList.toggle("expanded");
    expandBtn.textContent = expanded ? "\u25be" : "\u25b4";
    expandBtn.setAttribute("aria-label", expanded ? "Collapse panel" : "Expand panel");
  };
  head.append(titleEl, stateDot, viewSwitch, expandBtn);

  // ---- directive + start/stop --------------------------------------------
  const controls = document.createElement("div");
  controls.className = "agent-controls";

  const directiveSelect = document.createElement("select");
  directiveSelect.className = "agent-directive mono";
  directiveSelect.setAttribute("aria-label", "Directive");
  directiveSelect.title = `Pick the directive to run — ${GET_AVAILABLE_DIRECTIVES_SERVICE}`;

  const toggleBtn = document.createElement("button");
  toggleBtn.type = "button";
  toggleBtn.className = "agent-toggle";
  toggleBtn.title = SET_BRAIN_ACTIVE_SERVICE;

  const resetBtn = document.createElement("button");
  resetBtn.type = "button";
  resetBtn.className = "agent-reset";
  resetBtn.title = `Reset the agent's brain / working memory — ${RESET_BRAIN_SERVICE}`;
  resetBtn.textContent = "Reset";

  controls.append(directiveSelect, toggleBtn, resetBtn);

  // ---- active skill -------------------------------------------------------
  const activeSkill = document.createElement("div");
  activeSkill.className = "agent-activeskill";
  const activeSkillLabel = document.createElement("span");
  activeSkillLabel.className = "microlabel";
  activeSkillLabel.textContent = "active skill";
  const activeSkillName = document.createElement("span");
  activeSkillName.className = "agent-activeskill-name mono";
  activeSkillName.textContent = "—";
  activeSkill.title = `the skill the brain is executing right now — ${SKILL_STATUS_UPDATE_TOPIC}`;
  activeSkill.append(activeSkillLabel, activeSkillName);

  // ---- live stream (thoughts + chat + skill runs) -------------------------
  const streamHead = document.createElement("div");
  streamHead.className = "agent-stream-head";
  const streamLabel = document.createElement("p");
  streamLabel.className = "microlabel agent-stream-label";
  streamLabel.textContent = "Activity";
  streamLabel.title = `the brain's thoughts, chat, and skill runs — ${CHAT_OUT_TOPIC}`;

  const compactToggle = document.createElement("button");
  compactToggle.type = "button";
  compactToggle.className = "agent-compact-toggle mono";
  compactToggle.setAttribute("role", "switch");
  compactToggle.setAttribute("aria-label", "Use compact activity view");
  const compactLabel = document.createElement("span");
  compactLabel.textContent = "Compact";
  const compactTrack = document.createElement("span");
  compactTrack.className = "agent-compact-track";
  compactTrack.setAttribute("aria-hidden", "true");
  compactTrack.appendChild(document.createElement("span"));
  compactToggle.append(compactLabel, compactTrack);
  streamHead.append(streamLabel, compactToggle);

  const stream = document.createElement("div");
  stream.className = "agent-stream";

  // ---- composer -----------------------------------------------------------
  const form = document.createElement("form");
  form.className = "agent-compose";
  const input = document.createElement("textarea");
  input.className = "agent-compose-input";
  input.rows = 1;
  input.placeholder = "Message the agent…";
  const send = document.createElement("button");
  send.type = "submit";
  send.className = "agent-compose-send";
  send.textContent = "Send";
  send.title = CHAT_IN_TOPIC;
  form.append(input, send);

  panel.append(head, controls, activeSkill, streamHead, stream, form);
  root.append(panel);

  let compactActivity = true;
  try {
    compactActivity = readCompactActivity(localStorage);
  } catch {
    // Accessing localStorage itself can throw on a locked-down origin.
  }

  /** @param {boolean} compact @param {boolean} [persist] */
  function setCompactActivity(compact, persist = false) {
    const wasAtBottom = atBottom();
    compactActivity = compact;
    panel.classList.toggle("compact-activity", compact);
    compactToggle.classList.toggle("on", compact);
    compactToggle.setAttribute("aria-checked", String(compact));
    compactToggle.title = compact
      ? "Compact view is on — show completed reasoning and legacy outputs"
      : "Hide completed reasoning and legacy outputs";
    if (compact) {
      for (const block of stream.querySelectorAll(".chat-thoughts.open")) {
        block.classList.remove("open");
        const toggle = block.querySelector(".chat-thoughts-toggle");
        const arrow = block.querySelector(".chat-thoughts-arrow");
        toggle?.setAttribute("aria-expanded", "false");
        if (arrow) arrow.textContent = "▾";
      }
      for (const block of stream.querySelectorAll(".chat-skill.open:not(.failed)")) {
        block.classList.remove("open");
        const head = block.querySelector(".chat-skill-head");
        const arrow = block.querySelector(".chat-skill-chevron");
        const detail = block.querySelector(".chat-skill-detail");
        head?.setAttribute("aria-expanded", "false");
        detail?.setAttribute("aria-hidden", "true");
        if (detail instanceof HTMLElement) detail.hidden = true;
        if (arrow) arrow.textContent = "▾";
      }
    }
    if (persist) {
      try {
        writeCompactActivity(localStorage, compact);
      } catch {
        // Keep the toggle functional when the storage getter is blocked.
      }
    }
    snapIfAtBottom(wasAtBottom);
  }

  compactToggle.addEventListener("click", () => setCompactActivity(!compactActivity, true));
  setCompactActivity(compactActivity);

  // ---- directive roster + start/stop --------------------------------------
  // The dropdown ARMS a directive; Start activates it. While active, switching
  // the dropdown switches the running directive live. brain-active drives the
  // toggle label (Start <-> Stop). We remember the last non-empty directive so
  // Stop -> Start resumes the same one even though the brain reports "" when idle.
  let lastDirective = "";
  let applying = false;
  /** @type {ReturnType<typeof setTimeout> | null} */
  let flashTimer = null;

  function renderRoster() {
    if (flashTimer) return; // keep the copy-feedback row until it expires
    const { agents, broken, currentDirective, brainActive } = agentState.get();
    if (currentDirective) lastDirective = currentDirective;

    directiveSelect.replaceChildren();
    if (agents.length === 0) {
      directiveSelect.appendChild(new Option("No agents available", ""));
    } else {
      for (const a of agents) directiveSelect.appendChild(new Option(a.name, a.id));
    }
    // Agents that failed to load stay visible instead of silently vanishing:
    // a picker row with the start of the load error, the full error in the
    // tooltip, and selecting the row copies it to the clipboard.
    for (const b of broken ?? []) {
      const preview = b.error.length > 60 ? b.error.slice(0, 59) + "…" : b.error;
      const opt = new Option(`⚠ ${b.name} — ${preview}`, `__broken__:${b.id}`);
      opt.title = `${b.error}\n\nSelect to copy the full error.`;
      opt.dataset.error = b.error;
      directiveSelect.appendChild(opt);
    }
    // Show the running directive when active, else the armed/last one. With no
    // prior choice, default to "Demo Agent" if the roster has it.
    const demo = agents.find((a) => /demo\s*agent/i.test(a.name) || /demo/i.test(a.id));
    const armed = currentDirective || lastDirective || demo?.id || (agents[0]?.id ?? "");
    if (armed) directiveSelect.value = armed;

    panel.classList.toggle("active", brainActive);
    stateDot.classList.toggle("on", brainActive);
    // A running loop makes the Brain segment glow — an invitation to look inside.
    brainBtn.classList.toggle("pulse", brainActive);

    toggleBtn.textContent = applying ? "…" : brainActive ? "Stop" : "Start";
    toggleBtn.classList.toggle("stop", brainActive);
    toggleBtn.disabled = applying || (!brainActive && agents.length === 0);
    // Keep the picker openable when only broken agents exist, so their rows
    // stay reachable; the change handler ignores non-agent values.
    directiveSelect.disabled = applying || (agents.length === 0 && (broken ?? []).length === 0);
    resetBtn.disabled = applying;
    input.placeholder = brainActive ? "Message the agent…" : "Message the agent… (sending starts it)";
  }

  /** @param {() => Promise<any>} fn */
  async function withApplying(fn) {
    // A real action supersedes the copy-feedback flash: without this, the
    // flash guard in renderRoster would hide the applying state for up to 1.2s.
    if (flashTimer) {
      clearTimeout(flashTimer);
      flashTimer = null;
    }
    applying = true;
    renderRoster();
    try {
      await fn();
    } finally {
      applying = false;
      renderRoster();
    }
  }

  directiveSelect.addEventListener("change", () => {
    const id = directiveSelect.value;
    if (id.startsWith("__broken__:")) {
      // Copy the full load error, flash the result in the closed box, then
      // re-render (which re-arms the previous choice).
      const opt = directiveSelect.selectedOptions[0];
      void copyText(opt?.dataset.error ?? "").then(
        () => {
          if (opt) opt.text = "✓ load error copied";
        },
        () => {
          if (opt) opt.text = "✗ copy failed — see tooltip";
        },
      );
      flashTimer = setTimeout(() => {
        flashTimer = null;
        renderRoster();
      }, 1200);
      return;
    }
    if (!id) return;
    lastDirective = id;
    // Switch live only when already running; otherwise just arm for Start.
    if (agentState.get().brainActive) void withApplying(() => agentState.setDirective(id));
  });

  // No local "started./stopped." echo: the brain announces both on
  // /brain/chat_out (and into history), so every client — not just the one
  // whose button was pressed — renders the same message via the normal
  // chat_out path.
  toggleBtn.addEventListener("click", () => {
    const { brainActive } = agentState.get();
    if (brainActive) {
      void withApplying(() => agentState.setDirective(""));
    } else {
      const selected = directiveSelect.value.startsWith("__broken__:") ? "" : directiveSelect.value;
      const id = selected || lastDirective;
      if (id) void withApplying(() => agentState.setDirective(id));
    }
  });

  resetBtn.addEventListener("click", () => {
    if (!window.confirm("Reset the agent's brain? This clears its working memory.")) return;
    agentState.resetBrain().catch(() => {});
  });

  const unsubAgents = agentState.subscribe(renderRoster);

  // ---- stream helpers -----------------------------------------------------

  // Sticky-bottom scrolling. Capture whether we're pinned to the bottom BEFORE
  // mutating the stream, then snap down afterwards only if we were. Measuring
  // *after* appending is the bug that left tall robot replies off-screen: the
  // freshly added height makes the distance-from-bottom exceed any threshold, so
  // it wrongly concludes the user had scrolled up.
  function atBottom() {
    return stream.scrollHeight - stream.scrollTop - stream.clientHeight < 80;
  }
  /** @param {boolean} wasAtBottom */
  function snapIfAtBottom(wasAtBottom) {
    if (wasAtBottom) stream.scrollTop = stream.scrollHeight;
  }

  /** @type {{ wrap: HTMLElement, status: HTMLElement, list: HTMLElement, lastByKind: Record<string, string>, startTs: number, latestTs: number } | null} */
  let thoughts = null;
  let lastTs = 0;

  /** @typedef {{ wrap: HTMLElement, head: HTMLElement, args: HTMLElement, status: HTMLElement, detail: HTMLElement | null, chevron: HTMLElement | null }} SkillRunView */
  /** @typedef {{ text: string, ts: number }} LegacyOutputEcho */
  /** @typedef {{ run: SkillRunView, text: string, ts: number }} OrphanOutput */
  /** Authoritative status outputs awaiting their identical legacy chat copy. @type {LegacyOutputEcho[]} */
  let pendingLegacyOutputs = [];
  /** Legacy chat copies that arrived before their authoritative status event. @type {OrphanOutput[]} */
  let orphanOutputs = [];

  /** Drop bookkeeping only after an entry is too old to match by definition. @param {number} referenceTs */
  function pruneOutputQueues(referenceTs) {
    pendingLegacyOutputs = pendingLegacyOutputs.filter(
      (candidate) => referenceTs - candidate.ts <= SKILL_OUTPUT_LINK_WINDOW_SEC,
    );
    // A legacy output may precede its authoritative status by at most the
    // one-second cross-topic reorder grace in skillEventsAreAdjacent().
    orphanOutputs = orphanOutputs.filter((candidate) => referenceTs - candidate.ts <= 1);
  }

  /** @param {boolean} active */
  function setThoughtsStatus(active) {
    if (!thoughts) return;
    const d = Math.max(0, Math.ceil(thoughts.latestTs - (lastTs || thoughts.startTs)));
    const word = active ? "Processing" : "Processed";
    thoughts.status.textContent = d > 0 ? `${word} for ${d} second${d === 1 ? "" : "s"}` : `${word}…`;
    thoughts.wrap.classList.toggle("active", active);
  }

  function finalizeThoughts() {
    if (thoughts) setThoughtsStatus(false);
    thoughts = null;
  }

  /** @param {string} kind @param {string} text @param {number} ts */
  function addThought(kind, text, ts) {
    const wasAtBottom = atBottom();
    if (!thoughts) {
      const wrap = document.createElement("div");
      // Collapsed by default — only the latest thought (preview) shows until expanded.
      wrap.className = "chat-thoughts";
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "chat-thoughts-toggle";
      toggle.title = "The model's internal reasoning for this turn — click to expand";
      const label = document.createElement("span");
      label.className = "chat-thoughts-label";
      label.textContent = "Thoughts";
      const arrow = document.createElement("span");
      arrow.className = "chat-thoughts-arrow";
      arrow.textContent = "▾";
      const status = document.createElement("span");
      status.className = "chat-thoughts-status";
      toggle.append(label, status, arrow);
      const list = document.createElement("div");
      list.className = "chat-thoughts-list";
      list.id = `thoughts-${crypto.randomUUID?.() ?? `${Date.now()}-${Math.random()}`}`;
      toggle.setAttribute("aria-controls", list.id);
      toggle.setAttribute("aria-expanded", "false");
      toggle.addEventListener("click", () => {
        const wasAtBottom = atBottom();
        const open = wrap.classList.toggle("open");
        toggle.setAttribute("aria-expanded", String(open));
        arrow.textContent = open ? "▴" : "▾";
        snapIfAtBottom(wasAtBottom);
      });
      wrap.append(toggle, list);
      stream.appendChild(wrap);
      thoughts = { wrap, status, list, lastByKind: {}, startTs: ts, latestTs: ts };
    }
    thoughts.latestTs = ts;
    if (thoughts.lastByKind[kind] !== text) {
      thoughts.lastByKind[kind] = text;
      const item = document.createElement("div");
      item.className = "chat-thought-item";
      item.textContent = roundNums(text);
      thoughts.list.appendChild(item);
    }
    setThoughtsStatus(true);
    snapIfAtBottom(wasAtBottom);
  }

  /** @param {string} kind @param {string} text @param {number} ts @param {string} [label] */
  function addMessage(kind, text, ts, label) {
    const wasAtBottom = atBottom();
    finalizeThoughts();
    const el = document.createElement("div");
    el.className = `chat-msg ${kind}`;
    if (kind === "system") {
      const tag = document.createElement("span");
      tag.className = "chat-sender mono";
      tag.textContent = (label || "system").replace(/_/g, " ");
      el.appendChild(tag);
    }
    const bubble = document.createElement("div");
    bubble.className = "chat-bubble";
    bubble.textContent = kind === "user" ? text : roundNums(text);
    el.appendChild(bubble);
    stream.appendChild(el);
    lastTs = ts;
    snapIfAtBottom(wasAtBottom);
  }

  /** @type {Map<string, SkillRunView>} */
  const skillRuns = new Map();

  /** @param {string} name @param {string} [tagText] @returns {SkillRunView} */
  function createSkillRow(name, tagText = "skill") {
    const wrap = document.createElement("div");
    wrap.className = "chat-skill";
    const head = document.createElement("div");
    head.className = "chat-skill-head";
    const tag = document.createElement("span");
    tag.className = "chat-skill-tag mono";
    tag.textContent = tagText;
    const nameEl = document.createElement("span");
    nameEl.className = "chat-skill-name";
    nameEl.textContent = name.replace(/_/g, " ");
    const argsEl = document.createElement("span");
    argsEl.className = "chat-skill-args mono";
    const statusEl = document.createElement("span");
    statusEl.className = "chat-skill-status mono";
    statusEl.title = SKILL_STATUS_UPDATE_TOPIC;
    head.append(tag, nameEl, argsEl, statusEl);
    wrap.append(head);
    /** @type {SkillRunView} */
    const run = { wrap, head, args: argsEl, status: statusEl, detail: null, chevron: null };
    const toggleDetail = () => {
      if (!run.detail) return;
      const wasAtBottom = atBottom();
      const open = run.wrap.classList.toggle("open");
      run.head.setAttribute("aria-expanded", String(open));
      run.detail.setAttribute("aria-hidden", String(!open));
      run.detail.hidden = !open;
      if (run.chevron) run.chevron.textContent = open ? "▴" : "▾";
      snapIfAtBottom(wasAtBottom);
    };
    head.addEventListener("click", toggleDetail);
    head.addEventListener("keydown", (event) => {
      if (!run.detail || (event.key !== "Enter" && event.key !== " ")) return;
      event.preventDefault();
      toggleDetail();
    });
    return run;
  }

  /** @param {SkillRunView} run @param {string} text @param {{ open?: boolean, title?: string, round?: boolean }} [opts] */
  function attachSkillDetail(run, text, opts = {}) {
    if (!run.detail) {
      run.detail = document.createElement("div");
      run.detail.className = "chat-skill-detail";
      run.detail.id = `skill-detail-${crypto.randomUUID?.() ?? `${Date.now()}-${Math.random()}`}`;
      run.chevron = document.createElement("span");
      run.chevron.className = "chat-skill-chevron mono";
      run.head.appendChild(run.chevron);
      run.wrap.appendChild(run.detail);
      run.wrap.classList.add("has-detail");
      run.head.tabIndex = 0;
      run.head.setAttribute("role", "button");
      run.head.setAttribute("aria-controls", run.detail.id);
    }
    run.detail.textContent = opts.round === false ? text : roundNums(text);
    run.head.title = opts.title || "Show or hide skill details";
    const open = !!opts.open;
    run.wrap.classList.toggle("open", open);
    run.head.setAttribute("aria-expanded", String(open));
    run.detail.setAttribute("aria-hidden", String(!open));
    run.detail.hidden = !open;
    if (run.chevron) run.chevron.textContent = open ? "▴" : "▾";
  }

  /** @param {string} text @param {number} ts */
  function addSkillOutput(text, ts) {
    const wasAtBottom = atBottom();
    finalizeThoughts();
    pruneOutputQueues(ts);

    const echoIndex = findMatchingOutputIndex(pendingLegacyOutputs, text, ts);
    if (echoIndex >= 0) {
      pendingLegacyOutputs.splice(echoIndex, 1);
      lastTs = ts;
      return;
    }

    // Old history may contain output without its status row. Keep it reachable
    // in Detailed mode, but never guess which output-less call it belongs to.
    const run = createSkillRow("output");
    run.wrap.classList.add("completed", "orphan-output");
    attachSkillDetail(run, text, { title: "Show or hide skill output" });
    stream.appendChild(run.wrap);
    orphanOutputs.push({ run, text, ts });
    lastTs = ts;
    snapIfAtBottom(wasAtBottom);
  }

  /** @param {string} key @param {string} name @param {string} status @param {number} ts @param {string} [reason]
   *  @param {any} [args] @param {string} [output] */
  function addSkillRun(key, name, status, ts, reason, args, output) {
    const wasAtBottom = atBottom();
    pruneOutputQueues(ts);
    const cls = ["running", "completed", "failed", "interrupted"].includes(status) ? status : "running";
    // Reflect the running primitive in the Active Skill readout.
    if (cls === "running") {
      activeSkillName.textContent = name.replace(/_/g, " ");
      activeSkill.classList.add("on");
    } else if (activeSkillName.textContent === name.replace(/_/g, " ")) {
      activeSkillName.textContent = "—";
      activeSkill.classList.remove("on");
    }

    let run = skillRuns.get(key);
    if (!run) {
      finalizeThoughts();
      run = createSkillRow(name);
      stream.appendChild(run.wrap);
      skillRuns.set(key, run);
    }
    const wasOpen = run.wrap.classList.contains("open");
    run.wrap.className = `chat-skill ${cls}`;
    if (run.detail) run.wrap.classList.add("has-detail");
    if (wasOpen) run.wrap.classList.add("open");
    // Terminal updates repeat the inputs; keep the last non-empty rendering so
    // a publisher that omits them can't blank args the run already showed.
    const inputs = formatSkillArgs(args);
    if (inputs.text) {
      run.args.textContent = inputs.text;
      run.args.title = inputs.full;
    }
    run.status.textContent = cls;

    // A failed run carries the failure reason / error — show it expanded
    // in-place (collapsible so a long trace can be tucked away).
    if (cls === "failed" && reason && !run.detail) {
      attachSkillDetail(run, reason, { open: true, title: "Show or hide the failure reason", round: false });
    }

    const outputText = typeof output === "string" && output.trim() ? output : "";
    if (cls === "completed" && outputText) {
      const orphanIndex = findMatchingOutputIndex(orphanOutputs, outputText, ts, false);
      if (orphanIndex >= 0) {
        orphanOutputs[orphanIndex].run.wrap.remove();
        orphanOutputs.splice(orphanIndex, 1);
      }
      attachSkillDetail(run, outputText, { title: "Show or hide skill output" });
      if (orphanIndex < 0) {
        pendingLegacyOutputs.push({ text: outputText, ts });
      }
    }

    lastTs = ts;
    if (cls !== "running") skillRuns.delete(key);
    snapIfAtBottom(wasAtBottom);
  }

  // ---- composer -----------------------------------------------------------
  // Talking to an idle agent means "start it" — an inactive brain drops
  // chat_in, and the mic device is only open while a directive runs.
  async function startIfIdle() {
    if (agentState.get().brainActive) return;
    const id = directiveSelect.value || lastDirective;
    if (id) await withApplying(() => agentState.setDirective(id));
  }

  async function startMic() {
    await startIfIdle();
    await mic?.start();
  }

  function stopMic() {
    mic?.stop();
  }

  async function submit() {
    const text = input.value.trim();
    if (!text) return;
    addMessage("user", text, Date.now() / 1000);
    input.value = "";
    input.style.height = "auto";
    // Always jump to our own message, even if we'd scrolled up reading earlier.
    stream.scrollTop = stream.scrollHeight;
    await startIfIdle();
    rosClient.publish(CHAT_IN_TOPIC, {
      data: JSON.stringify({ text, sender: "user", timestamp: Date.now() / 1000, origin: selfOrigin }),
    });
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    void submit();
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void submit();
    }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 120)}px`;
  });

  // ---- history backfill ---------------------------------------------------
  // Live topics only carry messages from after we subscribe, so a fresh page
  // load starts blank. The brain keeps the full conversation; pull it once on
  // connect and replay it through the same renderers, then keep appending live.
  let historyLoaded = false;

  async function loadHistory() {
    if (historyLoaded) return;
    let res;
    try {
      res = await rosClient.callService(GET_CHAT_HISTORY_SERVICE, {});
    } catch {
      return; // best-effort — the live stream still works without it
    }
    /** @type {any[]} */
    let entries;
    try {
      entries = JSON.parse(res?.history ?? "[]");
    } catch {
      return;
    }
    if (!Array.isArray(entries) || entries.length === 0) {
      historyLoaded = true;
      return;
    }
    // The snapshot already includes anything the live stream just showed, so
    // reset and replay it wholesale rather than trying to merge.
    stream.replaceChildren();
    thoughts = null;
    skillRuns.clear();
    pendingLegacyOutputs = [];
    orphanOutputs = [];
    lastTs = 0;
    for (const e of entries) replayEntry(e);
    historyLoaded = true;
    stream.scrollTop = stream.scrollHeight;
  }

  /** Render one stored history entry, mirroring the live chat_out routing.
   *  @param {any} e */
  function replayEntry(e) {
    const ts = Number(e?.timestamp) || Date.now() / 1000;
    const sender = String(e?.sender ?? "");
    if (sender === "task_activated") {
      const name = String(e?.text ?? e?.skill_name ?? e?.skillId ?? "");
      const status = String(e?.taskStatus ?? "");
      if (!name || !status) return;
      const key = String(e?.primitiveId ?? e?.skillId ?? name);
      addSkillRun(
        key,
        name,
        status,
        ts,
        typeof e?.failureReason === "string" ? e.failureReason : "",
        e?.args,
        typeof e?.output === "string" ? e.output : "",
      );
      return;
    }
    const text = String(e?.text ?? "");
    if (!text) return;
    if (sender === "robot_thoughts" || sender === "robot_anticipation") {
      addThought(sender, text, ts);
    } else if (sender === "vision_agent_output") {
      return; // raw vision dumps — noisy, drop (matches live)
    } else if (sender === "skill_output") {
      addSkillOutput(text, ts);
    } else if (sender === "user" || sender === "robot") {
      addMessage(sender, text, ts);
    } else {
      addMessage("system", text, ts, sender || undefined);
    }
  }

  const unsubConn = rosClient.onStateChange((s) => {
    if (s === "connected") void loadHistory();
  });

  // ---- live subscriptions -------------------------------------------------
  const unsubIn = rosClient.subscribe(CHAT_IN_TOPIC, (m) => {
    if (typeof m?.data !== "string") return;
    let payload;
    try {
      payload = JSON.parse(m.data);
    } catch {
      return;
    }
    if (payload?.origin === selfOrigin) return;
    if (String(payload?.sender ?? "") !== "user") return;
    const text = String(payload?.text ?? "");
    if (!text) return;
    addMessage("user", text, Number(payload?.timestamp) || Date.now() / 1000);
  }, undefined, "std_msgs/msg/String");

  const unsubOut = rosClient.subscribe(CHAT_OUT_TOPIC, (m) => {
    if (typeof m?.data !== "string") return;
    let payload;
    try {
      payload = JSON.parse(m.data);
    } catch {
      return;
    }
    const sender = String(payload?.sender ?? "");
    const text = String(payload?.text ?? "");
    if (!sender || !text) return;
    const ts = Number(payload?.timestamp) || Date.now() / 1000;
    if (sender === "robot_thoughts" || sender === "robot_anticipation") {
      addThought(sender, text, ts);
    } else if (sender === "vision_agent_output") {
      return; // raw vision dumps — noisy, drop
    } else if (sender === "skill_output") {
      addSkillOutput(text, ts);
    } else if (sender === "user" || sender === "robot") {
      addMessage(sender, text, ts);
    } else {
      addMessage("system", text, ts, sender);
    }
  }, undefined, "std_msgs/msg/String");

  const unsubSkill = rosClient.subscribe(SKILL_STATUS_UPDATE_TOPIC, (m) => {
    if (typeof m?.data !== "string") return;
    let payload;
    try {
      payload = JSON.parse(m.data);
    } catch {
      return;
    }
    const name = String(payload?.primitive_name ?? payload?.skill_name ?? payload?.skill_id ?? "");
    const status = String(payload?.status ?? "");
    if (!name || !status) return;
    const key = String(payload?.primitive_id ?? payload?.skill_id ?? name);
    const reason = typeof payload?.reason === "string" ? payload.reason : "";
    addSkillRun(
      key,
      name,
      status,
      Number(payload?.timestamp) || Date.now() / 1000,
      reason,
      payload?.args,
      typeof payload?.output === "string" ? payload.output : "",
    );
  }, undefined, "std_msgs/msg/String");

  return {
    setView,
    startMic,
    stopMic,
    destroy() {
      mic?.destroy();
      if (flashTimer) clearTimeout(flashTimer);
      unsubAgents();
      unsubConn();
      unsubIn();
      unsubOut();
      unsubSkill();
      panel.remove();
    },
  };
}

/**
 * A segment of the panel's Live/Brain stage switch.
 * @param {string} label
 * @param {string} title
 */
function viewButton(label, title) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "agent-view-btn";
  btn.textContent = label;
  btn.title = title;
  return btn;
}

/**
 * A skill's inputs on one line: `text` clipped for the run's header row, `full`
 * intact for its hover title. A lone input reads better as its bare value
 * ("head emotion  happy") — the skill name already says what it is; several
 * need their keys to stay legible.
 * @param {any} args
 * @returns {{ text: string, full: string }}
 */
function formatSkillArgs(args) {
  if (!args || typeof args !== "object" || Array.isArray(args)) return { text: "", full: "" };
  const entries = Object.entries(args).filter(([, v]) => v !== null && v !== undefined && v !== "");
  if (entries.length === 0) return { text: "", full: "" };
  const show = (/** @type {any} */ v) => roundNums(typeof v === "object" ? JSON.stringify(v) : String(v));
  const line = (/** @type {(v: any) => string} */ value) =>
    entries.length === 1
      ? value(entries[0][1])
      : entries.map(([k, v]) => `${k.replace(/_/g, " ")}: ${value(v)}`).join(" · ");
  return { text: line((v) => clip(show(v), 40)), full: line(show) };
}

/** @param {string} text @param {number} max */
function clip(text, max) {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

/**
 * Shorten long decimals in a display string to 2 places. Cosmetic only.
 * @param {string} text
 */
function roundNums(text) {
  return text.replace(/-?\d+\.\d+(?:[eE][-+]?\d+)?/g, (n) => {
    const v = Number(n);
    return Number.isFinite(v) ? String(Math.round(v * 100) / 100) : n;
  });
}
