// @ts-check
// "Running now" hero card — a compact, prominent banner for the currently
// training run (step progress + ETA + actions), shown above the skill list.
// Detailed run info lives in the right-side detail pane (runDetail.js).

const STATUS_RUNNING = 5;

/** Format seconds as "1h05m" / "9m" / "45s". @param {number} secs */
function fmtDur(secs) {
  if (secs < 60) return `${secs}s`;
  const m = Math.floor(secs / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h${String(m % 60).padStart(2, "0")}m`;
}

/** Build the "Running now" hero card for a running run.
 * @param {object} a
 * @param {TrainingSkillStatus} a.sk
 * @param {TrainingRunStatus} a.run
 * @param {number|null} a.eta  seconds remaining, or null
 * @param {HTMLElement[]} a.actions  action buttons (built by the dashboard)
 * @param {() => void} a.onOpen  open this run in the detail pane
 * @returns {HTMLElement} */
export function renderRunningHero({ sk, run, eta, actions, onOpen }) {
  const card = document.createElement("div");
  card.className = "run-hero";

  const eyebrow = document.createElement("span");
  eyebrow.className = "hero-eyebrow";
  eyebrow.textContent = "RUNNING NOW";

  const titleRow = document.createElement("button");
  titleRow.type = "button";
  titleRow.className = "hero-title";
  const name = document.createElement("span");
  name.textContent = `${sk.skill_name} / run #${run.run_id}`;
  const badge = document.createElement("span");
  badge.className = "hero-badge";
  badge.textContent = "RUNNING";
  titleRow.append(name, badge);
  titleRow.addEventListener("click", onOpen);

  const meta = document.createElement("div");
  meta.className = "hero-meta mono";
  const start = run.started_at?.sec || 0;
  const metaParts = [];
  if (run.instance_type) metaParts.push(run.instance_type);
  if (start) metaParts.push(`${fmtDur(Math.max(0, Math.floor(Date.now() / 1000) - start))} elapsed`);
  if (eta != null) metaParts.push(`~${fmtDur(Math.round(eta))} remaining`);
  meta.textContent = metaParts.join(" · ");

  const step = run.current_step || 0;
  const total = run.total_step || 0;
  const stepLine = document.createElement("div");
  stepLine.className = "hero-step mono";
  const pct = total > 0 ? Math.min(100, Math.round((step / total) * 100)) : 0;
  stepLine.textContent =
    total > 0
      ? `${step.toLocaleString()} / ${total.toLocaleString()} steps · ${pct}%`
      : `${step.toLocaleString()} steps`;

  const bar = document.createElement("div");
  bar.className = total > 0 ? "tbar" : "tbar tbar-indeterminate";
  const fill = document.createElement("div");
  fill.className = "tbar-fill";
  if (total > 0) fill.style.width = `${pct}%`;
  bar.appendChild(fill);

  const acts = document.createElement("div");
  acts.className = "hero-actions";
  acts.append(...actions);

  card.append(eyebrow, titleRow, meta, stepLine, bar, acts);
  return card;
}

export { STATUS_RUNNING };
