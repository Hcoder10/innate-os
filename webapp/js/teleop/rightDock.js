// @ts-check
// Right dock — a shared, collapsible, resizable column on the right edge of the
// cockpit that hosts one or more stacked panels (Skills, Chat). Each panel has
// its own little popup toggle pinned to the camera view's right edge; the
// toggles are always visible. Opening any panel insets the cockpit so the feed +
// overlays shift left instead of being overlapped. When one panel is open it
// fills the dock; when several are open they split the height evenly.

const WIDTH_KEY = "innate.dockWidth";
const MIN_WIDTH = 240;
const MAX_WIDTH = 560;
const DEFAULT_WIDTH = 320;

/**
 * @param {HTMLElement} cockpit The teleop view layer (its `right` is inset when open).
 * @returns {{
 *   addPanel: (opts: { key: string, label: string, togglePos: number }) =>
 *     { body: HTMLElement, setOpen: (open: boolean) => void, isOpen: () => boolean },
 *   destroy: () => void,
 * }}
 */
export function createRightDock(cockpit) {
  const stage = cockpit.parentElement || cockpit;

  const dock = document.createElement("div");
  dock.className = "dock";

  const resize = document.createElement("div");
  resize.className = "dock-resize";

  const panes = document.createElement("div");
  panes.className = "dock-panes";

  dock.append(resize, panes);
  stage.appendChild(dock);

  /** @type {Array<{ key: string, open: boolean, pane: HTMLElement, toggle: HTMLElement, chevron: HTMLElement }>} */
  const panels = [];

  let width = clampWidth(Number(localStorage.getItem(WIDTH_KEY)) || DEFAULT_WIDTH);

  const anyOpen = () => panels.some((p) => p.open);

  function layout() {
    const open = anyOpen();
    dock.style.width = `${width}px`;
    dock.classList.toggle("open", open);
    cockpit.style.right = open ? `${width}px` : "";
    for (const p of panels) {
      p.pane.style.display = p.open ? "flex" : "none";
      p.toggle.style.right = open ? `${width}px` : "0px";
      p.toggle.classList.toggle("active", p.open);
      p.chevron.textContent = p.open ? "›" : "‹";
      p.toggle.title = p.open ? `Hide ${p.key}` : `Show ${p.key}`;
    }
  }
  layout();

  /** @param {{ key: string, open: boolean }} panel @param {boolean} open */
  function setOpen(panel, open) {
    panel.open = open;
    localStorage.setItem(`innate.dock.${panel.key}`, open ? "1" : "0");
    layout();
  }

  // ---- shared left-edge resize -------------------------------------------
  /** @type {{ startX: number, startW: number } | null} */
  let drag = null;
  resize.addEventListener("pointerdown", (e) => {
    resize.setPointerCapture(e.pointerId);
    drag = { startX: e.clientX, startW: width };
    dock.classList.add("resizing");
    cockpit.classList.add("dock-resizing");
  });
  resize.addEventListener("pointermove", (e) => {
    if (!drag) return;
    width = clampWidth(drag.startW + (drag.startX - e.clientX)); // drag left widens
    dock.style.width = `${width}px`;
    cockpit.style.right = `${width}px`;
    for (const p of panels) p.toggle.style.right = `${width}px`;
  });
  resize.addEventListener("pointerup", (e) => {
    if (!drag) return;
    try {
      resize.releasePointerCapture(e.pointerId);
    } catch {
      // stale pointer — fine
    }
    drag = null;
    dock.classList.remove("resizing");
    cockpit.classList.remove("dock-resizing");
    localStorage.setItem(WIDTH_KEY, String(width));
  });

  return {
    addPanel({ key, label, togglePos }) {
      const pane = document.createElement("div");
      pane.className = "dock-pane";
      panes.appendChild(pane);

      const toggle = document.createElement("button");
      toggle.className = "dock-toggle";
      toggle.type = "button";
      toggle.style.top = `${togglePos * 100}%`;
      const chevron = document.createElement("span");
      chevron.className = "dock-toggle-chevron";
      const text = document.createElement("span");
      text.className = "dock-toggle-label";
      text.textContent = label;
      toggle.append(chevron, text);
      stage.appendChild(toggle);

      const panel = { key, open: localStorage.getItem(`innate.dock.${key}`) === "1", pane, toggle, chevron };
      panels.push(panel);
      toggle.addEventListener("click", () => setOpen(panel, !panel.open));
      layout();

      return {
        body: pane,
        setOpen: (open) => setOpen(panel, open),
        isOpen: () => panel.open,
      };
    },
    destroy() {
      cockpit.style.right = "";
      cockpit.classList.remove("dock-resizing");
      for (const p of panels) p.toggle.remove();
      dock.remove();
    },
  };
}

/** @param {number} w */
function clampWidth(w) {
  return Math.max(MIN_WIDTH, Math.min(MAX_WIDTH, Math.round(w)));
}
