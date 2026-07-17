// @ts-check
// Challenge panel (sim only) — top-left card on the Agent page. Renders the
// world server's challenge judge state relayed through the sim session
// (session.onChallenge, see challenges.py): a roster of challenges to start,
// and while one runs, its goal checklist, timer, and pass/fail banner. All
// judging happens server-side against ground truth; this panel is a thin
// renderer plus two commands (start/abort).

/**
 * @param {HTMLElement} container
 * @param {any} session sim session exposing onChallenge/startChallenge/abortChallenge
 * @returns {{ destroy: () => void }}
 */
export function createChallengePanel(container, session) {
  const panel = document.createElement("section");
  panel.className = "overlay challenge-panel";
  panel.hidden = true; // until the first challenge block arrives

  const head = document.createElement("div");
  head.className = "challenge-head";
  const title = document.createElement("span");
  title.className = "microlabel";
  title.textContent = "Challenges";
  head.append(title);

  const body = document.createElement("div");
  body.className = "challenge-body";
  panel.append(head, body);
  container.append(panel);

  /** Last rendered structure (block minus the ticking clock) — the timer text
   * updates in place so the DOM isn't rebuilt 10x a second. */
  let renderedKey = "";
  /** @type {HTMLElement | null} */
  let timerEl = null;

  const unsub = session.onChallenge((/** @type {any} */ block) => {
    panel.hidden = false;
    const active = block.active;
    if (timerEl && active) timerEl.textContent = timerText(active);
    const key = JSON.stringify({ ...block, active: active && { ...active, elapsed_s: null } });
    if (key === renderedKey) return;
    renderedKey = key;
    body.replaceChildren(active ? renderActive(active, block.list) : renderList(block.list));
  });

  /** @param {any[]} list */
  function renderList(list) {
    timerEl = null;
    const wrap = document.createElement("div");
    wrap.className = "challenge-list";
    if (!list.length) {
      const empty = document.createElement("div");
      empty.className = "challenge-empty";
      empty.textContent = "No challenges installed (sim/challenges/).";
      wrap.append(empty);
      return wrap;
    }
    for (const c of list) {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "challenge-item";
      const dot = document.createElement("span");
      dot.className = `challenge-dot${c.passed ? " passed" : ""}`;
      const text = document.createElement("div");
      text.className = "challenge-item-text";
      const name = document.createElement("div");
      name.className = "challenge-item-title";
      name.textContent = c.title;
      const meta = document.createElement("div");
      meta.className = "challenge-item-meta";
      meta.textContent = c.passed
        ? `passed${c.best_time_s != null ? ` · best ${fmtClock(c.best_time_s)}` : ""}`
        : c.attempts
          ? `${c.attempts} ${c.attempts === 1 ? "attempt" : "attempts"}`
          : "not attempted";
      text.append(name, meta);
      const play = document.createElement("span");
      play.className = "challenge-play";
      play.textContent = "▶";
      item.append(dot, text, play);
      item.title = c.brief;
      item.addEventListener("click", () => session.startChallenge(c.id));
      wrap.append(item);
    }
    return wrap;
  }

  /**
   * @param {any} active
   * @param {any[]} list
   */
  function renderActive(active, list) {
    const info = list.find((c) => c.id === active.id);
    const wrap = document.createElement("div");
    wrap.className = "challenge-active";

    const titleRow = document.createElement("div");
    titleRow.className = "challenge-title-row";
    const name = document.createElement("span");
    name.className = "challenge-item-title";
    name.textContent = info ? info.title : active.id;
    timerEl = document.createElement("span");
    timerEl.className = `challenge-timer${active.state !== "running" ? " final" : ""}`;
    timerEl.textContent = timerText(active);
    titleRow.append(name, timerEl);
    wrap.append(titleRow);

    if (info && active.state === "running") {
      const brief = document.createElement("div");
      brief.className = "challenge-brief";
      brief.textContent = info.brief;
      wrap.append(brief);
    }

    const goals = document.createElement("ul");
    goals.className = "challenge-goals";
    for (const g of active.goals) {
      const li = document.createElement("li");
      li.className = g.done ? "done" : "";
      li.textContent = g.label;
      goals.append(li);
    }
    wrap.append(goals);

    if (active.state !== "running") {
      const banner = document.createElement("div");
      banner.className = `challenge-banner ${active.state}`;
      banner.textContent =
        active.state === "passed"
          ? `Passed in ${fmtClock(active.elapsed_s)}`
          : `Failed${active.reason ? ` — ${active.reason}` : ""}`;
      wrap.append(banner);
    }

    const actions = document.createElement("div");
    actions.className = "challenge-actions";
    if (active.state === "running") {
      actions.append(actionButton("Abort", () => session.abortChallenge()));
    } else {
      actions.append(
        actionButton("Retry", () => session.startChallenge(active.id)),
        actionButton("Done", () => session.abortChallenge()),
      );
    }
    wrap.append(actions);
    return wrap;
  }

  /**
   * @param {string} label
   * @param {() => void} onClick
   */
  function actionButton(label, onClick) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "challenge-action";
    btn.textContent = label;
    btn.addEventListener("click", onClick);
    return btn;
  }

  /** @param {any} active */
  function timerText(active) {
    if (active.state !== "running") return fmtClock(active.elapsed_s);
    if (active.time_limit_s != null) return `${fmtClock(Math.max(0, active.time_limit_s - active.elapsed_s))} left`;
    return fmtClock(active.elapsed_s);
  }

  /** @param {number} s */
  function fmtClock(s) {
    const m = Math.floor(s / 60);
    return `${m}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
  }

  return {
    destroy() {
      unsub();
      panel.remove();
    },
  };
}
