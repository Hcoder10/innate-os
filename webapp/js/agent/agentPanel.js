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
import { createMicStream } from "./micStream.js";
import {
  CHAT_IN_TOPIC,
  CHAT_OUT_TOPIC,
  GET_AVAILABLE_DIRECTIVES_SERVICE,
  GET_CHAT_HISTORY_SERVICE,
  RESET_BRAIN_SERVICE,
  SET_BRAIN_ACTIVE_SERVICE,
  SKILL_STATUS_UPDATE_TOPIC,
} from "../constants.js";

const CHAT_EXAMPLES = [
  "What can you see right now?",
  "What do you remember about this room?",
  "What can you help me with?",
  "Wave hello",
  "Move to the other side of the room",
  "Pick up the object in front of you",
];

/**
 * @param {HTMLElement} root cockpit root — the panel mounts as a right-edge overlay.
 * @param {import("../rosClient.js").RosClient} rosClient
 * @param {ReturnType<typeof import("../teleop/agentState.js").sharedAgentState>} agentState
 * @param {{
 *   enableMic?: boolean,
 *   onMicState?: (state: {on: boolean, busy: boolean, level: number, waveform: number[], error: string | null}) => void
 * }} opts
 *   enableMic connects the browser microphone in sim, where the robot has no
 *   physical microphone (see micStream.js).
 * @returns {{
 *   destroy: () => void,
 *   dismissOverlays: () => void,
 *   startMic: () => Promise<void>,
 *   stopMic: () => void,
 *   previewSkill: (sample: {
 *     name: string,
 *     args: Record<string, unknown>,
 *     status: "running" | "completed" | "failed" | "interrupted",
 *     reason?: string
 *   } | null) => void
 * }}
 */
export function createAgentPanel(root, rosClient, agentState, opts) {
  const selfOrigin = crypto.randomUUID?.() ?? `web-${Date.now()}-${Math.random()}`;
  const mic = opts.enableMic
    ? createMicStream(rosClient, (state) => opts.onMicState?.(state))
    : null;

  const panel = document.createElement("section");
  panel.className = "overlay agent-panel";
  const controlPanel = document.createElement("section");
  controlPanel.className = "agent-control-panel collapsed";
  const thoughtsPanel = document.createElement("section");
  thoughtsPanel.className = "agent-thoughts-panel";
  const chatToggle = document.createElement("button");
  chatToggle.type = "button";
  chatToggle.className = "agent-chat-toggle";
  chatToggle.innerHTML =
    '<svg class="agent-chat-toggle-icon agent-chat-toggle-chat" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 11.5a7.5 7.5 0 0 1-8 7.5 8.8 8.8 0 0 1-3.6-.8L4 20l1.5-3.8A7.2 7.2 0 0 1 4 11.5 7.5 7.5 0 0 1 12 4a7.5 7.5 0 0 1 8 7.5Z"/></svg><svg class="agent-chat-toggle-icon agent-chat-toggle-close" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="m7 7 10 10M17 7 7 17"/></svg><span class="agent-chat-toggle-thinking" aria-hidden="true"><i></i><i></i><i></i></span>';
  chatToggle.setAttribute("aria-controls", "agent-chat-panel");
  thoughtsPanel.id = "agent-chat-panel";
  let chatOpen = false;

  /** @param {boolean} open */
  function setChatOpen(open) {
    chatOpen = open;
    panel.classList.toggle("chat-open", open);
    chatToggle.classList.toggle("open", open);
    chatToggle.setAttribute("aria-expanded", String(open));
    chatToggle.setAttribute("aria-label", open ? "Close Chat with MARS" : "Open Chat with MARS");
  }

  /** @param {boolean} thinking */
  function setChatThinking(thinking) {
    chatToggle.classList.toggle("thinking", thinking);
    chatToggle.setAttribute("aria-busy", String(thinking));
  }

  chatToggle.addEventListener("click", () => setChatOpen(!chatOpen));
  setChatOpen(false);
  setChatThinking(false);

  // ---- header -------------------------------------------------------------
  const head = document.createElement("button");
  head.type = "button";
  head.className = "agent-head";
  head.setAttribute("aria-label", "Expand agent controls");
  head.setAttribute("aria-expanded", "false");
  const titleEl = document.createElement("span");
  titleEl.className = "agent-title";
  titleEl.innerHTML =
    '<svg class="agent-title-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3.5l1.7 6.8 6.8 1.7-6.8 1.7L12 20.5l-1.7-6.8L3.5 12l6.8-1.7z"/></svg>';
  const headCopy = document.createElement("span");
  headCopy.className = "agent-head-copy";
  const headLabel = document.createElement("span");
  headLabel.className = "agent-head-label";
  headLabel.textContent = "Agent";
  const headAgentName = document.createElement("span");
  headAgentName.className = "agent-head-agent-name";
  headAgentName.textContent = "—";
  headCopy.append(headLabel, headAgentName);
  const headChevron = document.createElement("span");
  headChevron.className = "agent-head-chev";
  headChevron.innerHTML =
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="9,6 15,12 9,18"/></svg>';
  /** @param {boolean} collapsed */
  function setControlsCollapsed(collapsed) {
    controlPanel.classList.toggle("collapsed", collapsed);
    head.setAttribute("aria-expanded", String(!collapsed));
    updateHeadLabel();
  }
  function updateHeadLabel() {
    const action = controlPanel.classList.contains("collapsed") ? "Expand" : "Collapse";
    head.setAttribute("aria-label", `${action} controls for ${headAgentName.textContent}`);
  }
  head.addEventListener("click", () => setControlsCollapsed(!controlPanel.classList.contains("collapsed")));
  head.append(titleEl, headCopy, headChevron);

  // ---- directive + start/stop --------------------------------------------
  const controls = document.createElement("div");
  controls.className = "agent-controls";

  const directivePicker = document.createElement("div");
  directivePicker.className = "agent-directive-picker";
  const directiveButton = document.createElement("button");
  directiveButton.type = "button";
  directiveButton.className = "agent-directive mono";
  directiveButton.setAttribute("role", "combobox");
  directiveButton.setAttribute("aria-label", "Agent");
  directiveButton.setAttribute("aria-haspopup", "listbox");
  directiveButton.setAttribute("aria-expanded", "false");
  directiveButton.title = `Pick the directive to run — ${GET_AVAILABLE_DIRECTIVES_SERVICE}`;
  const directiveValue = document.createElement("span");
  directiveValue.className = "agent-directive-value";
  directiveValue.textContent = "No agents available";
  const directiveChevron = document.createElement("span");
  directiveChevron.className = "agent-directive-chevron";
  directiveChevron.innerHTML =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="9,6 15,12 9,18"/></svg>';
  directiveButton.append(directiveValue, directiveChevron);
  const directiveList = document.createElement("div");
  directiveList.className = "agent-directive-list mono";
  directiveList.id = `agent-directive-list-${selfOrigin}`;
  directiveList.setAttribute("role", "listbox");
  directiveList.setAttribute("aria-label", "Agents");
  directiveButton.setAttribute("aria-controls", directiveList.id);
  directivePicker.append(directiveButton, directiveList);

  const toggleBtn = document.createElement("button");
  toggleBtn.type = "button";
  toggleBtn.className = "agent-toggle";

  const resetBtn = document.createElement("button");
  resetBtn.type = "button";
  resetBtn.className = "agent-reset";
  resetBtn.title = `Reset the agent's brain / working memory — ${RESET_BRAIN_SERVICE}`;
  resetBtn.setAttribute("aria-label", "Reset agent");
  resetBtn.innerHTML = '<span class="agent-reset-icon" aria-hidden="true"></span>';

  controls.append(directivePicker, toggleBtn, resetBtn);

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
  const streamLabel = document.createElement("p");
  streamLabel.className = "microlabel agent-stream-label";
  streamLabel.textContent = "Chat with MARS";
  streamLabel.title = `MARS chat, thoughts, and skill runs — ${CHAT_OUT_TOPIC}`;
  const streamHead = document.createElement("div");
  streamHead.className = "agent-stream-head";
  const streamMode = document.createElement("div");
  streamMode.className = "agent-stream-mode";
  streamMode.setAttribute("role", "group");
  streamMode.setAttribute("aria-label", "Chat detail");
  const compactBtn = modeButton("Simple");
  const detailedBtn = modeButton("Detailed");
  streamMode.append(compactBtn, detailedBtn);
  streamHead.append(streamLabel, streamMode);

  const stream = document.createElement("div");
  stream.className = "agent-stream compact";
  compactBtn.classList.add("active");
  compactBtn.setAttribute("aria-pressed", "true");
  detailedBtn.setAttribute("aria-pressed", "false");
  compactBtn.addEventListener("click", () => setStreamMode("compact"));
  detailedBtn.addEventListener("click", () => setStreamMode("detailed"));

  // ---- composer -----------------------------------------------------------
  const form = document.createElement("form");
  form.className = "agent-compose";
  const input = document.createElement("textarea");
  input.className = "agent-compose-input";
  input.rows = 1;
  input.setAttribute("aria-label", "Message MARS");
  const placeholder = document.createElement("span");
  placeholder.className = "agent-compose-placeholder";
  placeholder.textContent = CHAT_EXAMPLES[0];
  const send = document.createElement("button");
  send.type = "submit";
  send.className = "agent-compose-send";
  send.innerHTML = '<span class="agent-compose-send-icon" aria-hidden="true"></span>';
  send.setAttribute("aria-label", "Send message");
  send.title = "Send message";
  send.disabled = true;
  form.append(input, placeholder, send);
  let placeholderIndex = 0;
  /** @type {ReturnType<typeof setTimeout> | null} */
  let placeholderSwapTimer = null;
  const placeholderInterval = setInterval(() => {
    placeholderIndex = (placeholderIndex + 1) % CHAT_EXAMPLES.length;
    if (input.value.trim()) {
      placeholder.textContent = CHAT_EXAMPLES[placeholderIndex];
      return;
    }
    placeholder.classList.add("exiting");
    placeholderSwapTimer = setTimeout(() => {
      placeholder.textContent = CHAT_EXAMPLES[placeholderIndex];
      placeholder.classList.remove("exiting");
      placeholder.classList.add("entering");
      void placeholder.offsetWidth;
      placeholder.classList.remove("entering");
      placeholderSwapTimer = null;
    }, 500);
  }, 3500);

  const statusRow = document.createElement("div");
  statusRow.className = "agent-status-row";
  statusRow.append(activeSkill);
  controlPanel.append(head, controls, statusRow);
  thoughtsPanel.append(streamHead, stream, form);
  panel.append(controlPanel, thoughtsPanel);
  root.append(panel, chatToggle);

  // ---- directive roster + start/stop --------------------------------------
  // The dropdown ARMS a directive; Start activates it. While active, switching
  // the dropdown switches the running directive live. brain-active drives the
  // toggle label (Start <-> Stop). We remember the last non-empty directive so
  // Stop -> Start resumes the same one even though the brain reports "" when idle.
  let lastDirective = "";
  let applying = false;
  let wasBrainActive = false;
  let directiveOpen = false;
  let selectedDirective = "";
  /** @type {ReturnType<typeof setTimeout> | null} */
  let flashTimer = null;

  /** @param {boolean} open */
  function setDirectiveOpen(open) {
    directiveOpen = open && !directiveButton.disabled;
    directivePicker.classList.toggle("open", directiveOpen);
    directiveButton.setAttribute("aria-expanded", String(directiveOpen));
    if (!directiveOpen) return;
    const selected = directiveList.querySelector('[aria-selected="true"]');
    const first = directiveList.querySelector('[role="option"]');
    const target = /** @type {HTMLElement | null} */ (selected ?? first);
    requestAnimationFrame(() => target?.focus());
  }

  /** @param {string} id @param {string | undefined} error */
  function chooseDirective(id, error) {
    setDirectiveOpen(false);
    if (error !== undefined) {
      void copyText(error).then(
        () => {
          directiveValue.textContent = "Load error copied";
        },
        () => {
          directiveValue.textContent = "Copy failed";
        },
      );
      flashTimer = setTimeout(() => {
        flashTimer = null;
        renderRoster();
      }, 1200);
      return;
    }
    if (!id) return;
    selectedDirective = id;
    lastDirective = id;
    const agent = agentState.get().agents.find((candidate) => candidate.id === id);
    directiveValue.textContent = agent?.name ?? id;
    headAgentName.textContent = directiveValue.textContent;
    updateHeadLabel();
    if (agentState.get().brainActive) void withApplying(() => agentState.setDirective(id));
  }

  /** @param {KeyboardEvent} event */
  function onDirectiveKeydown(event) {
    const options = /** @type {HTMLElement[]} */ ([...directiveList.querySelectorAll('[role="option"]')]);
    if (event.currentTarget === directiveButton && ["ArrowDown", "ArrowUp"].includes(event.key)) {
      event.preventDefault();
      setDirectiveOpen(true);
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      setDirectiveOpen(false);
      directiveButton.focus();
      return;
    }
    const current = Math.max(0, options.indexOf(/** @type {HTMLElement} */ (document.activeElement)));
    let next = current;
    if (event.key === "ArrowDown") next = Math.min(current + 1, options.length - 1);
    else if (event.key === "ArrowUp") next = Math.max(current - 1, 0);
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = options.length - 1;
    else return;
    event.preventDefault();
    options[next]?.focus();
  }

  /** @param {MouseEvent} event */
  function onDirectiveOutsideClick(event) {
    if (!directivePicker.contains(/** @type {Node} */ (event.target))) setDirectiveOpen(false);
  }

  directiveButton.addEventListener("click", () => setDirectiveOpen(!directiveOpen));
  directiveButton.addEventListener("keydown", onDirectiveKeydown);
  directiveList.addEventListener("keydown", onDirectiveKeydown);
  directivePicker.addEventListener("click", (event) => event.stopPropagation());
  document.addEventListener("click", onDirectiveOutsideClick);

  function renderRoster() {
    if (flashTimer) return; // keep the copy-feedback row until it expires
    const { agents, broken, currentDirective, brainActive } = agentState.get();
    if (currentDirective) lastDirective = currentDirective;

    const demo = agents.find((a) => /demo\s*agent/i.test(a.name) || /demo/i.test(a.id));
    const armed = currentDirective || lastDirective || demo?.id || (agents[0]?.id ?? "");
    directiveList.replaceChildren();
    for (const agent of agents) {
      const option = document.createElement("button");
      option.type = "button";
      option.className = "agent-directive-option";
      option.setAttribute("role", "option");
      option.setAttribute("aria-selected", String(agent.id === armed));
      option.innerHTML =
        '<span class="agent-directive-check" aria-hidden="true"><svg viewBox="0 0 14 11" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m1.5 5.5 3.3 3.3 7.7-7.3"/></svg></span>';
      const name = document.createElement("span");
      name.className = "agent-directive-option-name";
      name.textContent = agent.name;
      option.append(name);
      option.addEventListener("click", () => chooseDirective(agent.id, undefined));
      directiveList.append(option);
    }
    for (const b of broken ?? []) {
      const preview = b.error.length > 60 ? b.error.slice(0, 59) + "…" : b.error;
      const option = document.createElement("button");
      option.type = "button";
      option.className = "agent-directive-option broken";
      option.setAttribute("role", "option");
      option.setAttribute("aria-selected", "false");
      option.title = `${b.error}\n\nSelect to copy the full error.`;
      const warning = document.createElement("span");
      warning.className = "agent-directive-warning";
      warning.textContent = "!";
      const copy = document.createElement("span");
      copy.className = "agent-directive-option-copy";
      const name = document.createElement("span");
      name.className = "agent-directive-option-name";
      name.textContent = b.name;
      const error = document.createElement("span");
      error.className = "agent-directive-option-error";
      error.textContent = preview;
      copy.append(name, error);
      option.append(warning, copy);
      option.addEventListener("click", () => chooseDirective("", b.error));
      directiveList.append(option);
    }
    if (agents.length === 0 && (broken ?? []).length === 0) {
      const empty = document.createElement("span");
      empty.className = "agent-directive-empty";
      empty.textContent = "No agents available";
      directiveList.append(empty);
    }
    const selectedAgent = agents.find((agent) => agent.id === armed);
    selectedDirective = selectedAgent?.id ?? "";
    directiveValue.textContent = selectedAgent?.name ?? "No agents available";
    headAgentName.textContent = selectedAgent?.name ?? "No agent";
    updateHeadLabel();

    panel.classList.toggle("active", brainActive);
    if (brainActive && !wasBrainActive && controlPanel.classList.contains("collapsed")) {
      setControlsCollapsed(false);
    }
    wasBrainActive = brainActive;

    const action = brainActive ? "Stop" : "Start";
    const agentName = selectedAgent?.name ?? "agent";
    toggleBtn.textContent = action;
    toggleBtn.title = `${action} ${agentName} — ${SET_BRAIN_ACTIVE_SERVICE}`;
    toggleBtn.setAttribute("aria-label", `${action} ${agentName}`);
    toggleBtn.setAttribute("aria-pressed", String(brainActive));
    toggleBtn.setAttribute("aria-busy", String(applying));
    toggleBtn.classList.toggle("stop", brainActive);
    toggleBtn.disabled = applying || (!brainActive && agents.length === 0);
    // Keep the picker openable when only broken agents exist, so their rows
    // stay reachable; the change handler ignores non-agent values.
    directiveButton.disabled = applying || (agents.length === 0 && (broken ?? []).length === 0);
    if (directiveButton.disabled) setDirectiveOpen(false);
    resetBtn.disabled = applying;
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

  // No local "started./stopped." echo: the brain announces both on
  // /brain/chat_out (and into history), so every client — not just the one
  // whose button was pressed — renders the same message via the normal
  // chat_out path.
  toggleBtn.addEventListener("click", () => {
    const { brainActive } = agentState.get();
    if (brainActive) {
      void withApplying(() => agentState.setDirective(""));
    } else {
      const id = selectedDirective || lastDirective;
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
  /** @type {HTMLElement | null} */
  let compactPrompt = null;
  /** @type {HTMLElement[]} */
  const compactMessages = [];
  let replayingHistory = false;
  /** @type {Set<ReturnType<typeof setTimeout>>} */
  const compactEnterTimers = new Set();
  /** @type {Set<ReturnType<typeof setTimeout>>} */
  const compactExitTimers = new Set();

  /** @param {"compact" | "detailed"} mode */
  function setStreamMode(mode) {
    const compact = mode === "compact";
    stream.classList.toggle("compact", compact);
    stream.classList.toggle("detailed", !compact);
    streamMode.classList.toggle("detailed-selected", !compact);
    compactBtn.classList.toggle("active", compact);
    detailedBtn.classList.toggle("active", !compact);
    compactBtn.setAttribute("aria-pressed", String(compact));
    detailedBtn.setAttribute("aria-pressed", String(!compact));
    for (const card of stream.querySelectorAll(".chat-skill.failed.has-detail")) {
      const head = card.querySelector(".chat-skill-head");
      if (card instanceof HTMLElement && head instanceof HTMLButtonElement) {
        setSkillElementOpen(card, head, !compact);
      }
    }
    if (!compact) stream.scrollTop = stream.scrollHeight;
  }

  /** @param {HTMLElement} message */
  function hideCompactMessage(message) {
    message.classList.remove("compact-visible", "compact-entering");
    if (stream.classList.contains("compact") && !replayingHistory) {
      message.classList.add("compact-exiting");
      const timer = setTimeout(() => {
        message.classList.remove("compact-exiting");
        compactExitTimers.delete(timer);
      }, 140);
      compactExitTimers.add(timer);
    } else {
      message.classList.remove("compact-exiting");
    }
  }

  /** @param {HTMLElement} next @param {boolean} isPrompt */
  function showCompactMessage(next, isPrompt) {
    if (isPrompt) {
      if (compactPrompt) {
        compactPrompt.classList.remove("compact-prompt");
        hideCompactMessage(compactPrompt);
      }
      for (const message of compactMessages) hideCompactMessage(message);
      compactMessages.length = 0;
      compactPrompt = next;
      next.classList.add("compact-prompt");
    } else {
      compactMessages.push(next);
    }

    next.classList.remove("compact-exiting");
    next.classList.add("compact-visible");
    if (stream.classList.contains("compact") && !replayingHistory) {
      next.classList.add("compact-entering");
      const timer = setTimeout(() => {
        next.classList.remove("compact-entering");
        compactEnterTimers.delete(timer);
      }, 180);
      compactEnterTimers.add(timer);
    }

    while (compactMessages.length > 7) {
      const previous = compactMessages.shift();
      if (previous) hideCompactMessage(previous);
    }
  }

  /** @param {boolean} active */
  function setThoughtsStatus(active) {
    if (!thoughts) return;
    const d = Math.max(0, Math.ceil(thoughts.latestTs - (lastTs || thoughts.startTs)));
    const word = active ? "Processing" : "Processed";
    thoughts.status.textContent = d > 0 ? `${word} for ${d} second${d === 1 ? "" : "s"}` : `${word}…`;
    thoughts.wrap.classList.toggle("active", active);
    if (!replayingHistory) setChatThinking(active);
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
      toggle.addEventListener("click", () => {
        const open = wrap.classList.toggle("open");
        arrow.textContent = open ? "▴" : "▾";
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
    el.classList.toggle("skill-output", label === "skill_output");
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
    if (label !== "skill_output") showCompactMessage(el, kind === "user");
    lastTs = ts;
    snapIfAtBottom(wasAtBottom);
  }

  /** @type {Map<string, {
   *  wrap: HTMLElement,
   *  head: HTMLButtonElement,
   *  summary: HTMLElement,
   *  status: HTMLElement,
   *  chevron: HTMLElement,
   *  parameters: HTMLElement,
   *  failure: HTMLElement,
   *  hasDetail: boolean
   * }>} */
  const skillRuns = new Map();

  /** @param {string} key @param {string} name @param {string} status @param {number} ts @param {string} [reason]
   *  @param {any} [args] @param {boolean} [preview] */
  function addSkillRun(key, name, status, ts, reason, args, preview = false) {
    const wasAtBottom = atBottom();
    const cls = ["running", "completed", "failed", "interrupted"].includes(status) ? status : "running";
    if (!preview && cls === "running") {
      activeSkillName.textContent = name.replace(/_/g, " ");
      activeSkill.classList.add("on");
    } else if (!preview && activeSkillName.textContent === name.replace(/_/g, " ")) {
      activeSkillName.textContent = "—";
      activeSkill.classList.remove("on");
    }

    let run = skillRuns.get(key);
    if (!run) {
      finalizeThoughts();
      const wrap = document.createElement("div");
      wrap.className = "chat-skill";
      const head = document.createElement("button");
      head.type = "button";
      head.className = "chat-skill-head";
      head.setAttribute("aria-expanded", "false");
      const icon = document.createElement("span");
      icon.className = "chat-skill-icon";
      icon.setAttribute("aria-hidden", "true");
      const copy = document.createElement("span");
      copy.className = "chat-skill-copy";
      const nameEl = document.createElement("span");
      nameEl.className = "chat-skill-name";
      nameEl.textContent = name.replace(/_/g, " ");
      const summary = document.createElement("span");
      summary.className = "chat-skill-summary";
      copy.append(nameEl, summary);
      const statusEl = document.createElement("span");
      statusEl.className = "chat-skill-status";
      statusEl.title = SKILL_STATUS_UPDATE_TOPIC;
      const chevron = document.createElement("span");
      chevron.className = "chat-skill-chevron";
      chevron.innerHTML =
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9,6 15,12 9,18"/></svg>';
      chevron.setAttribute("aria-hidden", "true");
      head.append(icon, copy, statusEl, chevron);
      const detail = document.createElement("div");
      detail.className = "chat-skill-detail";
      const parameters = document.createElement("div");
      parameters.className = "chat-skill-parameters";
      const failure = document.createElement("div");
      failure.className = "chat-skill-failure";
      detail.append(parameters, failure);
      wrap.append(head, detail);
      stream.appendChild(wrap);
      const createdRun = { wrap, head, summary, status: statusEl, chevron, parameters, failure, hasDetail: false };
      run = createdRun;
      head.addEventListener("click", () => {
        if (!createdRun.hasDetail) return;
        setSkillRunOpen(createdRun, !createdRun.wrap.classList.contains("open"));
      });
      skillRuns.set(key, createdRun);
      if (preview) wrap.classList.add("compact-visible");
      else showCompactMessage(wrap, false);
    }
    run.wrap.classList.remove("running", "completed", "failed", "interrupted");
    run.wrap.classList.add(cls);
    const inputs = formatSkillArgs(name, args);
    if (inputs.rows.length) {
      run.summary.textContent = inputs.summary;
      renderSkillParameters(run.parameters, inputs.rows);
      run.hasDetail = true;
    }
    run.status.textContent = skillStatusLabel(cls);
    run.head.setAttribute("aria-label", `${name.replace(/_/g, " ")}: ${run.status.textContent}`);

    if (cls === "failed" && reason) {
      renderSkillFailure(run.failure, reason);
      run.hasDetail = true;
    }
    run.wrap.classList.toggle("has-detail", run.hasDetail);
    run.head.title = run.hasDetail ? "Show skill details" : "";
    if (cls === "failed") setSkillRunOpen(run, !stream.classList.contains("compact"));

    lastTs = ts;
    if (cls !== "running") skillRuns.delete(key);
    snapIfAtBottom(wasAtBottom);
    return run.wrap;
  }

  /** @type {HTMLElement | null} */
  let skillPreview = null;
  /** @param {{
   *  name: string,
   *  args: Record<string, unknown>,
   *  status: "running" | "completed" | "failed" | "interrupted",
   *  reason?: string
   * } | null} sample */
  function previewSkill(sample) {
    skillPreview?.remove();
    skillRuns.delete("__skill-preview__");
    skillPreview = null;
    if (!sample) return;
    setChatOpen(true);
    skillPreview = addSkillRun(
      "__skill-preview__",
      sample.name,
      sample.status,
      Date.now() / 1000,
      sample.status === "failed" ? sample.reason || "The skill could not complete." : "",
      sample.args,
      true,
    );
    stream.scrollTop = stream.scrollHeight;
  }

  // ---- composer -----------------------------------------------------------
  // Talking to an idle agent means "start it" — an inactive brain drops
  // chat_in, and the mic device is only open while a directive runs.
  async function startIfIdle() {
    if (agentState.get().brainActive) return;
    const id = selectedDirective || lastDirective;
    if (id) await withApplying(() => agentState.setDirective(id));
  }

  async function startMic() {
    setChatOpen(true);
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
    send.disabled = true;
    placeholder.classList.remove("hidden");
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
    const empty = input.value.trim().length === 0;
    send.disabled = empty;
    placeholder.classList.toggle("hidden", !empty);
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
    for (const timer of compactEnterTimers) clearTimeout(timer);
    compactEnterTimers.clear();
    for (const timer of compactExitTimers) clearTimeout(timer);
    compactExitTimers.clear();
    compactPrompt = null;
    compactMessages.length = 0;
    thoughts = null;
    setChatThinking(false);
    skillRuns.clear();
    lastTs = 0;
    replayingHistory = true;
    stream.classList.add("replaying");
    try {
      for (const e of entries) replayEntry(e);
    } finally {
      replayingHistory = false;
      stream.classList.remove("replaying");
    }
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
      addSkillRun(key, name, status, ts, typeof e?.failureReason === "string" ? e.failureReason : "", e?.args);
      return;
    }
    const text = String(e?.text ?? "");
    if (!text) return;
    if (sender === "robot_thoughts" || sender === "robot_anticipation") {
      addThought(sender, text, ts);
    } else if (sender === "vision_agent_output") {
      return; // raw vision dumps — noisy, drop (matches live)
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
    setChatOpen(true);
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
    addSkillRun(key, name, status, Number(payload?.timestamp) || Date.now() / 1000, reason, payload?.args);
  }, undefined, "std_msgs/msg/String");

  return {
    startMic,
    stopMic,
    previewSkill,
    dismissOverlays() {
      setChatOpen(false);
      setDirectiveOpen(false);
      setControlsCollapsed(true);
    },
    destroy() {
      mic?.destroy();
      document.removeEventListener("click", onDirectiveOutsideClick);
      if (flashTimer) clearTimeout(flashTimer);
      clearInterval(placeholderInterval);
      if (placeholderSwapTimer) clearTimeout(placeholderSwapTimer);
      for (const timer of compactEnterTimers) clearTimeout(timer);
      for (const timer of compactExitTimers) clearTimeout(timer);
      unsubAgents();
      unsubConn();
      unsubIn();
      unsubOut();
      unsubSkill();
      panel.remove();
      chatToggle.remove();
    },
  };
}

/** @param {string} label */
function modeButton(label) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "agent-stream-mode-btn";
  btn.textContent = label;
  return btn;
}

/** @param {{wrap: HTMLElement, head: HTMLButtonElement}} run @param {boolean} open */
function setSkillRunOpen(run, open) {
  setSkillElementOpen(run.wrap, run.head, open);
}

/** @param {HTMLElement} wrap @param {HTMLButtonElement} head @param {boolean} open */
function setSkillElementOpen(wrap, head, open) {
  wrap.classList.toggle("open", open);
  head.setAttribute("aria-expanded", String(open));
  head.title = open ? "Hide skill details" : "Show skill details";
}

/**
 * @param {string} name
 * @param {any} args
 * @returns {{ summary: string, rows: Array<{label: string, value: string}> }}
 */
function formatSkillArgs(name, args) {
  if (!args || typeof args !== "object" || Array.isArray(args)) return { summary: "", rows: [] };
  const entries = Object.entries(args).filter(([, v]) => v !== null && v !== undefined && v !== "");
  if (!entries.length) return { summary: "", rows: [] };
  const rows = entries.map(([key, value]) => ({
    label: skillArgLabel(key),
    value: skillArgValue(name, key, value),
  }));
  return { summary: skillArgSummary(name, args, rows), rows };
}

/** @param {HTMLElement} container @param {Array<{label: string, value: string}>} rows */
function renderSkillParameters(container, rows) {
  const label = document.createElement("p");
  label.className = "chat-skill-detail-label";
  label.textContent = "Parameters";
  const list = document.createElement("dl");
  for (const row of rows) {
    const term = document.createElement("dt");
    term.textContent = row.label;
    const value = document.createElement("dd");
    value.textContent = row.value;
    list.append(term, value);
  }
  container.replaceChildren(label, list);
}

/** @param {HTMLElement} container @param {string} reason */
function renderSkillFailure(container, reason) {
  const label = document.createElement("p");
  label.className = "chat-skill-detail-label";
  label.textContent = "Failure";
  const message = document.createElement("p");
  message.className = "chat-skill-failure-message";
  message.textContent = reason;
  container.replaceChildren(label, message);
}

/** @param {string} status */
function skillStatusLabel(status) {
  return {
    running: "Running",
    completed: "Completed",
    failed: "Failed",
    interrupted: "Interrupted",
  }[status] || "Running";
}

/** @param {string} key */
function skillArgLabel(key) {
  const labels = {
    theta_degrees: "Heading",
    angle_degrees: "Angle",
    local_frame: "Frame",
    num_loops: "Loops",
    points_per_loop: "Points per loop",
    duration_per_point: "Point duration",
    is_capture: "Capture",
    keep_gripper: "Keep gripper",
  };
  if (key in labels) return labels[/** @type {keyof typeof labels} */ (key)];
  if (/^[xyz]$/.test(key)) return key.toUpperCase();
  const text = key.replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** @param {string} name @param {string} key @param {any} value */
function skillArgValue(name, key, value) {
  if (key === "local_frame") return value ? "Local" : "Map";
  if (key === "is_capture" || key === "keep_gripper") return value ? "Yes" : "No";
  if (typeof value === "object") return roundNums(JSON.stringify(value));
  if (typeof value !== "number") return String(value).replace(/_/g, " ");
  const number = roundNums(String(value));
  if (key === "theta_degrees" || key === "angle_degrees") return `${number}°`;
  if (/^(x|y|z|center_x|center_y|center_z|radius|distance)$/.test(key)) return `${number} m`;
  if (key.includes("duration")) return `${number} s`;
  if (key === "speed" && name.replace(/_/g, " ").includes("turn")) return `${number} rad/s`;
  if (key === "speed" && name.replace(/_/g, " ").includes("move")) return `${number} m/s`;
  return number;
}

/** @param {string} name @param {Record<string, any>} args @param {Array<{label: string, value: string}>} rows */
function skillArgSummary(name, args, rows) {
  const skill = name.replace(/_/g, " ").toLowerCase();
  if (skill.includes("navigate to position")) {
    return `x ${roundNums(String(args.x))} m · y ${roundNums(String(args.y))} m · ${roundNums(String(args.theta_degrees ?? 0))}° · ${args.local_frame ? "Local" : "Map"}`;
  }
  if (skill.includes("move straight")) {
    const distance = Number(args.distance);
    return `${roundNums(String(Math.abs(distance)))} m ${distance < 0 ? "backward" : "forward"} · ${roundNums(String(args.speed))} m/s`;
  }
  if (skill.includes("turn in place")) {
    const angle = Number(args.angle_degrees);
    return `${roundNums(String(Math.abs(angle)))}° ${angle < 0 ? "right" : "left"} · ${roundNums(String(args.speed))} rad/s`;
  }
  if (skill.includes("head emotion")) {
    return `${String(args.emotion).replace(/_/g, " ")}${Number(args.repeat) > 1 ? ` ×${args.repeat}` : ""}`;
  }
  if (skill.includes("search memory")) return `“${String(args.query)}”`;
  if (skill.includes("arm move to xyz")) {
    return `x ${roundNums(String(args.x))} m · y ${roundNums(String(args.y))} m · z ${roundNums(String(args.z))} m`;
  }
  return rows
    .slice(0, 3)
    .map(({ label, value }) => `${label} ${value}`)
    .join(" · ");
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
