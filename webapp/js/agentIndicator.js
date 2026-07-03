// @ts-check
// Top-center "<agent> is running" pill, shown on every page except the Agent
// page itself while the brain is active. It's a link: clicking jumps to the
// Agent page to take control. Glass + full pill shape mirror the speak bar.

/**
 * @param {ReturnType<import("./teleop/agentState.js").createAgentState>} agentState
 * @param {string} agentHref Link to the Agent page (root-relative per page depth).
 * @returns {{ destroy: () => void }}
 */
export function createAgentIndicator(agentState, agentHref) {
  const pill = document.createElement("a");
  pill.className = "agent-indicator";
  pill.href = agentHref;
  pill.hidden = true;

  const dot = document.createElement("span");
  dot.className = "agent-indicator-dot";
  const label = document.createElement("span");
  label.className = "agent-indicator-label";
  const stop = document.createElement("button");
  stop.type = "button";
  stop.className = "agent-indicator-stop";
  stop.textContent = "Stop";
  pill.append(dot, label, stop);
  document.body.appendChild(pill);

  // Stop the brain in place — the click must not also follow the link to the
  // Agent page. The roster refresh then hides the pill.
  stop.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    stop.disabled = true;
    Promise.resolve(agentState.setDirective("")).finally(() => {
      stop.disabled = false;
    });
  });

  const unsub = agentState.subscribe((s) => {
    pill.hidden = !s.brainActive;
    if (!s.brainActive) return;
    const agent = s.agents.find((a) => a.id === s.currentDirective);
    label.textContent = `${agent?.name || "Agent"} is running`;
  });

  return {
    destroy() {
      unsub();
      pill.remove();
    },
  };
}
