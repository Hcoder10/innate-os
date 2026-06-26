// @ts-check
// Chat panel — a pane in the shared right dock (see rightDock.js) for talking to
// the brain/agent. Operator text goes out on /brain/chat_in; the agent's replies
// plus system / thought / skill-output lines stream back on /brain/chat_out. The
// brain does not echo the operator's own messages, so those are rendered locally
// on send.

import { CHAT_IN_TOPIC, CHAT_OUT_TOPIC, SKILL_STATUS_UPDATE_TOPIC } from "../constants.js";

/**
 * @param {ReturnType<import("./rightDock.js").createRightDock>} dock
 * @param {import("../rosClient.js").RosClient} rosClient
 * @param {ReturnType<import("./agentState.js").createAgentState>} agentState
 * @returns {{ destroy: () => void }}
 */
export function createChatPanel(dock, rosClient, agentState) {
  // Bottom pane of the dock; toggle sits three-quarters down the right edge.
  const { body } = dock.addPanel({ key: "chat", label: "Chat", togglePos: 0.75 });

  const head = document.createElement("div");
  head.className = "dock-head";
  const title = document.createElement("p");
  title.className = "microlabel";
  title.textContent = "chat";
  head.append(title);

  // Agent bar — pick the active directive (None = brain idle) from the roster
  // discovered on the brain.
  const agentBar = document.createElement("div");
  agentBar.className = "chat-agentbar";
  const agentLabel = document.createElement("span");
  agentLabel.className = "chat-agent-label microlabel";
  agentLabel.textContent = "agent";
  const agentSelect = document.createElement("select");
  agentSelect.className = "chat-agent-select mono";
  const resetBrainBtn = document.createElement("button");
  resetBrainBtn.type = "button";
  resetBrainBtn.className = "chat-reset-brain";
  resetBrainBtn.title = "Reset the agent's brain / working memory";
  resetBrainBtn.textContent = "Reset";
  agentBar.append(agentLabel, agentSelect, resetBrainBtn);

  const messages = document.createElement("div");
  messages.className = "chat-messages";

  const form = document.createElement("form");
  form.className = "chat-input-row";
  const input = document.createElement("textarea");
  input.className = "chat-input mono";
  input.rows = 1;
  input.placeholder = "Message the robot…";
  const send = document.createElement("button");
  send.type = "submit";
  send.className = "chat-send";
  send.textContent = "Send";
  form.append(input, send);

  body.append(head, agentBar, messages, form);

  // ---- agent roster + selection (shared agentState) ----------------------

  function renderAgents() {
    const { agents, currentDirective } = agentState.get();
    agentSelect.replaceChildren();
    agentSelect.appendChild(new Option("None", ""));
    for (const a of agents) {
      const opt = new Option(a.name, a.id);
      if (a.id === currentDirective) opt.selected = true;
      agentSelect.appendChild(opt);
    }
    agentSelect.value = currentDirective || "";
  }
  const unsubAgents = agentState.subscribe(renderAgents);

  agentSelect.addEventListener("change", () => agentState.setDirective(agentSelect.value));
  resetBrainBtn.addEventListener("click", () => {
    if (!window.confirm("Reset the agent's brain? This clears its working memory.")) return;
    agentState.resetBrain().catch(() => {});
  });

  function scrollToLatest() {
    const atBottom = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 60;
    if (atBottom) messages.scrollTop = messages.scrollHeight;
  }

  // ---- thinking-stream grouping (mirrors the sim console) -----------------
  // Contiguous robot_thoughts / robot_anticipation collapse into one expandable
  // "Thoughts" block (collapsed by default, deduped) so the agent's reasoning
  // doesn't bury the actual reply. Tool-call output (vision/skill) is dropped.

  /** @type {{ wrap: HTMLElement, status: HTMLElement, list: HTMLElement, lastByKind: Record<string, string>, startTs: number, latestTs: number } | null} */
  let thoughts = null;
  let lastTs = 0; // timestamp of the last finalized (non-thought) message

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
    if (!thoughts) {
      const wrap = document.createElement("div");
      wrap.className = "chat-thoughts";
      const toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "chat-thoughts-toggle";
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
      messages.appendChild(wrap);
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
    scrollToLatest();
  }

  /** @param {string} kind @param {string} text @param {number} ts @param {string} [label] */
  function addMessage(kind, text, ts, label) {
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
    messages.appendChild(el);
    lastTs = ts;
    scrollToLatest();
  }

  // ---- skill execution status (which primitive the agent is running) ------
  /** @type {Map<string, { wrap: HTMLElement, status: HTMLElement }>} */
  const skillRuns = new Map();

  /** @param {string} key @param {string} name @param {string} status @param {number} ts */
  function addSkillRun(key, name, status, ts) {
    const cls = ["running", "completed", "failed", "interrupted"].includes(status) ? status : "running";
    let run = skillRuns.get(key);
    if (!run) {
      finalizeThoughts();
      const wrap = document.createElement("div");
      wrap.className = "chat-skill";
      const tag = document.createElement("span");
      tag.className = "chat-skill-tag mono";
      tag.textContent = "skill";
      const nameEl = document.createElement("span");
      nameEl.className = "chat-skill-name";
      nameEl.textContent = name.replace(/_/g, " ");
      const statusEl = document.createElement("span");
      statusEl.className = "chat-skill-status mono";
      wrap.append(tag, nameEl, statusEl);
      messages.appendChild(wrap);
      run = { wrap, status: statusEl };
      skillRuns.set(key, run);
    }
    run.wrap.className = `chat-skill ${cls}`;
    run.status.textContent = cls;
    lastTs = ts;
    // Once a run reaches a terminal state, let a fresh run of the same primitive
    // start a new line rather than reusing this one.
    if (cls !== "running") skillRuns.delete(key);
    scrollToLatest();
  }

  function submit() {
    const text = input.value.trim();
    if (!text) return;
    addMessage("user", text, Date.now() / 1000);
    rosClient.publish(CHAT_IN_TOPIC, {
      data: JSON.stringify({ text, sender: "user", timestamp: Date.now() / 1000 }),
    });
    input.value = "";
    input.style.height = "auto";
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    submit();
  });
  input.addEventListener("keydown", (e) => {
    // Enter sends; Shift+Enter inserts a newline.
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 120)}px`;
  });

  const unsub = rosClient.subscribe(CHAT_OUT_TOPIC, (m) => {
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
      // Reasoning — collapse into the grouped "Thoughts" block.
      addThought(sender, text, ts);
    } else if (sender === "vision_agent_output") {
      // Raw vision dumps — the one genuinely noisy stream; drop it.
      return;
    } else if (sender === "user" || sender === "robot") {
      addMessage(sender, text, ts);
    } else {
      // system, task_activated, skill_output (skills the agent runs), and any
      // other line — keep, tagged with the sender so it's legible but compact.
      addMessage("system", text, ts, sender);
    }
  });

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
    const ts = Number(payload?.timestamp) || Date.now() / 1000;
    addSkillRun(key, name, status, ts);
  });

  return {
    destroy() {
      unsub();
      unsubSkill();
      unsubAgents();
    },
  };
}

/**
 * Shorten long decimals in a display string to 2 places (e.g.
 * "1.8369701987210297e-16" -> "0", "3.14159…" -> "3.14"). Integers are left
 * alone. Not a parse — just a cosmetic squeeze of float-looking substrings.
 * @param {string} text
 */
function roundNums(text) {
  return text.replace(/-?\d+\.\d+(?:[eE][-+]?\d+)?/g, (n) => {
    const v = Number(n);
    return Number.isFinite(v) ? String(Math.round(v * 100) / 100) : n;
  });
}
